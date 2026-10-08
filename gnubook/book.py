"""Access to the GnuCash SQL book: engine, account index, time handling, GnuCash lock handling.

Reads use plain SQL (SQLAlchemy Core). Writes go through piecash (see writer.py) and are only
allowed while GnuCash Desktop does not hold the book (table ``gnclock``).
"""
from __future__ import annotations

import os
import socket
import threading
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SAWarning

from .i18n import _
from .money import ZERO, gnc_decimal

warnings.filterwarnings("ignore", category=SAWarning)

UTC = timezone.utc

# GnuCash account types
ASSET_TYPES = ("ASSET", "BANK", "CASH", "STOCK", "MUTUAL", "RECEIVABLE")
LIABILITY_TYPES = ("LIABILITY", "CREDIT", "PAYABLE")
BALANCE_SHEET_TYPES = ASSET_TYPES + LIABILITY_TYPES
# accounts whose balance GnuCash shows with reversed sign (default "credit accounts")
REVERSED_TYPES = ("INCOME", "CREDIT", "LIABILITY", "EQUITY", "PAYABLE")
TYPE_LABELS = {
    "ASSET": "Aktiva", "BANK": "Bank", "CASH": "Bargeld", "STOCK": "Aktie", "MUTUAL": "Fonds",
    "RECEIVABLE": "Forderungen", "LIABILITY": "Fremdkapital", "CREDIT": "Kreditkarte",
    "PAYABLE": "Verbindlichkeiten", "INCOME": "Ertrag", "EXPENSE": "Aufwand", "EQUITY": "Eigenkapital",
    "TRADING": "Devisenhandel", "ROOT": "Wurzel",
}
# legally fixed euro conversion rates (used when the price database has no entry)
FIXED_EURO_RATES = {"DEM": Decimal("1.95583"), "ATS": Decimal("13.7603"), "FRF": Decimal("6.55957"),
                    "NLG": Decimal("2.20371"), "BEF": Decimal("40.3399"), "ITL": Decimal("1936.27"),
                    "ESP": Decimal("166.386"), "FIM": Decimal("5.94573"), "IEP": Decimal("0.787564"),
                    "LUF": Decimal("40.3399"), "PTE": Decimal("200.482")}

LOCK_TAG = f"gnubook@{socket.gethostname()}"[:255]


class BookError(RuntimeError):
    pass


class WriteLockError(BookError):
    """GnuCash Desktop (or another program) holds the book."""

    def __init__(self, holders):
        self.holders = holders
        who = ", ".join(f"{h} (PID {p})" for h, p in holders) or _("unbekannt")
        super().__init__(_("Das Buch ist gesperrt – GnuCash Desktop hat es geöffnet: {who}. "
                           "Bitte GnuCash schließen und erneut versuchen.", who=who))


@dataclass
class Commodity:
    guid: str
    namespace: str
    mnemonic: str
    fullname: str
    fraction: int

    @property
    def is_currency(self) -> bool:
        return self.namespace == "CURRENCY"


@dataclass
class Account:
    guid: str
    name: str
    type: str
    commodity_guid: str | None
    commodity_scu: int
    parent_guid: str | None
    code: str
    description: str
    hidden: bool
    placeholder: bool
    full_name: str = ""
    depth: int = 0
    children: list = field(default_factory=list)
    commodity: Commodity | None = None

    @property
    def reversed(self) -> bool:
        return self.type in REVERSED_TYPES

    @property
    def type_label(self) -> str:
        return _(TYPE_LABELS[self.type]) if self.type in TYPE_LABELS else self.type

    @property
    def mnemonic(self) -> str:
        return self.commodity.mnemonic if self.commodity else ""

    @property
    def is_balance_sheet(self) -> bool:
        return self.type in BALANCE_SHEET_TYPES

    def display(self, value: Decimal) -> Decimal:
        """Value in the sign convention GnuCash shows (credit accounts reversed)."""
        return -value if self.reversed else value


