"""Copy of the book as a GnuCash SQLite file (.gnucash) after every change.

GnuCash Desktop opens the file directly (Datei → Öffnen). For a PostgreSQL book the tables are copied
row by row into a fresh SQLite file in GnuCash's SQLite layout (timestamps as text, dates as YYYYMMDD);
for an SQLite book the database is copied with SQLite's backup API. The copy is written to a temporary
file and renamed, so the target is never half-written. Older copies are rotated (keep).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import MetaData, types

log = logging.getLogger("gnubook.backup")


def _sqlite_type(col) -> str:
    t = col.type
    if isinstance(t, types.DateTime):
        return "text(19)"
    if isinstance(t, types.Date):
        return "text(8)"
    if isinstance(t, (types.Integer,)):
        return "integer" if not isinstance(t, types.BigInteger) else "bigint"
    if isinstance(t, (types.Float, types.Numeric)):
        return "real"
    length = getattr(t, "length", None)
    return f"text({length})" if length else "text"


def _value(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y%m%d")
    return v


def write_gnucash_file(engine, target: Path):
    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    if engine.dialect.name == "sqlite":
        src = sqlite3.connect(engine.url.database)
        dst = sqlite3.connect(tmp)
        with dst:
            src.backup(dst)
        src.close()
        dst.execute("DELETE FROM gnclock")
        dst.commit()
        dst.close()
    else:
        meta = MetaData()
        meta.reflect(bind=engine)
        dst = sqlite3.connect(tmp)
        with engine.connect() as conn:
            for table in meta.sorted_tables:
                cols = list(table.columns)
                pk = [c.name for c in cols if c.primary_key]
                defs = []
                for c in cols:
                    d = f'"{c.name}" {_sqlite_type(c)}'
                    if len(pk) == 1 and c.primary_key:
                        d += " PRIMARY KEY" + (" AUTOINCREMENT" if c.autoincrement is True or
                                               (isinstance(c.type, types.Integer) and c.name == "id") else "")
                    elif not c.nullable:
                        d += " NOT NULL"
                    defs.append(d)
                if len(pk) > 1:
                    defs.append("PRIMARY KEY (" + ", ".join(f'"{p}"' for p in pk) + ")")
                dst.execute(f'CREATE TABLE "{table.name}" ({", ".join(defs)})')
                if table.name == "gnclock":
                    continue
                names = ", ".join(f'"{c.name}"' for c in cols)
                marks = ", ".join("?" for _ in cols)
                rows = conn.execute(table.select())
                dst.executemany(f'INSERT INTO "{table.name}" ({names}) VALUES ({marks})',
                                ([_value(v) for v in r] for r in rows))
                for idx in table.indexes:
                    dst.execute(f'CREATE INDEX "{idx.name}" ON "{table.name}" ('
                                + ", ".join(f'"{c.name}"' for c in idx.columns) + ")")
        dst.commit()
        dst.close()
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


class BackupWriter:
    """Writes the .gnucash copy in the background after changes (coalesces bursts, e.g. imports)."""

    def __init__(self, book, path: str, keep: int = 10, delay: float = 3.0):
        self.book = book
        self.path = Path(path)
        self.keep = keep
        self.delay = delay
        self._pending = threading.Event()
        self._lock = threading.Lock()
        self.last_ok: str | None = None
        self.last_error: str | None = None
        threading.Thread(target=self._loop, daemon=True, name="gnucash-backup").start()

    def request(self):
        self._pending.set()

    def _rotate(self):
        if self.keep <= 1 or not self.path.exists():
            return
        stamp = datetime.fromtimestamp(self.path.stat().st_mtime).strftime("%Y%m%d-%H%M%S-%f")
        os.replace(self.path, self.path.with_name(f"{self.path.stem}.{stamp}{self.path.suffix}"))
        old = sorted(self.path.parent.glob(f"{self.path.stem}.*{self.path.suffix}"))
        for f in old[:-(self.keep - 1)]:
            f.unlink(missing_ok=True)

    def run_now(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                tmp_target = self.path.with_name(self.path.name + ".new")
                write_gnucash_file(self.book.engine, tmp_target)
                self._rotate()
                os.replace(tmp_target, self.path)
                self.last_ok = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                self.last_error = None
            except Exception as exc:  # never break the actual write
                self.last_error = str(exc)
                log.exception("GnuCash-Sicherung fehlgeschlagen")

    def _loop(self):
        while True:
            self._pending.wait()
            time.sleep(self.delay)
            self._pending.clear()
            self.run_now()
