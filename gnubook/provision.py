"""Create a GnuCash book in PostgreSQL: own role + database, empty book or content of an uploaded file.

Needs [postgres] admin_url: a role with CREATEROLE and CREATEDB (no superuser needed).
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime
from unittest import mock

from sqlalchemy import MetaData, create_engine, text, types
from sqlalchemy.engine import make_url

from .system import slug

NAME_RE = re.compile(r"[a-z][a-z0-9_]{1,40}")


class ProvisionError(RuntimeError):
    pass


def db_name_for(name: str, prefix: str = "gnucash_") -> str:
    return (prefix + slug(name).replace("-", "_"))[:48]


def _admin_engine(admin_url: str):
    return create_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)


def exists(admin_url: str, dbname: str) -> bool:
    eng = _admin_engine(admin_url)
    try:
        with eng.connect() as c:
            db = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname}).scalar()
            role = c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": dbname}).scalar()
            return bool(db or role)
    finally:
        eng.dispose()


def create_role_and_db(admin_url: str, dbname: str) -> tuple[str, str]:
    """CREATE ROLE <dbname> LOGIN + CREATE DATABASE <dbname> OWNER <dbname>. Returns (user URL, password)."""
    if not NAME_RE.fullmatch(dbname):
        raise ProvisionError(f"Ungültiger Datenbankname: {dbname}")
    if exists(admin_url, dbname):
        raise ProvisionError(f"Datenbank oder Rolle „{dbname}“ gibt es schon.")
    password = secrets.token_urlsafe(24)
    eng = _admin_engine(admin_url)
    try:
        with eng.connect() as c:
            # identifiers are checked by NAME_RE; the password is passed as a literal via format()
            pw = c.execute(text("SELECT quote_literal(:p)"), {"p": password}).scalar()
            c.execute(text(f'CREATE ROLE "{dbname}" LOGIN PASSWORD {pw}'))
            try:
                c.execute(text(f'GRANT "{dbname}" TO CURRENT_USER'))  # needed to create a db owned by it (PG16+)
            except Exception:  # noqa: BLE001  (already member / older PostgreSQL)
                pass
            c.execute(text(f'CREATE DATABASE "{dbname}" OWNER "{dbname}" ENCODING \'UTF8\' TEMPLATE template0'))
            c.execute(text(f'REVOKE ALL ON DATABASE "{dbname}" FROM PUBLIC'))
    finally:
        eng.dispose()
    url = make_url(admin_url).set(username=dbname, password=password, database=dbname)
    return url.render_as_string(hide_password=False), password


def drop_role_and_db(admin_url: str, dbname: str):
    if not NAME_RE.fullmatch(dbname):
        raise ProvisionError(f"Ungültiger Datenbankname: {dbname}")
    eng = _admin_engine(admin_url)
    try:
        with eng.connect() as c:
            c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"), {"n": dbname})
            c.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
            c.execute(text(f'DROP ROLE IF EXISTS "{dbname}"'))
    finally:
        eng.dispose()


def create_empty_book(url: str, currency: str = "EUR", template: str = "none"):
    """GnuCash schema + root accounts in an existing, empty database (as the owner role)."""
    import piecash
    import sqlalchemy_utils.functions as suf

    # piecash.create_book wants to create the database itself; ours exists already and is empty
    with mock.patch.object(suf, "database_exists", lambda _u: False), \
            mock.patch.object(suf, "create_database", lambda *_a, **_k: None):
        book = piecash.create_book(uri_conn=url, currency=currency)
    try:
        if template == "simple":
            _simple_chart(book)
            book.save()
    finally:
        eng = book.session.bind
        book.close()
        eng.dispose()


# a small German chart of accounts: (path, type, placeholder)
SIMPLE_CHART = [
    ("Aktiva", "ASSET", True), ("Aktiva:Barvermögen", "ASSET", True), ("Aktiva:Barvermögen:Girokonto", "BANK", False),
    ("Aktiva:Barvermögen:Sparkonto", "BANK", False), ("Aktiva:Barvermögen:Bargeld", "CASH", False),
    ("Fremdkapital", "LIABILITY", True), ("Fremdkapital:Kreditkarte", "CREDIT", False),
    ("Erträge", "INCOME", True), ("Erträge:Gehalt", "INCOME", False), ("Erträge:Zinsen", "INCOME", False),
    ("Erträge:Sonstige Erträge", "INCOME", False),
    ("Aufwendungen", "EXPENSE", True), ("Aufwendungen:Wohnen", "EXPENSE", True),
    ("Aufwendungen:Wohnen:Miete", "EXPENSE", False), ("Aufwendungen:Wohnen:Strom", "EXPENSE", False),
    ("Aufwendungen:Wohnen:Internet und Telefon", "EXPENSE", False), ("Aufwendungen:Lebensmittel", "EXPENSE", False),
    ("Aufwendungen:Haushalt", "EXPENSE", False), ("Aufwendungen:Versicherungen", "EXPENSE", False),
    ("Aufwendungen:Mobilität", "EXPENSE", False), ("Aufwendungen:Freizeit", "EXPENSE", False),
    ("Aufwendungen:Gesundheit", "EXPENSE", False), ("Aufwendungen:Kleidung", "EXPENSE", False),
    ("Aufwendungen:Bankgebühren", "EXPENSE", False), ("Aufwendungen:Sonstiges", "EXPENSE", False),
    ("Eigenkapital", "EQUITY", True), ("Eigenkapital:Anfangsbestand", "EQUITY", False),
]


def _simple_chart(book):
    from piecash import Account

    cur = book.default_currency
    made = {}
    for path, typ, ph in SIMPLE_CHART:
        parent_path, _, name = path.rpartition(":")
        parent = made[parent_path] if parent_path else book.root_account
        made[path] = Account(name=name, type=typ, parent=parent, commodity=cur, placeholder=int(ph))
    Account(name=f"Ausgleichskonto-{cur.mnemonic}", type="BANK", parent=book.root_account, commodity=cur)


def import_sqlite_book(sqlite_path: str, url: str):
    """Copy a GnuCash SQLite file (.gnucash in SQL format) into the empty database at url."""
    with open(sqlite_path, "rb") as fh:
        head = fh.read(16)
    if head != b"SQLite format 3\x00":
        raise ProvisionError("Die Datei ist keine GnuCash-Datei im SQLite-Format. XML-Dateien bitte in GnuCash "
                             "Desktop öffnen und mit „Speichern unter → sqlite3“ umwandeln.")
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"books", "accounts", "transactions", "splits", "versions"} <= tables:
            raise ProvisionError("Die Datei enthält kein GnuCash-Buch.")
        create_empty_book(url)  # creates the GnuCash tables with PostgreSQL types
        eng = create_engine(url)
        meta = MetaData()
        meta.reflect(bind=eng)
        missing = [t for t in tables if t not in meta.tables and not t.startswith("sqlite_")]
        if missing:
            raise ProvisionError("Unbekannte Tabellen in der Datei: " + ", ".join(sorted(missing)))
        with eng.begin() as conn:
            for t in meta.sorted_tables:
                conn.execute(t.delete())
            for name in tables:
                if name.startswith("sqlite_") or name == "gnclock":
                    continue
                table = meta.tables[name]
                cols = [r[1] for r in src.execute(f'PRAGMA table_info("{name}")')]
                unknown = [c for c in cols if c not in table.c]
                if unknown:
                    raise ProvisionError(f"Spalten {unknown} in Tabelle {name} passen nicht zum Schema.")
                conv = []
                for c in cols:
                    t = table.c[c].type
                    conv.append(_to_timestamp if isinstance(t, types.DateTime) else
                                _to_date if isinstance(t, types.Date) else None)
                rows = []
                for r in src.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{name}"'):
                    rows.append({c: (f(v) if f and v is not None else v) for c, v, f in zip(cols, r, conv)})
                if rows:
                    conn.execute(table.insert(), rows)
            # keep the id sequence of slots behind the copied ids
            if "slots" in meta.tables:
                conn.execute(text("SELECT setval(pg_get_serial_sequence('slots', 'id'), "
                                  "GREATEST((SELECT COALESCE(MAX(id), 0) FROM slots), 1))"))
        eng.dispose()
    finally:
        src.close()


def _to_timestamp(v):
    s = str(v)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ProvisionError(f"Unbekanntes Datumsformat: {s!r}")


def _to_date(v):
    s = str(v)
    return datetime.strptime(s, "%Y%m%d").date() if len(s) == 8 else datetime.strptime(s[:10], "%Y-%m-%d").date()
