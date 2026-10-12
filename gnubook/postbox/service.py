"""One run of a postbox source: fetch new documents, keep them, import them into PP, copy them into Nextcloud.

Runs come from the timer (`gnubook postbox-fetch --scheduled`), from the web app (a request file, picked up by
gnubook-postbox-request.path) or from the command line. One run per book and source at a time (a lock file); the
web app never starts the browser itself. After a failed login the timer pauses until the settings are saved again
(a broker may lock an account after a few failed logins); runs started by hand still log in.

Every downloaded PDF is stored at once below data_dir/postbox/book-<id>/<source>/<year>/ and recorded in the
book's own database, so a run that breaks off keeps what it got. Importing and uploading work on the recorded
documents afterwards: documents that pp-core or Nextcloud could not take are retried by the next run.
"""
from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..i18n import gettext as _
from . import jobs, plan
from .fzero import Cancelled, FetchError, FinanzenZeroFetcher, _first_line
from .settings import SourceSettings
from .store import PP_ATTENTION, PostboxStore

log = logging.getLogger("gnubook.postbox")

PP_CHUNK = 25                    # documents per pp-core import call
NC_FAILURES_IN_A_ROW = 3         # then Nextcloud is probably unreachable: the rest waits for the next run
NOT_RECOGNISED = ("no transactions recognised", "unsupported bank or document type")
TARGET_MISSING = "target account missing"
PAUSING = ("login_failed", "approval_required")
TZ = ZoneInfo("Europe/Berlin")
STOP_TEXTS = {
    "date": "bis zum bekannten Stand gelesen",
    "end": "Ende der Liste erreicht",
    "limit": "Klickgrenze erreicht ([postbox] max_clicks) – ältere Belege fehlen noch",
    "stalled": "„Mehr anzeigen“ lud nichts nach – ältere Belege fehlen evtl. noch",
}


class Busy(RuntimeError):
    pass


def _today():
    return datetime.now(TZ).date()


@dataclass
class RunOutcome:
    # ok | warning | dry_run | paused | busy | error | login_failed | approval_required | not_installed
    status: str
    message: str = ""
    listed: int = 0
    new: int = 0
    downloaded: int = 0
    clicks: int = 0
    processed: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status not in ("ok", "warning", "dry_run", "busy", "paused")


def make_fetcher(kind: str, cfg, login: str, password: str, work_dir: Path):
    """The fetcher of a source (replaced in tests). work_dir: data_dir/postbox."""
    if kind == "finanzen-zero":
        return FinanzenZeroFetcher(cfg.postbox, login, password, work_dir / "debug", work_dir / "home")
    raise ValueError(f"unknown postbox source {kind}")