class AccountIndex:
    """All accounts of the book (template accounts of scheduled transactions excluded)."""

    def __init__(self, accounts: list[Account], commodities: dict[str, Commodity], root_guid: str,
                 separator: str = ":"):
        self.commodities = commodities
        self.root_guid = root_guid
        self.separator = separator
        self.by_guid: dict[str, Account] = {}
        all_by_guid = {a.guid: a for a in accounts}
        for a in accounts:
            a.commodity = commodities.get(a.commodity_guid) if a.commodity_guid else None
        # walk from the real root so template-root subtrees are excluded
        for a in accounts:
            if a.parent_guid in all_by_guid:
                all_by_guid[a.parent_guid].children.append(a)
        for a in accounts:
            a.children.sort(key=lambda c: c.name.casefold())
        self.root = all_by_guid.get(root_guid)
        if self.root is None:
            raise BookError("Wurzelkonto des Buchs nicht gefunden")
        stack = [(c, 0, "") for c in reversed(self.root.children)]
        while stack:
            acc, depth, prefix = stack.pop()
            acc.depth = depth
            acc.full_name = f"{prefix}{separator}{acc.name}" if prefix else acc.name
            self.by_guid[acc.guid] = acc
            for child in reversed(acc.children):
                stack.append((child, depth + 1, acc.full_name))
        self.by_full_name = {a.full_name: a for a in self.by_guid.values()}

    def __contains__(self, guid):
        return guid in self.by_guid

    def get(self, guid) -> Account | None:
        return self.by_guid.get(guid)

    def find(self, ref: str) -> Account | None:
        """Account by GUID or full name."""
        if not ref:
            return None
        return self.by_guid.get(ref) or self.by_full_name.get(ref)

    def top_level(self) -> list[Account]:
        return list(self.root.children)

    def walk(self, accounts=None):
        """Depth-first, sorted by name."""
        for acc in (accounts if accounts is not None else self.root.children):
            yield acc
            yield from self.walk(acc.children)

    def descendants(self, acc: Account):
        for child in acc.children:
            yield child
            yield from self.descendants(child)

    def postable(self, currency_guid: str | None = None) -> list[Account]:
        """Accounts that can receive splits (optionally only those in the given currency)."""
        out = []
        for acc in self.walk():
            if acc.placeholder or acc.type in ("ROOT",):
                continue
            if currency_guid and acc.commodity_guid != currency_guid:
                continue
            out.append(acc)
        return out


