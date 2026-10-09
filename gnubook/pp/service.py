"""The Portfolio Performance link of one book: settings, pp-core client and background synchronisation."""
from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from ..appdb import now_iso
from ..book import WriteLockError
from ..i18n import _
from .client import PPCoreClient, PPCoreError
from .settings import PPSettings
from .sync import SyncError, SyncResult, book_guid, sync

log = logging.getLogger("gnubook.pp")

RETRY_SECONDS = 300
BUSY_RETRY_SECONDS = 60


class SyncBusy(SyncError):
    """Another synchronisation of the same book is running (web, timer or command line)."""


class PPService:
    """Lives in BookContext.pp when [pp] url is configured."""

    def __init__(self, ctx, cfg, system):
        self.ctx = ctx
        self.cfg = cfg
        self.system = system
        self.client = PPCoreClient(cfg.pp.url, cfg.pp.token, cfg.pp.timeout)
        self._lock = threading.Lock()
        self.worker = SyncWorker(self)

    @property
    def cid(self) -> str:
        return f"book-{self.ctx.id}"

    def settings(self) -> PPSettings:
        return self.system.pp_settings(self.ctx.id)

    @property
    def enabled(self) -> bool:
        return self.settings().enabled

    # ------------------------------------------------------------------ sync
    @contextmanager
    def _exclusive(self, wait: float):
        """One synchronisation per book at a time – also across processes (the web app and the timer's
        `gnubook pp-update` both sync). A run plans against the records of the previous one, so two
        overlapping runs would book the same new transaction twice."""
        def busy():
            return SyncBusy(_("Eine andere Übernahme aus Portfolio Performance läuft gerade – bitte gleich noch einmal."))

        deadline = time.monotonic() + wait
        if not self._lock.acquire(timeout=max(wait, 0.01)):
            raise busy()
        try:
            path = Path(self.cfg.app.data_dir) / f"pp-sync-{self.ctx.id}.lock"
            path.parent.mkdir(parents=True, exist_ok=True)
            # read-only is enough for flock – also when another user (root) created the file
            fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
            try:
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise busy() from None
                        time.sleep(0.25)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            self._lock.release()

    def sync(self, force=(), actor: str = "pp-sync", dry_run: bool = False, wait: float = 30) -> SyncResult:
        """Synchronise now (blocking). Raises PPCoreError, SyncError (SyncBusy), WriteLockError."""
        with self._exclusive(wait):
            settings = self.settings()
            if not settings.enabled:
                raise SyncError(_("Die Übernahme nach GnuCash ist für dieses Buch ausgeschaltet."))
            export = self.client.export(self.cid, prices="all" if settings.prices else "none")
            try:
                result = sync(self.ctx, settings, export, force=force, actor=actor, dry_run=dry_run)
            except Exception as exc:
                if not dry_run and not isinstance(exc, WriteLockError):
                    self.ctx.appdb.pp_add_run(now_iso(), actor, (export.get("client") or {}).get("revision", ""),
                                              "error", str(exc)[:500])
                raise
        return result

    def needs_sync(self) -> bool:
        """True when the PP file or the settings changed since the last run, or the last run was not today
        (the price window moves with the date)."""
        settings = self.settings()
        if not settings.enabled:
            return False
        appdb = self.ctx.appdb
        summary = self.client.summary(self.cid)
        if not summary.get("exists"):
            return False
        last = appdb.meta("pp_last_sync") or ""
        known_book = appdb.meta("pp_book_guid")
        if known_book:
            with self.ctx.book.connect() as conn:
                if book_guid(conn) != known_book:  # book replaced: book everything again
                    return True
        return (summary.get("revision") != appdb.meta("pp_revision")
                or settings.booking_fingerprint() != appdb.meta("pp_settings_fp")
                or last[:10] != date.today().isoformat())

    def request_sync(self, after_quotes: bool = False):
        """After a change of the PP file: book it (when switched on) and copy the file into Nextcloud."""
        if self.enabled:
            self.worker.request(after_quotes=after_quotes)
        if after_quotes:
            threading.Thread(target=self._copy_after_quotes, daemon=True, name=f"pp-copy-{self.ctx.id}").start()
        else:
            self.request_copy()

    def request_copy(self):
        backup = getattr(self.ctx, "backup", None)
        if backup is not None:
            backup.request_extras()

    def _copy_after_quotes(self):
        self.worker.wait_for_quotes()
        self.request_copy()

    def backup_files(self) -> list:
        """[(suffix, content)] of the PP file for the Nextcloud copy (empty without file)."""
        if not self.client.summary(self.cid).get("exists"):
            return []
        data, name = self.client.download(self.cid)
        suffix = "." + name.rsplit(".", 1)[1] if "." in name else ".xml"
        return [(suffix, data)]

    def status(self) -> dict:
        appdb = self.ctx.appdb
        runs = appdb.pp_runs(1)
        return {"last_sync": appdb.meta("pp_last_sync"), "revision": appdb.meta("pp_revision"),
                "last_run": dict(runs[0]) if runs else None, "busy": self.worker.busy,
                "waiting": self.worker.waiting, "error": self.worker.last_error}

    def close(self):
        self.worker.stop()


class SyncWorker:
    """Runs synchronisations in the background (after imports, uploads, price updates) and retries while
    GnuCash Desktop has the book open."""

    def __init__(self, service: PPService):
        self.service = service
        self._event = threading.Event()
        self._stop = False
        self._after_quotes = False
        self.busy = False
        self.waiting: str | None = None  # why a retry is pending
        self.last_error: str | None = None
        self.last_result: SyncResult | None = None
        self._retry_at = 0.0
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"pp-sync-{service.ctx.id}")
        self._thread.start()

    def request(self, after_quotes: bool = False):
        self._after_quotes = self._after_quotes or after_quotes
        self._event.set()

    def stop(self):
        self._stop = True
        self._event.set()

    def wait_for_quotes(self):
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline and not self._stop:
            try:
                st = self.service.client.quotes_status(self.service.cid)
            except PPCoreError:
                return
            if st.get("state") != "running":
                return
            time.sleep(5)

    def _loop(self):
        while not self._stop:
            timeout = None
            if self._retry_at:
                timeout = max(1.0, self._retry_at - time.monotonic())
            fired = self._event.wait(timeout)
            if self._stop:
                return
            self._event.clear()
            if not fired and not self._retry_at:
                continue
            self._retry_at = 0.0
            if self._after_quotes:
                self._after_quotes = False
                self.wait_for_quotes()
            time.sleep(1.0)  # coalesce bursts (several uploads in a row)
            self.busy = True
            try:
                self.last_result = self.service.sync(actor="pp-auto", wait=120)
                self.last_error = None
                self.waiting = None
            except WriteLockError as exc:
                self.waiting = str(exc)
                self._retry_at = time.monotonic() + RETRY_SECONDS
            except SyncBusy as exc:
                self.waiting = str(exc)
                self._retry_at = time.monotonic() + BUSY_RETRY_SECONDS
            except (PPCoreError, SyncError) as exc:
                self.last_error = str(exc)
                self.waiting = None
                log.warning("PP-Synchronisierung (Buch %s): %s", self.service.ctx.id, exc)
            except Exception as exc:  # noqa: BLE001 – keep the worker alive
                self.last_error = str(exc)
                log.exception("PP-Synchronisierung fehlgeschlagen")
            finally:
                self.busy = False