class PostboxService:
    def __init__(self, reg, ctx):
        self.reg = reg
        self.ctx = ctx
        self.cfg = reg.cfg
        self.store = PostboxStore(ctx.appdb)

    # ------------------------------------------------------------------ paths and settings
    @property
    def root(self) -> Path:
        return Path(self.cfg.app.data_dir) / "postbox"

    def book_dir(self, kind: str) -> Path:
        return self.root / f"book-{self.ctx.id}" / kind

    def doc_path(self, row) -> Path:
        year = (row["day"] or "")[:4] or "ohne-Datum"
        return self.book_dir(row["source"]) / year / row["filename"]

    def source(self, kind: str):
        return self.reg.system.postbox_source(self.ctx.id, kind)

    @staticmethod
    def settings_of(row) -> SourceSettings:
        return SourceSettings.from_json(row["settings"] if row is not None else None)

    def paused(self, kind: str, row=None):
        """The run after which scheduled runs pause (failed login or approval asked, settings not saved since),
        or None."""
        row = row if row is not None else self.source(kind)
        if row is None:
            return None
        # the last run that got as far as a login decision (a run that failed earlier, e.g. site down, says
        # nothing about the password)
        decided = PAUSING + ("ok", "warning", "dry_run")
        last = next((r for r in self.store.runs(kind, 20) if r["status"] in decided), None)
        if last is None or last["status"] not in PAUSING:
            return None
        return last if (row["updated_at"] or "") <= last["started_at"] else None

    def alive(self, kind: str) -> bool | None:
        """True when a process holds the run lock of the source, False when none does (a run shown as running
        died), None when that cannot be told (lock file not readable, e.g. created by root)."""
        try:
            fd = os.open(self.root / f"book-{self.ctx.id}-{kind}.lock", os.O_RDONLY)
        except FileNotFoundError:
            return False
        except OSError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        finally:
            os.close(fd)  # closing also releases a lock taken here
        return False

    def close_dead_run(self, kind: str) -> bool:
        """A run shown as running whose process is gone (killed, container restarted): mark it and clear a
        cancel request, so the page does not wait for it. True when one was closed."""
        run = self.store.running(kind)
        if run is None or self.alive(kind) is not False:
            return False
        self.store.finish_run(run["id"], "cancelled", _("abgebrochen – der Prozess lief nicht mehr"))
        jobs.clear_cancel(self.cfg.app.data_dir, self.ctx.id, kind)
        return True

    @contextmanager
    def _lock(self, kind: str, wait: float = 0):
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / f"book-{self.ctx.id}-{kind}.lock", os.O_RDONLY | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Busy() from None
                    time.sleep(2)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ------------------------------------------------------------------ run
    def run(self, kind: str, actor: str, mode: str = "fetch", sync_now: bool = True, scheduled: bool = False,
            wait: float = 0) -> RunOutcome:
        """mode: fetch | dry-run | process (only import/upload what is pending). sync_now: book PP changes into
        GnuCash right away (command line) instead of in the web app's background worker. scheduled: a timer run
        (pauses after a failed login). wait: seconds to wait for a run of the same source that is in progress."""
        try:
            with self._lock(kind, wait):
                return self._run(kind, actor, mode, sync_now, scheduled)
        except Busy:
            return RunOutcome("busy", _("Für dieses Buch läuft gerade schon ein Abruf."))

    def cancelled(self, kind: str) -> bool:
        return jobs.cancel_requested(self.cfg.app.data_dir, self.ctx.id, kind)

    def _run(self, kind: str, actor: str, mode: str, sync_now: bool, scheduled: bool) -> RunOutcome:
        jobs.clear_cancel(self.cfg.app.data_dir, self.ctx.id, kind)  # a cancel meant for an earlier run
        try:
            return self._run_source(kind, actor, mode, sync_now, scheduled)
        finally:
            jobs.clear_cancel(self.cfg.app.data_dir, self.ctx.id, kind)

    def _run_source(self, kind: str, actor: str, mode: str, sync_now: bool, scheduled: bool) -> RunOutcome:
        row = self.source(kind)
        if row is None:
            return RunOutcome("error", _("Für dieses Buch ist kein Postfach eingerichtet."))
        st = self.settings_of(row)
        self.store.close_stale_runs(kind)
        if mode == "process":
            done = self.process(kind, row, st, sync_now)
            return RunOutcome("ok", self._process_text(done), processed=done)
        if scheduled and self.paused(kind, row) is not None:
            return RunOutcome("paused", _("Automatischer Abruf pausiert: die letzte Anmeldung schlug fehl. Zugangsdaten prüfen und speichern oder von Hand abrufen."))
        try:
            password = self.reg.system.postbox_password(row)
        except Exception as exc:  # noqa: BLE001 – key changed: report instead of crashing
            return RunOutcome("error", _("Gespeichertes Passwort nicht lesbar: {a0}", a0=exc))
        if not row["login"] or not password:
            return RunOutcome("error", _("Benutzername oder Passwort fehlt."))

        dry_run = mode == "dry-run"
        run_id = self.store.start_run(kind, actor, dry_run)
        out = RunOutcome("error", _("abgebrochen"))
        try:
            out = self._fetch_and_process(kind, row, st, password, dry_run, sync_now)
            return out
        except BaseException as exc:  # also SIGTERM (SystemExit) from systemd
            out = RunOutcome("error", _("abgebrochen: {a0}", a0=_first_line(exc)))
            raise
        finally:
            # also when something unexpected happened: the run must not stay "running"
            self.store.finish_run(run_id, out.status, out.message, out.listed or None, out.new or None,
                                  out.downloaded or None, out.clicks or None)

    def _fetch_and_process(self, kind: str, row, st: SourceSettings, password: str, dry_run: bool,
                           sync_now: bool) -> RunOutcome:
        complete = self.store.complete_day(kind)
        stop = plan.stop_day(st.since_day, complete, st.overlap_days, st.rescan_day)
        known = self.store.known_ids(kind)
        fetcher = make_fetcher(kind, self.cfg, row["login"], password, self.root)
        fetcher.cancelled = lambda: self.cancelled(kind)
        try:
            result = fetcher.fetch(stop, lambda listed: plan.select(listed, known, st.since_day),
                                   lambda doc, data: self._save(kind, row, st, doc, data), dry_run=dry_run)
        except FetchError as exc:
            message = str(exc)
            if exc.debug:
                message += " " + _("(Screenshot: {a0}.png)", a0=exc.debug)
            log.warning("Postfach %s (Buch %s): %s", kind, self.ctx.id, message)
            if isinstance(exc, Cancelled):
                msg = _("Abgebrochen – bis dahin geladene Belege bleiben erhalten und werden beim nächsten Lauf übernommen und kopiert.")
                return RunOutcome(exc.code, msg)
            if not dry_run:
                try:
                    self.process(kind, row, st, sync_now)  # earlier documents still get imported and copied
                except Exception:  # noqa: BLE001 – the run's result is the failed login, not this
                    log.exception("Postfach: Nachholen nach fehlgeschlagenem Abruf")
            return RunOutcome(exc.code, message)

        out = RunOutcome("ok", listed=len(result.listed), new=len(result.selected), downloaded=result.downloaded,
                         clicks=result.clicks)
        reach = _(STOP_TEXTS.get(result.stopped, result.stopped))
        if dry_run:
            oldest = min((d.day for d in result.listed if d.day), default=None)
            out.status = "dry_run"
            out.message = _("Probelauf: {a0} Einträge gelesen (bis {a1}), davon {a2} neu – {a3}.",
                            a0=len(result.listed), a1=oldest.strftime("%d.%m.%Y") if oldest else "?",
                            a2=len(result.selected), a3=reach)
            return out

        if result.complete:
            failed_days = [d.day for d, _msg in result.failed]
            new_complete = plan.complete_day_after(result.listed, failed_days, complete)
            today = _today()
            if new_complete is not None and new_complete > today:
                new_complete = today  # a misread or future-dated entry must not make later runs stop too early
            self.store.set_complete_day(kind, new_complete)
            if st.rescan_from:
                self._clear_rescan(kind, st.rescan_from)
        done = self.process(kind, row, st, sync_now)
        out.processed = done
        parts = [_("{a0} neu heruntergeladen", a0=result.downloaded), reach]
        if result.failed:
            out.status = "warning"
            parts.append(_("{a0} Downloads fehlgeschlagen (z. B. {a1}: {a2})", a0=len(result.failed),
                           a1=result.failed[0][0].title, a2=result.failed[0][1]))
        linkless = [d for d in result.listed if not d.href and d.external_id not in self.store.known_ids(kind)
                    and (d.day is None or st.since_day is None or d.day >= st.since_day)]
        if linkless:
            out.status = "warning"
            parts.append(_("{a0} Einträge ohne Link übersprungen (z. B. {a1})", a0=len(linkless), a1=linkless[0].title))
        if not result.complete:
            out.status = "warning"
        parts.append(self._process_text(done))
        out.message = " · ".join(p for p in parts if p)
        return out

    def _clear_rescan(self, kind: str, used: str):
        """The one-time look back is used up – unless the settings were saved with a new one during the run."""
        row = self.source(kind)
        if row is None:
            return
        current = self.settings_of(row)
        if current.rescan_from == used:
            current.rescan_from = ""
            self.reg.system.set_postbox_settings(row["id"], current.to_json())

    def _save(self, kind: str, row, st: SourceSettings, doc: plan.Listed, data: bytes):
        name = plan.file_name(doc.day, doc.external_id, doc.title)
        path = self.book_dir(kind) / (doc.day.strftime("%Y") if doc.day else "ohne-Datum") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.chmod(0o600)
        os.replace(tmp, path)
        pp_status = "pending" if st.import_pp and self.ctx.pp is not None else "off"
        nc_status = "pending" if st.nc_on and row["user_id"] is not None and self.cfg.nextcloud.enabled else "off"
        self.store.add_doc(kind, doc, name, hashlib.sha256(data).hexdigest(), len(data), pp_status, nc_status)

    @staticmethod
    def _process_text(done: dict) -> str:
        parts = []
        if done.get("imported"):
            parts.append(_("{a0} in Portfolio Performance übernommen", a0=done["imported"]))
        if done.get("attention"):
            parts.append(_("{a0} zur Prüfung", a0=done["attention"]))
        if done.get("uploaded"):
            parts.append(_("{a0} in die Nextcloud kopiert", a0=done["uploaded"]))
        for msg in done.get("errors", [])[:2]:
            parts.append(msg)
        return " · ".join(parts)

    # ------------------------------------------------------------------ import and upload
    def process(self, kind: str, row=None, st: SourceSettings | None = None, sync_now: bool = False) -> dict:
        """PP import and Nextcloud copy of every document that still needs it."""
        row = row if row is not None else self.source(kind)
        st = st if st is not None else self.settings_of(row)
        done = {"imported": 0, "attention": 0, "uploaded": 0, "errors": []}
        self.refresh_sessions(kind)
        self._process_pp(kind, st, done, sync_now)
        if row is not None:
            self._process_nc(kind, row, st, done)
        return done

    def _process_pp(self, kind: str, st: SourceSettings, done: dict, sync_now: bool):
        rows = self.store.for_pp(kind)
        if not rows:
            return
        svc = self.ctx.pp
        if svc is None or not st.import_pp:
            for r in rows:
                if r["pp_status"] == "pending":
                    self.store.set_pp(r["id"], "off")
            return
        from ..pp.client import PPCoreError

        try:
            exists = svc.client.summary(svc.cid).get("exists")
        except PPCoreError as exc:
            done["errors"].append(_("Portfolio Performance: {a0}", a0=exc))
            return
        if not exists:
            done["errors"].append(_("Noch keine Portfolio-Performance-Datei – unter Portfolio Performance → Einstellungen hochladen oder anlegen."))
            return
        imported = 0
        for i in range(0, len(rows), PP_CHUNK):
            if self.cancelled(kind):
                done["errors"].append(_("abgebrochen"))
                break
            chunk, files = [], []
            for r in rows[i:i + PP_CHUNK]:
                try:
                    files.append((r["filename"], self.doc_path(r).read_bytes()))
                    chunk.append(r)
                except OSError as exc:
                    self.store.set_pp(r["id"], "error", _("PDF fehlt: {a0}", a0=exc))
            if not files:
                continue
            try:
                result = svc.client.import_pdfs(svc.cid, files, apply=True, auto_feed=True)
            except PPCoreError as exc:
                done["errors"].append(_("Portfolio Performance: {a0}", a0=exc))
                if exc.unreachable:
                    break
                continue
            n, attention = self._apply_result(chunk, result)
            imported += n
            done["attention"] += attention
        done["imported"] += imported
        if imported:
            self._after_pp_change(sync_now, done)

    def _after_pp_change(self, sync_now: bool, done: dict):
        """Book the new PP transactions into GnuCash and copy the PP file into Nextcloud."""
        svc = self.ctx.pp
        if not sync_now:
            svc.request_sync()  # web app: background worker
            return
        if svc.enabled:
            from ..book import WriteLockError
            from ..pp.client import PPCoreError
            from ..pp.sync import SyncError

            try:
                r = svc.sync(actor="postbox", wait=600)
                log.info("Postfach: PP-Übernahme %s", r.summary())
            except (WriteLockError, SyncError, PPCoreError) as exc:
                # the hourly gnubook-pp.timer books it later
                done["errors"].append(_("Übernahme ins Buch später: {a0}", a0=exc))
            except Exception as exc:  # noqa: BLE001 – the documents are imported; booking is retried by the timer
                log.exception("Postfach: Übernahme ins Buch fehlgeschlagen")
                done["errors"].append(_("Übernahme ins Buch später: {a0}", a0=exc))
        svc.request_copy()
        self.ctx.backup.flush()

    def _apply_result(self, chunk, result: dict) -> tuple[int, int]:
        """Status of every document of an import call from pp-core's answer. Returns (documents with imported
        transactions, documents that need attention)."""
        items = defaultdict(list)
        for it in result.get("items") or []:
            items[it.get("file")].append(it)
        file_errors = defaultdict(list)
        for fe in result.get("fileErrors") or []:
            file_errors[fe.get("file")].append(fe.get("message") or "")
        needs_target = bool(result.get("needsTarget"))
        session = result.get("session")
        imported_docs = attention = 0
        for r in chunk:
            its = items.get(r["filename"], [])
            status, message = _doc_status(its, file_errors.get(r["filename"], []), needs_target)
            n = sum(1 for i in its if i.get("imported") and i.get("kind") != "SecurityItem")
            self.store.set_pp(r["id"], status, message, items=sum(1 for i in its if i.get("kind") != "SecurityItem"),
                              imported=n, session=session if status in PP_ATTENTION else None)
            imported_docs += 1 if n else 0
            attention += 1 if status in PP_ATTENTION else 0
        return imported_docs, attention

    def refresh_sessions(self, kind: str):
        """Documents decided on the PDF import page (forced or with a chosen securities account) since the run.
        pp-core's GET of a session only shows it (and checks it again) – it imports nothing."""
        svc = self.ctx.pp
        rows = self.store.with_session(kind)
        if svc is None or not rows:
            return
        from ..pp.client import PPCoreError

        by_session = defaultdict(list)
        for r in rows:
            by_session[r["pp_session"]].append(r)
        for sid, group in by_session.items():
            try:
                result = svc.client.import_session(svc.cid, sid)
            except PPCoreError as exc:
                if exc.status == 404:  # expired: keep the status, forget the session
                    for r in group:
                        self.store.set_pp(r["id"], r["pp_status"], r["pp_message"], r["pp_items"], r["pp_imported"])
                continue
            self._apply_result(group, result)

    def review(self, doc_id: int) -> str | None:
        """pp-core import session for deciding about one document on the PDF import page: the session of the
        run while it exists, otherwise the document is imported once more (OK transactions are taken at once)."""
        from ..pp.client import PPCoreError

        row = self.store.doc(doc_id)
        svc = self.ctx.pp
        if row is None or svc is None:
            return None
        if row["pp_session"]:
            try:
                svc.client.import_session(svc.cid, row["pp_session"])
                return row["pp_session"]
            except PPCoreError as exc:
                if exc.status != 404:
                    raise
        result = svc.client.import_pdfs(svc.cid, [(row["filename"], self.doc_path(row).read_bytes())],
                                        apply=True, auto_feed=True)
        n, _attention = self._apply_result([row], result)
        if n:
            svc.request_sync()
        return result.get("session")

    def _nc_client(self, row, st: SourceSettings | None = None):
        """(client, None) for the own share link of the source or the owner's Nextcloud, or (None, why not)."""
        if not self.cfg.nextcloud.enabled:
            return None, _("Nextcloud ist in config.toml ausgeschaltet.")
        if row["user_id"] is None or not self.reg.system.may_use(row["user_id"], self.ctx.id):
            return None, _("Der Besitzer des Postfachs hat keinen Zugriff mehr auf dieses Buch – Einstellungen einmal speichern.")
        if st is not None and st.nc_share_url:
            from ..nextcloud import Client, parse_share_link

            try:
                server, token = parse_share_link(st.nc_share_url, self.cfg.nextcloud.allow_http)
                password = self.reg.system.box.decrypt(st.nc_share_secret) if st.nc_share_secret else ""
                return Client.for_share(server, token, password, st.nc_share_legacy), None
            except Exception as exc:  # noqa: BLE001 – key changed, link invalid
                return None, str(exc)
        account = self.reg.system.nextcloud_account(row["user_id"])
        if account is None:
            return None, _("Keine Nextcloud verbunden (Einstellungen → Nextcloud).")
        from ..nextcloud import client_for

        try:
            return client_for(account, self.reg.system.nextcloud_password(account)), None
        except Exception as exc:  # noqa: BLE001 – key changed
            return None, str(exc)

    def nc_folder(self, st: SourceSettings, day: str | None) -> str:
        from ..nextcloud import clean_folder

        folder = clean_folder(st.nc_folder)
        sub = st.nc_subfolder(SourceSettings._day(day or ""))
        return folder.rstrip("/") + "/" + sub if sub else folder

    def _process_nc(self, kind: str, row, st: SourceSettings, done: dict, only=None):
        rows = only if only is not None else self.store.for_nc(kind)
        if not rows:
            return
        if not st.nc_on:
            for r in rows:
                self.store.set_nc(r["id"], "off")
            return
        client, why = self._nc_client(row, st)
        if client is None:
            for r in rows:
                self.store.set_nc(r["id"], "error", message=why)
            done["errors"].append(_("Nextcloud: {a0}", a0=why))
            return
        from ..nextcloud import NextcloudError

        ensured, in_a_row = set(), 0
        for r in rows:  # pending ones first, so documents that keep failing do not hold back new ones
            if self.cancelled(kind):
                done["errors"].append(_("abgebrochen"))
                break
            try:
                data = self.doc_path(r).read_bytes()
            except OSError as exc:
                self.store.set_nc(r["id"], "error", message=_("PDF fehlt: {a0}", a0=exc))
                continue
            try:
                folder = self.nc_folder(st, r["day"])
                if folder not in ensured:
                    client.ensure_folder(folder)
                    ensured.add(folder)
                client.upload_bytes(data, folder, r["filename"], ensure=False, content_type="application/pdf")
                self.store.set_nc(r["id"], "done", path=f"{folder.rstrip('/')}/{r['filename']}")
                done["uploaded"] += 1
                in_a_row = 0
            except NextcloudError as exc:
                self.store.set_nc(r["id"], "error", message=str(exc))
                if not any(str(e).startswith("Nextcloud") for e in done["errors"]):
                    done["errors"].append(_("Nextcloud: {a0}", a0=exc))
                in_a_row += 1
                if in_a_row >= NC_FAILURES_IN_A_ROW:
                    break  # probably unreachable: the rest stays pending/error for the next run

    def upload_one(self, kind: str, doc_id: int) -> tuple[bool, str]:
        row = self.source(kind)
        doc = self.store.doc(doc_id)
        if row is None or doc is None:
            return False, _("Nicht gefunden.")
        st = self.settings_of(row)
        done = {"imported": 0, "attention": 0, "uploaded": 0, "errors": []}
        self._process_nc(kind, row, st, done, only=[doc])
        if done["uploaded"]:
            return True, ""
        return False, (done["errors"][0] if done["errors"] else _("Kein Nextcloud-Ordner eingestellt."))