class Book:
    """Connection to one GnuCash SQL book."""

    def __init__(self, url: str, tz: str = "Europe/Berlin", separator: str = ":"):
        if not url:
            raise BookError("Keine Buch-URL konfiguriert ([book] url)")
        self.url = url
        self.tz = ZoneInfo(tz)
        self.separator = separator
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 20}
        else:
            kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800)
        self.engine = create_engine(url, **kwargs)
        self.is_sqlite = self.engine.dialect.name == "sqlite"
        if self.is_sqlite:
            @event.listens_for(self.engine, "connect")
            def _sqlite_connect(dbapi_conn, _record):
                # Unicode-aware lower() for case-insensitive search (SQLite's own only folds ASCII)
                dbapi_conn.create_function("lower", 1, lambda s: s.lower() if isinstance(s, str) else s,
                                           deterministic=True)
        self._write_mutex = threading.Lock()
        self.after_write = []  # callbacks run after every successful write
        self._schema_ok: bool | None = None
        self._schema_checked = 0.0

    # ------------------------------------------------------------------ basics
    @contextmanager
    def connect(self):
        with self.engine.connect() as conn:
            yield conn

    def dispose(self):
        self.engine.dispose()

    def schema_info(self) -> dict:
        with self.connect() as conn:
            rows = conn.execute(text("SELECT table_name, table_version FROM versions")).fetchall()
        versions = {r[0]: int(r[1]) for r in rows}
        from piecash.core.session import version_supported

        tables = {k: v for k, v in versions.items() if "Gnucash" not in k}
        supported = any(tables == {k: v for k, v in vt.items() if "Gnucash" not in k}
                        for vt in version_supported.values())
        gv = versions.get("Gnucash", 0)
        if not gv:
            gnucash = "?"
        elif gv >= 3000000:  # since 3.0: major * 1000000 + minor
            gnucash = f"{gv // 1000000}.{gv % 1000000}"
        else:                # 2.x: major * 1000000 + minor * 10000 + micro * 100
            gnucash = f"{gv // 1000000}.{gv // 10000 % 100}.{gv // 100 % 100}"
        return {"gnucash": gnucash, "supported": supported, "versions": versions}

    def load_accounts(self, conn=None) -> AccountIndex:
        def _load(c):
            book = c.execute(text("SELECT root_account_guid FROM books")).fetchone()
            if book is None:
                raise BookError("Tabelle books ist leer – ist das ein GnuCash-Buch?")
            commodities = {r[0]: Commodity(r[0], r[1], r[2], r[3] or r[2], int(r[4] or 100))
                           for r in c.execute(text(
                               "SELECT guid, namespace, mnemonic, fullname, fraction FROM commodities"))}
            accounts = [Account(r[0], r[1], r[2], r[3], int(r[4] or 100), r[5], r[6] or "", r[7] or "",
                                bool(r[8]), bool(r[9]))
                        for r in c.execute(text(
                            "SELECT guid, name, account_type, commodity_guid, commodity_scu, parent_guid, "
                            "code, description, hidden, placeholder FROM accounts"))]
            return AccountIndex(accounts, commodities, book[0], self.separator)

        if conn is not None:
            return _load(conn)
        with self.connect() as c:
            return _load(c)

    # ------------------------------------------------------------------ time
    def to_utc_naive(self, value) -> datetime | None:
        """post_date/enter_date from the DB as naive UTC datetime (SQLite returns text)."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo is None else value.astimezone(UTC).replace(tzinfo=None)
        s = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        raise BookError(f"Unbekanntes Datumsformat in der Datenbank: {s!r}")

    def day_of(self, value) -> date | None:
        """Local calendar day of a GnuCash timestamp (as GnuCash shows it)."""
        dt = self.to_utc_naive(value)
        if dt is None:
            return None
        return dt.replace(tzinfo=UTC).astimezone(self.tz).date()

    def local_datetime(self, value) -> datetime | None:
        dt = self.to_utc_naive(value)
        return dt.replace(tzinfo=UTC).astimezone(self.tz) if dt else None

    def day_start_utc(self, d: date) -> str:
        """UTC timestamp string of local midnight starting day d (for SQL range filters)."""
        local = datetime.combine(d, dtime(0, 0), tzinfo=self.tz)
        return local.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")

    def day_end_utc(self, d: date) -> str:
        """Exclusive upper bound: local midnight after day d."""
        return self.day_start_utc(d + timedelta(days=1))

    def today(self) -> date:
        return datetime.now(self.tz).date()

    # ------------------------------------------------------------------ GnuCash lock
    def lock_holders(self, conn=None) -> list[tuple[str, int]]:
        def _q(c):
            return [(r[0] or "", int(r[1] or 0)) for r in c.execute(text("SELECT hostname, pid FROM gnclock"))]
        if conn is not None:
            return _q(conn)
        with self.connect() as c:
            return _q(c)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _cleanup_own_stale(self, conn):
        for host, pid in self.lock_holders(conn):
            if host == LOCK_TAG and not self._pid_alive(pid):
                conn.execute(text("DELETE FROM gnclock WHERE hostname = :h AND pid = :p"), {"h": host, "p": pid})

    def foreign_lock_holders(self) -> list[tuple[str, int]]:
        with self.engine.begin() as conn:
            self._cleanup_own_stale(conn)
            return [(h, p) for h, p in self.lock_holders(conn) if h != LOCK_TAG]

    def is_locked(self) -> bool:
        return bool(self.foreign_lock_holders())

    def remove_foreign_locks(self) -> int:
        """Admin action: delete stale GnuCash lock rows. Only safe when GnuCash Desktop is really closed."""
        with self.engine.begin() as conn:
            res = conn.execute(text("DELETE FROM gnclock WHERE hostname <> :h"), {"h": LOCK_TAG})
            return res.rowcount or 0

    def ensure_writable_schema(self):
        now = time.monotonic()
        if self._schema_ok is None or now - self._schema_checked > 600:
            self._schema_ok = bool(self.schema_info()["supported"])
            self._schema_checked = now
        if not self._schema_ok:
            raise BookError(_("Unbekannte GnuCash-Datenbankversion – gnubook schreibt aus Sicherheitsgründen nicht. "
                              "Bitte gnubook aktualisieren."))

    @contextmanager
    def exclusive(self, wait_seconds: float = 8.0):
        """Hold the book for one write: process mutex + own row in gnclock.

        Raises WriteLockError when GnuCash Desktop has the book open.
        """
        self.ensure_writable_schema()
        pid = os.getpid()
        if not self._write_mutex.acquire(timeout=wait_seconds):
            raise BookError(_("Eine andere Schreiboperation läuft noch – bitte erneut versuchen."))
        try:
            deadline = time.monotonic() + wait_seconds
            while True:
                with self.engine.begin() as conn:
                    self._cleanup_own_stale(conn)
                    holders = self.lock_holders(conn)
                    foreign = [(h, p) for h, p in holders if h != LOCK_TAG]
                    if foreign:
                        raise WriteLockError(foreign)
                    busy = [(h, p) for h, p in holders if h == LOCK_TAG and p != pid]
                    if not busy:
                        conn.execute(text("INSERT INTO gnclock (hostname, pid) VALUES (:h, :p)"),
                                     {"h": LOCK_TAG, "p": pid})
                if not busy:
                    break
                if time.monotonic() > deadline:  # another gnubook worker is writing
                    raise BookError(_("Das Buch wird gerade von einem anderen gnubook-Prozess geschrieben."))
                time.sleep(0.2)
            # re-check: GnuCash may have opened the book between our check and insert
            foreign = [(h, p) for h, p in self.lock_holders() if h != LOCK_TAG]
            if foreign:
                self._release(pid)
                raise WriteLockError(foreign)
            try:
                yield
            finally:
                self._release(pid)
            for cb in self.after_write:
                cb()
        finally:
            self._write_mutex.release()

    def _release(self, pid: int):
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM gnclock WHERE hostname = :h AND pid = :p"), {"h": LOCK_TAG, "p": pid})

    # ------------------------------------------------------------------ piecash
    def piecash_book(self):
        """A piecash Book bound to our engine (call only inside exclusive())."""
        import piecash  # noqa: F401  # pyflakes: registers all mapped piecash classes
        from piecash.core.book import Book as PcBook
        from piecash.core.session import adapt_session
        from piecash.sa_extra import Session as PcSession

        session = PcSession(bind=self.engine)
        pc_book = session.query(PcBook).one()
        adapt_session(session, book=pc_book, readonly=False)
        return pc_book


def convert(value: Decimal, from_cdty: Commodity | None, to_cdty: Commodity | None, prices: dict | None = None):
    """Convert an amount between commodities; returns None when no rate is known."""
    if from_cdty is None or to_cdty is None or from_cdty.guid == to_cdty.guid:
        return value
    if prices:
        rate = prices.get((from_cdty.guid, to_cdty.guid))
        if rate is not None:
            return value * rate
        inv = prices.get((to_cdty.guid, from_cdty.guid))
        if inv:
            return value / inv
    if to_cdty.mnemonic == "EUR" and from_cdty.mnemonic in FIXED_EURO_RATES:
        return value / FIXED_EURO_RATES[from_cdty.mnemonic]
    if from_cdty.mnemonic == "EUR" and to_cdty.mnemonic in FIXED_EURO_RATES:
        return value * FIXED_EURO_RATES[to_cdty.mnemonic]
    return None


def latest_prices(conn) -> dict:
    """(commodity_guid, currency_guid) -> latest price."""
    out, seen = {}, {}
    for cg, cug, d, num, den in conn.execute(text(
            "SELECT commodity_guid, currency_guid, date, value_num, value_denom FROM prices")):
        key = (cg, cug)
        ds = str(d)
        if key not in seen or ds > seen[key]:
            seen[key] = ds
            out[key] = gnc_decimal(num, den)
    return out


__all__ = ["Account", "AccountIndex", "Book", "BookError", "Commodity", "WriteLockError", "ZERO", "convert",
           "latest_prices", "ASSET_TYPES", "LIABILITY_TYPES", "BALANCE_SHEET_TYPES", "LOCK_TAG", "UTC"]
