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


def export_gnucash_file(book, target) -> Path:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_gnucash_file(book.engine, target)
    return target


def rotate(directory, prefix: str, keep: int):
    files = sorted(Path(directory).glob(f"{prefix}-*.gnucash"))
    for f in files[:-keep] if keep > 0 else []:
        f.unlink(missing_ok=True)


def extra_name(filename: str, suffix: str) -> str:
    """'Hauptbuch.gnucash' + '.xml' -> 'Hauptbuch-PP.xml' (next to the book copy)."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return f"{stem}-PP{suffix}"


class BackupWriter:
    """Writes the .gnucash copy in the background after changes (coalesces bursts, e.g. imports).

    `path` is the local file ('' = no local copy). `remote` returns the Nextcloud targets of the book
    (dicts with user_id, kind, server, login, decrypted app_password, dav_user, legacy, folder, filename); it is asked on every run,
    so targets added in the settings take effect without a restart. Without a local file the copy is built
    in `work_dir` and removed after the uploads.
    """

    def __init__(self, book, path: str, keep: int = 10, delay: float = 3.0, remote=None, work_dir=None,
                 extras=None):
        self.book = book
        # extras() -> [(suffix, bytes)]: more files copied next to the .gnucash file into Nextcloud
        # (the Portfolio Performance file, as "<name>-PP<suffix>")
        self.extras = extras
        self._extras_pending = threading.Event()
        self._book_changed = False
        self.path = Path(path) if path else None
        self.keep = keep
        self.delay = delay
        self.remote = remote
        self.work_dir = Path(work_dir) if work_dir else None
        self._pending = threading.Event()
        self._lock = threading.Lock()
        self.last_ok: str | None = None
        self.last_error: str | None = None
        self.remote_status: dict[int, dict] = {}  # user id -> {"ok": time, "error": text}
        threading.Thread(target=self._loop, daemon=True, name="gnucash-backup").start()

    def request(self):
        self._book_changed = True
        self._pending.set()

    def request_extras(self):
        """Only the extra files changed (e.g. the PP file): upload them, no new .gnucash version."""
        self._extras_pending.set()
        self._pending.set()

    def flush(self):
        """Write a requested copy now – for the command line, which ends before the background thread runs."""
        if self._pending.is_set():
            self._pending.clear()
            self._run()

    def _run(self):
        book, self._book_changed = self._book_changed, False
        extras = self._extras_pending.is_set()
        self._extras_pending.clear()
        if book:
            self.run_now()  # uploads the extra files as well
        elif extras:
            self.upload_extras()

    def _rotate(self):
        if self.keep <= 1 or not self.path.exists():
            return
        stamp = datetime.fromtimestamp(self.path.stat().st_mtime).strftime("%Y%m%d-%H%M%S-%f")
        os.replace(self.path, self.path.with_name(f"{self.path.stem}.{stamp}{self.path.suffix}"))
        old = sorted(self.path.parent.glob(f"{self.path.stem}.*{self.path.suffix}"))
        for f in old[:-(self.keep - 1)]:
            f.unlink(missing_ok=True)

    def _targets(self):
        if self.remote is None:
            return []
        try:
            return list(self.remote())
        except Exception:  # noqa: BLE001
            log.exception("Nextcloud-Ziele konnten nicht gelesen werden")
            return []

    def run_now(self):
        with self._lock:
            targets = self._targets()
            if self.path is None and not targets:
                return
            now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            try:
                if self.path is not None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_target = self.path.with_name(self.path.name + ".new")
                    write_gnucash_file(self.book.engine, tmp_target)
                    self._rotate()
                    os.replace(tmp_target, self.path)
                    source = self.path
                else:
                    base = self.work_dir or Path(".")
                    base.mkdir(parents=True, exist_ok=True)
                    source = base / f"upload-{threading.get_ident()}.gnucash"
                    write_gnucash_file(self.book.engine, source)
                self.last_ok = now
                self.last_error = None
            except Exception as exc:  # never break the actual write
                self.last_error = str(exc)
                log.exception("GnuCash-Sicherung fehlgeschlagen")
                return
            try:
                self._upload(source, targets, now)
            finally:
                if self.path is None:
                    source.unlink(missing_ok=True)

    def _extra_files(self) -> list:
        if self.extras is None:
            return []
        try:
            return list(self.extras())
        except Exception as exc:  # noqa: BLE001 – the book copy must not fail because of an extra file
            log.warning("Zusatzdatei für Nextcloud nicht lesbar: %s", exc)
            return []

    def upload_extras(self):
        """Upload only the extra files to every Nextcloud target."""
        with self._lock:
            targets = self._targets()
            files = self._extra_files() if targets else []
            if not files:
                return
            from .nextcloud import client_for

            now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
            for t in targets:
                st = self.remote_status.setdefault(t["user_id"], {"ok": None, "error": None})
                try:
                    if t.get("error"):
                        raise RuntimeError(t["error"])
                    client = client_for(t, t["app_password"])
                    for suffix, data in files:
                        client.upload_bytes(data, t["folder"], extra_name(t["filename"], suffix))
                    st.update(ok=now, error=None)
                except Exception as exc:  # noqa: BLE001
                    st["error"] = str(exc)
                    log.warning("Nextcloud-Upload für Benutzer %s fehlgeschlagen: %s", t["user_id"], exc)

    def _upload(self, source: Path, targets, now: str):
        from .nextcloud import client_for

        files = self._extra_files() if targets else []

        seen = set()
        for t in targets:
            seen.add(t["user_id"])
            st = self.remote_status.setdefault(t["user_id"], {"ok": None, "error": None})
            try:
                if t.get("error"):
                    raise RuntimeError(t["error"])
                client = client_for(t, t["app_password"])
                client.upload(source, t["folder"], t["filename"])
                for suffix, data in files:
                    client.upload_bytes(data, t["folder"], extra_name(t["filename"], suffix))
                st.update(ok=now, error=None)
            except Exception as exc:  # noqa: BLE001 – one failing Nextcloud must not stop the others
                st["error"] = str(exc)
                log.warning("Nextcloud-Upload für Benutzer %s fehlgeschlagen: %s", t["user_id"], exc)
        for uid in list(self.remote_status):
            if uid not in seen:
                del self.remote_status[uid]

    def _loop(self):
        while True:
            self._pending.wait()
            time.sleep(self.delay)
            self._pending.clear()
            self._run()
