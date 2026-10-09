"""gnubook's own small SQLite database (data_dir/gnubook.sqlite).

Holds what does not belong into the GnuCash book: stable numeric API ids, import records
(duplicate detection), accepted checkpoint differences and an audit log of all writes.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS account_ids (id INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS tx_ids (id INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    hash TEXT NOT NULL,
    occurrence INTEGER NOT NULL DEFAULT 1,
    account_guid TEXT NOT NULL,
    tx_guid TEXT,
    split_guid TEXT,
    status TEXT NOT NULL,
    source TEXT,
    candidate_tx_guid TEXT,
    counterparty_name TEXT,
    counterparty_iban TEXT,
    day TEXT,
    amount TEXT,
    description TEXT,
    payload TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    UNIQUE (hash, occurrence)
);
CREATE INDEX IF NOT EXISTS imports_split ON imports (split_guid);
CREATE INDEX IF NOT EXISTS imports_tx ON imports (tx_guid);
CREATE TABLE IF NOT EXISTS import_state (
    account_guid TEXT PRIMARY KEY, last_hash TEXT, last_k INTEGER, last_at TEXT
);
CREATE TABLE IF NOT EXISTS checkpoint_acceptance (
    account_guid TEXT NOT NULL, tx_guid TEXT NOT NULL, diff_stand TEXT NOT NULL, diff_end TEXT NOT NULL,
    note TEXT, accepted_at TEXT NOT NULL, PRIMARY KEY (account_guid, tx_guid)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
    tx_guid TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS login_failures (ip TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pp_sync (
    pp_key TEXT PRIMARY KEY, tx_guid TEXT, spec_fp TEXT, gc_fp TEXT, status TEXT NOT NULL, message TEXT,
    kind TEXT, day TEXT, description TEXT, amount TEXT, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pp_sync_tx ON pp_sync (tx_guid);
CREATE TABLE IF NOT EXISTS pp_objects (key TEXT PRIMARY KEY, guid TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pp_prices (
    commodity_guid TEXT NOT NULL, day TEXT NOT NULL, price_guid TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (commodity_guid, day)
);
CREATE TABLE IF NOT EXISTS pp_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, actor TEXT, revision TEXT,
    status TEXT NOT NULL, summary TEXT, details TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class AppDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def conn(self):
        with self._lock:
            c = sqlite3.connect(self.path, timeout=15)
            c.row_factory = sqlite3.Row
            try:
                c.execute("PRAGMA journal_mode=WAL")
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    # ------------------------------------------------------------------ numeric ids
    def _id_for(self, table: str, guid: str) -> int:
        with self.conn() as c:
            row = c.execute(f"SELECT id FROM {table} WHERE guid = ?", (guid,)).fetchone()
            if row:
                return int(row["id"])
            cur = c.execute(f"INSERT INTO {table} (guid) VALUES (?)", (guid,))
            return int(cur.lastrowid)

    def _guid_for(self, table: str, id_: int) -> str | None:
        with self.conn() as c:
            row = c.execute(f"SELECT guid FROM {table} WHERE id = ?", (int(id_),)).fetchone()
            return row["guid"] if row else None

    def account_id(self, guid: str) -> int:
        return self._id_for("account_ids", guid)

    def account_ids(self, guids) -> dict[str, int]:
        guids = list(guids)
        with self.conn() as c:
            known = {r["guid"]: int(r["id"]) for r in c.execute("SELECT id, guid FROM account_ids")}
            for g in guids:
                if g not in known:
                    known[g] = int(c.execute("INSERT INTO account_ids (guid) VALUES (?)", (g,)).lastrowid)
        return {g: known[g] for g in guids}

    def account_guid(self, id_) -> str | None:
        try:
            return self._guid_for("account_ids", int(id_))
        except (TypeError, ValueError):
            return None

    def tx_id(self, guid: str) -> int:
        return self._id_for("tx_ids", guid)

    def tx_guid(self, id_) -> str | None:
        try:
            return self._guid_for("tx_ids", int(id_))
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ imports
    def peek_occurrence(self, account_guid: str, hash_: str, window_seconds: int = 120) -> int:
        """Identical bank lines sent directly one after another count as 2nd, 3rd ... occurrence.

        A repeated import run starts minutes later (FinTS login, TAN), so a short window separates
        "the bank really has this line twice" from "the same line was sent again".
        """
        now = datetime.now(timezone.utc)
        with self.conn() as c:
            row = c.execute("SELECT last_hash, last_k, last_at FROM import_state WHERE account_guid = ?",
                            (account_guid,)).fetchone()
        if row and row["last_hash"] == hash_ and row["last_at"]:
            age = (now - datetime.fromisoformat(row["last_at"])).total_seconds()
            if age <= window_seconds:
                return int(row["last_k"]) + 1
        return 1

    def commit_occurrence(self, account_guid: str, hash_: str, k: int):
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self.conn() as c:
            c.execute("INSERT INTO import_state (account_guid, last_hash, last_k, last_at) VALUES (?, ?, ?, ?) "
                      "ON CONFLICT(account_guid) DO UPDATE SET last_hash = excluded.last_hash, "
                      "last_k = excluded.last_k, last_at = excluded.last_at", (account_guid, hash_, k, now))

    def find_import(self, hash_: str, occurrence: int):
        with self.conn() as c:
            return c.execute("SELECT * FROM imports WHERE hash = ? AND occurrence = ?", (hash_, occurrence)).fetchone()

    def add_import(self, **rec) -> int:
        rec.setdefault("created_at", now_iso())
        if isinstance(rec.get("payload"), (dict, list)):
            rec["payload"] = json.dumps(rec["payload"], ensure_ascii=False, sort_keys=True, default=str)
        cols = ", ".join(rec)
        marks = ", ".join("?" for _ in rec)
        with self.conn() as c:
            return int(c.execute(f"INSERT INTO imports ({cols}) VALUES ({marks})", list(rec.values())).lastrowid)

    def linked_split_guids(self, split_guids) -> set[str]:
        split_guids = list(split_guids)
        out = set()
        with self.conn() as c:
            for i in range(0, len(split_guids), 400):
                chunk = split_guids[i:i + 400]
                q = ",".join("?" for _ in chunk)
                out.update(r["split_guid"] for r in c.execute(
                    f"SELECT split_guid FROM imports WHERE split_guid IN ({q})", chunk))
        return out

    def imports(self, limit: int = 200, only_open: bool = False):
        """Import records, newest first. only_open: not reviewed and possibly needing attention."""
        sql = "SELECT * FROM imports"
        if only_open:
            sql += " WHERE reviewed = 0 AND (status = 'possible_duplicate' OR source = 'fallback')"
        sql += " ORDER BY id DESC LIMIT ?"
        with self.conn() as c:
            return c.execute(sql, (limit,)).fetchall()

    def imports_for_tx(self, tx_guid: str):
        with self.conn() as c:
            return c.execute("SELECT * FROM imports WHERE tx_guid = ? ORDER BY id", (tx_guid,)).fetchall()

    def mark_reviewed(self, ids, reviewed: bool = True):
        ids = [int(i) for i in ids]
        if not ids:
            return
        with self.conn() as c:
            c.execute(f"UPDATE imports SET reviewed = ? WHERE id IN ({','.join('?' for _ in ids)})",
                      [1 if reviewed else 0, *ids])

    def last_import_at(self) -> str | None:
        with self.conn() as c:
            row = c.execute("SELECT MAX(created_at) FROM imports").fetchone()
            return row[0] if row else None

    # ------------------------------------------------------------------ checkpoint acceptance
    def acceptances(self) -> dict:
        with self.conn() as c:
            return {(r["account_guid"], r["tx_guid"]): dict(r) for r in c.execute("SELECT * FROM checkpoint_acceptance")}

    def accept(self, account_guid: str, tx_guid: str, diff_stand, diff_end, note: str = ""):
        with self.conn() as c:
            c.execute("INSERT INTO checkpoint_acceptance (account_guid, tx_guid, diff_stand, diff_end, note, accepted_at) "
                      "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account_guid, tx_guid) DO UPDATE SET "
                      "diff_stand = excluded.diff_stand, diff_end = excluded.diff_end, note = excluded.note, "
                      "accepted_at = excluded.accepted_at",
                      (account_guid, tx_guid, str(diff_stand), str(diff_end), note or "", now_iso()))

    def unaccept(self, account_guid: str, tx_guid: str):
        with self.conn() as c:
            c.execute("DELETE FROM checkpoint_acceptance WHERE account_guid = ? AND tx_guid = ?", (account_guid, tx_guid))

    # ------------------------------------------------------------------ Portfolio Performance link
    def meta(self, key: str, default: str | None = None) -> str | None:
        with self.conn() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str | None):
        with self.conn() as c:
            if value is None:
                c.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                          "value = excluded.value", (key, value))

    def pp_records(self) -> dict:
        with self.conn() as c:
            return {r["pp_key"]: dict(r) for r in c.execute("SELECT * FROM pp_sync")}

    def pp_record_for_tx(self, tx_guid: str):
        with self.conn() as c:
            row = c.execute("SELECT * FROM pp_sync WHERE tx_guid = ?", (tx_guid,)).fetchone()
            return dict(row) if row else None

    def pp_save_records(self, rows: list[dict], delete_keys=()):
        with self.conn() as c:
            for key in delete_keys:
                c.execute("DELETE FROM pp_sync WHERE pp_key = ?", (key,))
            for r in rows:
                r = dict(r, updated_at=now_iso())
                c.execute("INSERT INTO pp_sync (pp_key, tx_guid, spec_fp, gc_fp, status, message, kind, day, "
                          "description, amount, updated_at) VALUES (:pp_key, :tx_guid, :spec_fp, :gc_fp, :status, "
                          ":message, :kind, :day, :description, :amount, :updated_at) ON CONFLICT(pp_key) DO UPDATE "
                          "SET tx_guid = excluded.tx_guid, spec_fp = excluded.spec_fp, gc_fp = excluded.gc_fp, "
                          "status = excluded.status, message = excluded.message, kind = excluded.kind, "
                          "day = excluded.day, description = excluded.description, amount = excluded.amount, "
                          "updated_at = excluded.updated_at",
                          {k: r.get(k) for k in ("pp_key", "tx_guid", "spec_fp", "gc_fp", "status", "message",
                                                 "kind", "day", "description", "amount", "updated_at")})

    def pp_set_status(self, key: str, status: str, message: str | None = None):
        with self.conn() as c:
            c.execute("UPDATE pp_sync SET status = ?, message = ?, updated_at = ? WHERE pp_key = ?",
                      (status, message, now_iso(), key))

    def pp_objects(self) -> dict:
        with self.conn() as c:
            return {r["key"]: r["guid"] for r in c.execute("SELECT key, guid FROM pp_objects")}

    def pp_save_objects(self, mapping: dict):
        with self.conn() as c:
            c.executemany("INSERT INTO pp_objects (key, guid) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                          "guid = excluded.guid", list(mapping.items()))

    def pp_reset_links(self):
        """Forget every link to objects in the GnuCash book (bookings, securities, accounts, prices), e.g. after the
        book was replaced. The next run finds its transactions again by their slot or books them anew."""
        with self.conn() as c:
            c.execute("DELETE FROM pp_sync")
            c.execute("DELETE FROM pp_objects")
            c.execute("DELETE FROM pp_prices")

    def pp_prices(self) -> dict:
        with self.conn() as c:
            return {(r["commodity_guid"], r["day"]): (r["price_guid"], r["value"])
                    for r in c.execute("SELECT * FROM pp_prices")}

    def pp_save_prices(self, upserts: dict, deletes=()):
        with self.conn() as c:
            c.executemany("DELETE FROM pp_prices WHERE commodity_guid = ? AND day = ?", list(deletes))
            c.executemany("INSERT INTO pp_prices (commodity_guid, day, price_guid, value) VALUES (?, ?, ?, ?) "
                          "ON CONFLICT(commodity_guid, day) DO UPDATE SET price_guid = excluded.price_guid, "
                          "value = excluded.value", [(k[0], k[1], v[0], v[1]) for k, v in upserts.items()])

    def pp_add_run(self, started_at: str, actor: str, revision: str, status: str, summary: str,
                   details: str = "") -> int:
        with self.conn() as c:
            rid = int(c.execute("INSERT INTO pp_runs (started_at, finished_at, actor, revision, status, summary, "
                                "details) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (started_at, now_iso(), actor, revision, status, summary, details[:20000])).lastrowid)
            c.execute("DELETE FROM pp_runs WHERE id <= ?", (rid - 200,))
            return rid

    def pp_runs(self, limit: int = 20):
        with self.conn() as c:
            return c.execute("SELECT * FROM pp_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ------------------------------------------------------------------ audit & login throttle
    def audit(self, actor: str, action: str, tx_guid: str | None, summary: str = ""):
        with self.conn() as c:
            c.execute("INSERT INTO audit (ts, actor, action, tx_guid, summary) VALUES (?, ?, ?, ?, ?)",
                      (now_iso(), actor, action, tx_guid, summary[:500]))

    def audit_log(self, limit: int = 100):
        with self.conn() as c:
            return c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def login_failures(self, ip: str, window: float, now: float) -> int:
        with self.conn() as c:
            c.execute("DELETE FROM login_failures WHERE ts < ?", (now - window,))
            return int(c.execute("SELECT COUNT(*) FROM login_failures WHERE ip = ?", (ip,)).fetchone()[0])

    def add_login_failure(self, ip: str, now: float):
        with self.conn() as c:
            c.execute("INSERT INTO login_failures (ip, ts) VALUES (?, ?)", (ip, now))

    def clear_login_failures(self, ip: str):
        with self.conn() as c:
            c.execute("DELETE FROM login_failures WHERE ip = ?", (ip,))