def _first_message(items) -> str:
    for i in items:
        for m in i.get("messages") or []:
            if m.get("message"):
                return m["message"]
        if i.get("error"):
            return i["error"]
    return ""


def _doc_status(items: list[dict], errors: list[str], needs_target: bool) -> tuple[str, str]:
    """(pp_status, message) of one document from all its items (new securities included) and file errors."""
    its = [i for i in items if i.get("kind") != "SecurityItem"]
    securities = [i for i in items if i.get("kind") == "SecurityItem"]
    n = sum(1 for i in its if i.get("imported"))
    waiting = [i for i in its if not i.get("imported")]
    warnings = [i for i in waiting if i.get("status") == "WARNING"]
    skipped = [i for i in waiting if i.get("status") == "SKIP"]
    target = [i for i in waiting if any(TARGET_MISSING in (m.get("message") or "") for m in i.get("messages") or [])]
    failed = [i for i in waiting if (i.get("status") == "ERROR" or i.get("error")) and i not in target]
    if not its:
        if errors and not any(any(t in e for t in NOT_RECOGNISED) for e in errors):
            return "error", errors[0]
        return "unsupported", _("Portfolio Performance erkennt diese Art von Beleg nicht – nur abgelegt.")
    ready = [i for i in waiting if i.get("status") == "OK"]  # checked fine, but not taken (target missing)
    if n == 0 and (target or (needs_target and ready)):
        return "needs_target", _("Depot und Verrechnungskonto für diese Bank einmal auf der PDF-Import-Seite wählen.")
    if warnings:
        return "review", _first_message(warnings) or _("Mögliches Duplikat – bitte prüfen.")
    if failed:
        return ("partial" if n else "error"), _first_message(failed)
    if n == 0 and skipped and len(skipped) == len(its):
        return "skipped", _first_message(skipped)
    left = [i for i in waiting if i not in skipped]
    if n == 0 or left:
        # OK but not taken, e.g. because its new security could not be created
        reason = _first_message(left + securities) or _("Nicht übernommen.")
        return ("partial" if n else "error"), reason
    return "imported", _("{a0} Buchungen übernommen.", a0=n)
