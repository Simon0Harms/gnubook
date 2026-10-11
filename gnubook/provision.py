"""Create a GnuCash book in PostgreSQL: own role + database, empty book or content of an uploaded file.

Needs [postgres] admin_url: a role with CREATEROLE and CREATEDB (no superuser needed).
"""
from __future__ import annotations

from .i18n import gettext as _

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
        raise ProvisionError(_("Ungültiger Datenbankname: {a0}", a0=dbname))
    if exists(admin_url, dbname):
        raise ProvisionError(_("Datenbank oder Rolle „{a0}“ gibt es schon.", a0=dbname))
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
        raise ProvisionError(_("Ungültiger Datenbankname: {a0}", a0=dbname))
    eng = _admin_engine(admin_url)
    try:
        with eng.connect() as c:
            c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"), {"n": dbname})
            c.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
            c.execute(text(f'DROP ROLE IF EXISTS "{dbname}"'))
    finally:
        eng.dispose()


def list_roles(admin_url: str) -> list[dict]:
    """Login roles the admin role may manage: no superuser, not itself, no pg_* system roles.

    Each entry: name, databases (owned by the role), can_drop (admin role has ADMIN OPTION, PG16+; older
    PostgreSQL: any non-superuser role, since CREATEROLE could drop those).
    """
    eng = _admin_engine(admin_url)
    try:
        with eng.connect() as c:
            new_pg = c.execute(text("SELECT current_setting('server_version_num')::int >= 160000")).scalar()
            rows = c.execute(text(
                "SELECT r.rolname, "
                "  COALESCE((SELECT array_agg(d.datname ORDER BY d.datname) FROM pg_database d "
                "            WHERE d.datdba = r.oid), '{}') AS dbs, "
                "  pg_has_role(current_user, r.oid, 'USAGE') AS member, "
                "  EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles a ON a.oid = m.member "
                "          WHERE m.roleid = r.oid AND a.rolname = current_user AND m.admin_option) AS admin "
                "FROM pg_roles r "
                "WHERE r.rolcanlogin AND NOT r.rolsuper AND r.rolname <> current_user "
                "  AND r.rolname NOT LIKE 'pg\\_%' "
                "ORDER BY r.rolname")).all()
    finally:
        eng.dispose()
    return [{"name": n, "databases": list(dbs or []), "can_drop": bool(adm) if new_pg else True,
             "member": bool(mem)} for n, dbs, mem, adm in rows]


def drop_role(admin_url: str, role: str, with_databases: bool = False) -> list[str]:
    """DROP ROLE, optionally DROP DATABASE for every database the role owns first. Returns dropped databases."""
    roles = {r["name"]: r for r in list_roles(admin_url)}
    if role not in roles:
        raise ProvisionError(_("Rolle „{a0}“ gibt es nicht oder gnubook darf sie nicht verwalten.", a0=role))
    info = roles[role]
    if not info["can_drop"]:
        raise ProvisionError(_("gnubooks Admin-Rolle hat keine ADMIN-Berechtigung für „{a0}“.", a0=role))
    if info["databases"] and not with_databases:
        raise ProvisionError(_("„{a0}“ besitzt noch Datenbanken ({a1}). Zum Löschen „Datenbanken mitlöschen“ ankreuzen.", a0=role, a1=", ".join(info["databases"])))
    eng = _admin_engine(admin_url)
    try:
        with eng.connect() as c:
            def q(name):  # quote_ident: role/database names here come from pg_catalog, not from the form
                return c.execute(text("SELECT quote_ident(:n)"), {"n": name}).scalar()
            if info["databases"] and not info["member"]:
                c.execute(text(f"GRANT {q(role)} TO CURRENT_USER"))  # DROP DATABASE needs ownership (via membership)
            for db in info["databases"]:
                c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"), {"n": db})
                c.execute(text(f"DROP DATABASE IF EXISTS {q(db)}"))
            c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = :n"), {"n": role})
            c.execute(text(f"DROP ROLE {q(role)}"))
    except ProvisionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProvisionError(_("Rolle konnte nicht gelöscht werden: {a0}", a0=getattr(exc, "orig", exc)))
    finally:
        eng.dispose()
    return info["databases"]


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
        raise ProvisionError(_("Die Datei ist keine GnuCash-Datei im SQLite-Format. XML-Dateien bitte in GnuCash Desktop öffnen und mit „Speichern unter → sqlite3“ umwandeln."))
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"books", "accounts", "transactions", "splits", "versions"} <= tables:
            raise ProvisionError(_("Die Datei enthält kein GnuCash-Buch."))
        create_empty_book(url)  # creates the GnuCash tables with PostgreSQL types
        eng = create_engine(url)
        meta = MetaData()
        meta.reflect(bind=eng)
        missing = [t for t in tables if t not in meta.tables and not t.startswith("sqlite_")]
        if missing:
            raise ProvisionError(_("Unbekannte Tabellen in der Datei: ") + ", ".join(sorted(missing)))
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
                    raise ProvisionError(_("Spalten {a0} in Tabelle {a1} passen nicht zum Schema.", a0=unknown, a1=name))
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
