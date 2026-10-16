"""Documents and runs of the postbox sources in the book's own database (data_dir/books/<id>.sqlite).

pp_status of a document:
    pending       not yet imported into Portfolio Performance (also after pp-core was not reachable)
    imported      PP took over every transaction of the document
    partial       PP took over some transactions, others failed
    review        PP found possible duplicates or other warnings – the user decides on the PDF import page
    needs_target  PP needs the securities account and cash account for this bank once (PDF import page)
    unsupported   PP's importers do not read this kind of document (it is only kept and copied)
    skipped       PP read the document but skips it on purpose (e.g. cost information)
    error         PP could not import it
    done          resolved by the user
    off           no import (switched off, or no Portfolio Performance)
nc_status: pending, done, error, off.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ..appdb import now_iso
from .plan import Listed

PP_RETRY = ("pending", "needs_target")          # imported again by every run
PP_ATTENTION = ("review", "needs_target", "error", "partial")
PP_FINAL = ("imported", "unsupported", "skipped", "done", "off")  # nothing left to do for PP
NC_RETRY = ("pending", "error")
RUNNING_STALE = timedelta(hours=13)              # a "running" run older than this died without finishing


class PostboxStore:
    def __init__(self, appdb):
        self.appdb = appdb

    # ------------------------------------------------------------------ list position
    def _meta_key(self, source: str) -> str:
        return f"postbox_complete_day:{source}"

    def complete_day(self, source: str) -> date | None:
        value = self.appdb.meta(self._meta_key(source))
        try:
            return date.fromisoformat(value) if value else None
        except ValueError:
            return None

    def set_complete_day(self, source: str, day: date | None):
        self.appdb.set_meta(self._meta_key(source), day.isoformat() if day else None)

    # ------------------------------------------------------------------ documents
    def known_ids(self, source: str) -> set[str]:
        with self.appdb.conn() as c:
            return {r[0] for r in c.execute("SELECT external_id FROM postbox_docs WHERE source = ?", (source,))}

    def add_doc(self, source: str, doc: Listed, filename: str, sha256: str, size: int,
                pp_status: str = "pending", nc_status: str = "pending") -> int | None:
        """New document; None when the id is already known (nothing is changed then)."""
        with self.appdb.conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO postbox_docs (source, external_id, title, day, filename, sha256, size, "
                "fetched_at, pp_status, nc_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source, doc.external_id, doc.title, doc.day.isoformat() if doc.day else None, filename, sha256,
                 size, now_iso(), pp_status, nc_status))
            return int(cur.lastrowid) if cur.rowcount else None

    def doc(self, doc_id: int):
        with self.appdb.conn() as c:
            return c.execute("SELECT * FROM postbox_docs WHERE id = ?", (int(doc_id),)).fetchone()

    def docs(self, source: str | None = None, attention: bool = False, limit: int = 200, offset: int = 0):
        where, args = [], []
        if source:
            where.append("source = ?")
            args.append(source)
        if attention:
            where.append(f"(pp_status IN ({','.join('?' * len(PP_ATTENTION))}) OR nc_status = 'error')")
            args += list(PP_ATTENTION)
        sql = "SELECT * FROM postbox_docs" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY COALESCE(day, '') DESC, id DESC LIMIT ? OFFSET ?"
        with self.appdb.conn() as c:
            return c.execute(sql, args + [int(limit), int(offset)]).fetchall()

    def counts(self, source: str) -> dict:
        with self.appdb.conn() as c:
            pp = {r[0]: r[1] for r in c.execute("SELECT pp_status, COUNT(*) FROM postbox_docs WHERE source = ? "
                                                "GROUP BY pp_status", (source,))}
            nc = {r[0]: r[1] for r in c.execute("SELECT nc_status, COUNT(*) FROM postbox_docs WHERE source = ? "
                                                "GROUP BY nc_status", (source,))}
        return {"total": sum(pp.values()), "pp": pp, "nc": nc,
                "attention": sum(pp.get(s, 0) for s in PP_ATTENTION) + nc.get("error", 0)}

    def for_pp(self, source: str, statuses=PP_RETRY):
        with self.appdb.conn() as c:
            return c.execute(f"SELECT * FROM postbox_docs WHERE source = ? AND pp_status IN "
                             f"({','.join('?' * len(statuses))}) ORDER BY COALESCE(day, ''), id",
                             (source, *statuses)).fetchall()

    def for_nc(self, source: str, statuses=NC_RETRY):
        """Pending documents first (oldest first), then those whose upload failed before."""
        with self.appdb.conn() as c:
            return c.execute(f"SELECT * FROM postbox_docs WHERE source = ? AND nc_status IN "
                             f"({','.join('?' * len(statuses))}) "
                             f"ORDER BY nc_status <> 'pending', COALESCE(day, ''), id",
                             (source, *statuses)).fetchall()

    def finished(self, source: str):
        """Documents in Nextcloud with nothing left to do for PP: their local PDF may go."""
        with self.appdb.conn() as c:
            return c.execute(f"SELECT * FROM postbox_docs WHERE source = ? AND nc_status = 'done' AND pp_status IN "
                             f"({','.join('?' * len(PP_FINAL))})", (source, *PP_FINAL)).fetchall()

    def with_session(self, source: str):
        """Documents waiting for a decision in a pp-core import session that may still exist (6 hours)."""
        since = (datetime.now(timezone.utc) - timedelta(hours=6)).replace(microsecond=0).isoformat()
        with self.appdb.conn() as c:
            return c.execute("SELECT * FROM postbox_docs WHERE source = ? AND pp_session IS NOT NULL AND pp_at >= ? "
                             "AND pp_status IN ('review', 'needs_target', 'error', 'partial')",
                             (source, since)).fetchall()

    def set_pp(self, doc_id: int, status: str, message: str | None = None, items: int | None = None,
               imported: int | None = None, session: str | None = None):
        with self.appdb.conn() as c:
            c.execute("UPDATE postbox_docs SET pp_status = ?, pp_message = ?, pp_items = ?, pp_imported = ?, "
                      "pp_session = ?, pp_at = ? WHERE id = ?",
                      (status, (message or None) and str(message)[:500], items, imported, session, now_iso(),
                       int(doc_id)))

    def set_nc(self, doc_id: int, status: str, path: str | None = None, message: str | None = None):
        with self.appdb.conn() as c:
            c.execute("UPDATE postbox_docs SET nc_status = ?, nc_path = COALESCE(?, nc_path), nc_message = ?, "
                      "nc_at = ? WHERE id = ?",
                      (status, path, (message or None) and str(message)[:500], now_iso(), int(doc_id)))

    def reset_nc(self, source: str, statuses=("off", "error")) -> int:
        with self.appdb.conn() as c:
            cur = c.execute(f"UPDATE postbox_docs SET nc_status = 'pending', nc_message = NULL WHERE source = ? AND "
                            f"nc_status IN ({','.join('?' * len(statuses))})", (source, *statuses))
            return cur.rowcount

    def forget(self, doc_id: int):
        with self.appdb.conn() as c:
            c.execute("DELETE FROM postbox_docs WHERE id = ?", (int(doc_id),))

    # ------------------------------------------------------------------ runs
    def start_run(self, source: str, actor: str, dry_run: bool = False) -> int:
        with self.appdb.conn() as c:
            return int(c.execute("INSERT INTO postbox_runs (source, started_at, actor, dry_run, status) "
                                 "VALUES (?, ?, ?, ?, 'running')", (source, now_iso(), actor, int(dry_run))).lastrowid)

    def finish_run(self, run_id: int, status: str, message: str = "", listed: int | None = None,
                   new: int | None = None, downloaded: int | None = None, clicks: int | None = None):
        with self.appdb.conn() as c:
            c.execute("UPDATE postbox_runs SET finished_at = ?, status = ?, message = ?, listed = ?, new = ?, "
                      "downloaded = ?, clicks = ? WHERE id = ?",
                      (now_iso(), status, (message or "")[:1000], listed, new, downloaded, clicks, int(run_id)))

    def runs(self, source: str, limit: int = 10):
        with self.appdb.conn() as c:
            return c.execute("SELECT * FROM postbox_runs WHERE source = ? ORDER BY id DESC LIMIT ?",
                             (source, int(limit))).fetchall()

    def running(self, source: str):
        """The run in progress (status running, not stale), or None."""
        since = (datetime.now(timezone.utc) - RUNNING_STALE).replace(microsecond=0).isoformat()
        with self.appdb.conn() as c:
            return c.execute("SELECT * FROM postbox_runs WHERE source = ? AND status = 'running' AND started_at >= ? "
                             "ORDER BY id DESC LIMIT 1", (source, since)).fetchone()

    def close_stale_runs(self, source: str):
        since = (datetime.now(timezone.utc) - RUNNING_STALE).replace(microsecond=0).isoformat()
        with self.appdb.conn() as c:
            c.execute("UPDATE postbox_runs SET status = 'error', finished_at = ?, message = 'abgebrochen' "
                      "WHERE source = ? AND status = 'running' AND started_at < ?", (now_iso(), source, since))
