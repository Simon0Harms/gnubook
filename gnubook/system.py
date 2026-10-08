"""Users, books and API tokens (data_dir/system.sqlite) and the per-book runtime contexts.

Every book is a GnuCash database of its own (normally one PostgreSQL database per book). Users and books
are related n:m. Each book keeps its gnubook data (API ids, import records, accepted checkpoint differences,
audit log) in data_dir/books/<id>.sqlite.
"""
from __future__ import annotations

from .i18n import gettext as _

import hashlib
import re
import secrets
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from .appdb import AppDB, now_iso
from .book import Book
from .config import Config
from .banks import get_profile
from .importer import Importer

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Europe/Berlin', backup_file TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, managed_db TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS user_books (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, book_id)
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    token_sha256 TEXT NOT NULL UNIQUE, label TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS login_failures (ip TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nextcloud_accounts (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    server TEXT NOT NULL, login TEXT NOT NULL, app_password TEXT NOT NULL, dav_user TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'account', legacy INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nextcloud_targets (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    folder TEXT NOT NULL, filename TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, book_id)
);
"""

MIN_PASSWORD = 10


class UserError(ValueError):
    pass


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
               .replace("ß", "ss")).strip("-")
    return s or "buch"


def mask_url(url: str) -> str:
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1***@", url)


class SystemDB:
    def __init__(self, path: Path, box=None):
        self.path = Path(path)
        self.box = box  # crypto.SecretBox for stored Nextcloud passwords
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            cols = {r[1] for r in c.execute("PRAGMA table_info(books)")}
            if "managed_db" not in cols:  # 0.2.0 databases
                c.execute("ALTER TABLE books ADD COLUMN managed_db TEXT NOT NULL DEFAULT ''")
            if "profile" not in cols:  # before 0.4.0
                c.execute("ALTER TABLE books ADD COLUMN profile TEXT NOT NULL DEFAULT 'de'")
                c.execute("ALTER TABLE books ADD COLUMN import_settings TEXT NOT NULL DEFAULT '{}'")
            nc_cols = {r[1] for r in c.execute("PRAGMA table_info(nextcloud_accounts)")}
            if "kind" not in nc_cols:  # first Nextcloud version (account only)
                c.execute("ALTER TABLE nextcloud_accounts ADD COLUMN kind TEXT NOT NULL DEFAULT 'account'")
                c.execute("ALTER TABLE nextcloud_accounts ADD COLUMN legacy INTEGER NOT NULL DEFAULT 0")
            if "language" not in {r[1] for r in c.execute("PRAGMA table_info(users)")}:
                c.execute("ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT ''")
        try:
            self.path.chmod(0o600)  # contains database URLs with passwords
        except OSError:
            pass

    @contextmanager
    def conn(self):
        with self._lock:
            c = sqlite3.connect(self.path, timeout=15)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    # ------------------------------------------------------------------ users
    def users(self):
        with self.conn() as c:
            return c.execute("SELECT * FROM users ORDER BY username").fetchall()

    def user(self, user_id):
        with self.conn() as c:
            return c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def user_by_name(self, username):
        with self.conn() as c:
            return c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    def authenticate(self, username: str, password: str):
        u = self.user_by_name(username)
        if u is None:
            check_password_hash(generate_password_hash("x"), password)  # similar timing
            return None
        if not u["active"] or not check_password_hash(u["password_hash"], password):
            return None
        return u

    @staticmethod
    def _check_password(password: str):
        if len(password or "") < MIN_PASSWORD:
            raise UserError(_("Das Passwort muss mindestens {a0} Zeichen haben.", a0=MIN_PASSWORD))

    def add_user(self, username: str, password: str | None = None, is_admin: bool = False,
                 password_hash: str | None = None) -> int:
        username = (username or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._@-]{2,64}", username):
            raise UserError(_("Benutzername: 2–64 Zeichen, nur Buchstaben, Ziffern und . _ @ -"))
        if password_hash is None:
            self._check_password(password)
            password_hash = generate_password_hash(password)
        try:
            with self.conn() as c:
                return int(c.execute("INSERT INTO users (username, password_hash, is_admin, created_at) "
                                     "VALUES (?, ?, ?, ?)", (username, password_hash, int(is_admin),
                                                            now_iso())).lastrowid)
        except sqlite3.IntegrityError:
            raise UserError(_("Benutzer „{a0}“ gibt es schon.", a0=username))

    def set_password(self, user_id: int, password: str):
        self._check_password(password)
        with self.conn() as c:
            c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user_id))

    def set_language(self, user_id: int, language: str):
        from .i18n import LANGUAGES

        with self.conn() as c:
            c.execute("UPDATE users SET language = ? WHERE id = ?",
                      (language if language in LANGUAGES else "", user_id))

    def update_user(self, user_id: int, is_admin: bool, active: bool):
        with self.conn() as c:
            if not is_admin or not active:
                others = c.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1 AND active = 1 AND id <> ?",
                                   (user_id,)).fetchone()[0]
                if others == 0:
                    raise UserError("Es muss mindestens ein aktiver Administrator bleiben.")
            c.execute("UPDATE users SET is_admin = ?, active = ? WHERE id = ?", (int(is_admin), int(active), user_id))

    def delete_user(self, user_id: int):
        self.update_user(user_id, False, False)  # raises when it is the last admin
        with self.conn() as c:
            c.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # ------------------------------------------------------------------ books
    def books(self):
        with self.conn() as c:
            return c.execute("SELECT * FROM books ORDER BY name").fetchall()

    def book(self, book_id):
        with self.conn() as c:
            return c.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()

    def add_book(self, name: str, url: str, timezone: str = "Europe/Berlin", backup_file: str = "",
                 managed_db: str = "") -> int:
        if not name.strip() or not url.strip():
            raise UserError(_("Name und Datenbank-URL sind nötig."))
        with self.conn() as c:
            if c.execute("SELECT 1 FROM books WHERE name = ?", (name.strip(),)).fetchone():
                raise UserError(_("Ein Buch „{a0}“ gibt es schon.", a0=name.strip()))
            return int(c.execute("INSERT INTO books (name, url, timezone, backup_file, created_at, managed_db) "
                                 "VALUES (?, ?, ?, ?, ?, ?)", (name.strip(), url.strip(), timezone, backup_file,
                                                               now_iso(), managed_db)).lastrowid)

    def set_book_import(self, book_id: int, profile: str, settings: dict):
        """Bank profile and import settings of one book (override [import] in config.toml)."""
        import json

        from .banks import profile_names

        if profile not in profile_names():
            raise UserError(f"Unbekanntes Bankprofil: {profile}")
        with self.conn() as c:
            c.execute("UPDATE books SET profile = ?, import_settings = ? WHERE id = ?",
                      (profile, json.dumps(settings, ensure_ascii=False, sort_keys=True), book_id))

    def update_book(self, book_id: int, name: str, url: str, timezone: str, backup_file: str):
        with self.conn() as c:
            c.execute("UPDATE books SET name = ?, url = ?, timezone = ?, backup_file = ? WHERE id = ?",
                      (name.strip(), url.strip(), timezone.strip() or "Europe/Berlin", backup_file.strip(), book_id))

    def delete_book(self, book_id: int):
        """Only removes the connection in gnubook – the GnuCash database itself is not touched."""
        with self.conn() as c:
            c.execute("DELETE FROM books WHERE id = ?", (book_id,))

    def user_books(self, user_id):
        with self.conn() as c:
            return c.execute("SELECT b.* FROM books b JOIN user_books ub ON ub.book_id = b.id "
                             "WHERE ub.user_id = ? ORDER BY b.name", (user_id,)).fetchall()

    def book_users(self, book_id):
        with self.conn() as c:
            return [r["user_id"] for r in c.execute("SELECT user_id FROM user_books WHERE book_id = ?", (book_id,))]

    def set_book_users(self, book_id: int, user_ids):
        with self.conn() as c:
            c.execute("DELETE FROM user_books WHERE book_id = ?", (book_id,))
            c.executemany("INSERT INTO user_books (user_id, book_id) VALUES (?, ?)",
                          [(int(u), book_id) for u in set(user_ids)])

    def grant(self, user_id: int, book_id: int):
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO user_books (user_id, book_id) VALUES (?, ?)", (user_id, book_id))

    def may_use(self, user_id, book_id) -> bool:
        with self.conn() as c:
            return c.execute("SELECT 1 FROM user_books ub JOIN users u ON u.id = ub.user_id WHERE ub.user_id = ? "
                             "AND ub.book_id = ? AND u.active = 1", (user_id, book_id)).fetchone() is not None

    # ------------------------------------------------------------------ API tokens
    def create_token(self, user_id: int, book_id: int, label: str = "") -> str:
        token = secrets.token_urlsafe(40)
        with self.conn() as c:
            c.execute("INSERT INTO api_tokens (user_id, book_id, token_sha256, label, created_at) VALUES (?, ?, ?, ?, ?)",
                      (user_id, book_id, hashlib.sha256(token.encode()).hexdigest(), label, now_iso()))
        return token

    def add_token_hash(self, user_id: int, book_id: int, sha256: str, label: str):
        with self.conn() as c:
            c.execute("INSERT OR IGNORE INTO api_tokens (user_id, book_id, token_sha256, label, created_at) "
                      "VALUES (?, ?, ?, ?, ?)", (user_id, book_id, sha256.strip().lower(), label, now_iso()))

    def tokens(self, book_id: int):
        with self.conn() as c:
            return c.execute("SELECT t.*, u.username FROM api_tokens t JOIN users u ON u.id = t.user_id "
                             "WHERE t.book_id = ? ORDER BY t.id", (book_id,)).fetchall()

    def delete_token(self, token_id: int, book_id: int):
        with self.conn() as c:
            c.execute("DELETE FROM api_tokens WHERE id = ? AND book_id = ?", (token_id, book_id))

    def token_lookup(self, token: str):
        """(user row, book id) for a valid token whose user still may use the book, else None."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.conn() as c:
            row = c.execute("SELECT * FROM api_tokens WHERE token_sha256 = ?", (digest,)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now_iso(), row["id"]))
        if not self.may_use(row["user_id"], row["book_id"]):
            return None
        return self.user(row["user_id"]), row["book_id"]

    # ------------------------------------------------------------------ Nextcloud (per user)
    def nextcloud_account(self, user_id):
        with self.conn() as c:
            return c.execute("SELECT * FROM nextcloud_accounts WHERE user_id = ?", (user_id,)).fetchone()

    def set_nextcloud_account(self, user_id: int, server: str, login: str, app_password: str, dav_user: str,
                              display_name: str = "", kind: str = "account", legacy: bool = False):
        """Stores a connection; the password is encrypted (crypto.SecretBox). kind: 'account' or 'share'."""
        secret = self.box.encrypt(app_password)
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO nextcloud_accounts (user_id, server, login, app_password, dav_user, "
                      "display_name, created_at, kind, legacy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (user_id, server, login, secret, dav_user, display_name, now_iso(), kind, int(legacy)))

    def nextcloud_password(self, row) -> str:
        return self.box.decrypt(row["app_password"])

    def encrypt_nextcloud_passwords(self) -> int:
        """Encrypts passwords stored before encryption existed. Returns the number of rows changed."""
        if self.box is None or not self.box.available:
            return 0
        with self.conn() as c:
            rows = c.execute("SELECT user_id, app_password FROM nextcloud_accounts").fetchall()
            plain = [r for r in rows if not self.box.is_encrypted(r["app_password"])]
            c.executemany("UPDATE nextcloud_accounts SET app_password = ? WHERE user_id = ?",
                          [(self.box.encrypt(r["app_password"]), r["user_id"]) for r in plain])
        return len(plain)

    def delete_nextcloud_account(self, user_id: int):
        with self.conn() as c:
            c.execute("DELETE FROM nextcloud_targets WHERE user_id = ?", (user_id,))
            c.execute("DELETE FROM nextcloud_accounts WHERE user_id = ?", (user_id,))

    def nextcloud_target(self, user_id, book_id):
        with self.conn() as c:
            return c.execute("SELECT * FROM nextcloud_targets WHERE user_id = ? AND book_id = ?",
                             (user_id, book_id)).fetchone()

    def set_nextcloud_target(self, user_id: int, book_id: int, folder: str, filename: str, enabled: bool = True):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO nextcloud_targets (user_id, book_id, folder, filename, enabled) "
                      "VALUES (?, ?, ?, ?, ?)", (user_id, book_id, folder, filename, int(enabled)))

    def delete_nextcloud_target(self, user_id: int, book_id: int):
        with self.conn() as c:
            c.execute("DELETE FROM nextcloud_targets WHERE user_id = ? AND book_id = ?", (user_id, book_id))

    def nextcloud_uploads(self, book_id: int):
        """Active targets of a book whose user still may use it, joined with the user's account."""
        with self.conn() as c:
            return c.execute(
                "SELECT t.user_id, t.folder, t.filename, a.server, a.login, a.app_password, a.dav_user, a.kind, "
                "a.legacy, u.username "
                "FROM nextcloud_targets t JOIN nextcloud_accounts a ON a.user_id = t.user_id "
                "JOIN users u ON u.id = t.user_id JOIN user_books ub ON ub.user_id = t.user_id AND ub.book_id = t.book_id "
                "WHERE t.book_id = ? AND t.enabled = 1 AND u.active = 1 ORDER BY t.user_id", (book_id,)).fetchall()

    # ------------------------------------------------------------------ login throttle
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


@dataclass
class BookContext:
    """Everything one request needs for one book (what views call `state()`)."""
    id: int
    name: str
    cfg: Config
    book: Book
    appdb: AppDB
    importer: Importer
    backup: object = None


class Registry:
    """Creates and caches one BookContext (engine, app db, importer, backup writer) per book."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.data = Path(cfg.app.data_dir)
        from .crypto import SecretBox

        self.box = SecretBox(cfg.nextcloud.encryption_key or cfg.app.secret_key)
        self.system = SystemDB(self.data / "system.sqlite", self.box)
        self.system.encrypt_nextcloud_passwords()
        self._contexts: dict[int, tuple[tuple, BookContext]] = {}
        self._lock = threading.Lock()
        self.bootstrap()

    def bootstrap(self):
        """Take over a single-user configuration ([app] username/password_hash, [book] url, [api] token)."""
        cfg = self.cfg
        if not self.system.users() and cfg.app.password_hash:
            uid = self.system.add_user(cfg.app.username, is_admin=True, password_hash=cfg.app.password_hash)
        else:
            uid = None
        if not self.system.books() and cfg.book.url:
            bid = self.system.add_book("Hauptbuch", cfg.book.url, cfg.book.timezone, cfg.backup.gnucash_file)
            old = self.data / "gnubook.sqlite"
            new = self.data / "books" / f"{bid}.sqlite"
            if old.exists() and not new.exists():
                new.parent.mkdir(parents=True, exist_ok=True)
                for suffix in ("", "-wal", "-shm"):
                    if Path(str(old) + suffix).exists():
                        shutil.move(str(old) + suffix, str(new) + suffix)
            if uid is None:
                admins = [u for u in self.system.users() if u["is_admin"]]
                uid = admins[0]["id"] if admins else None
            if uid is not None:
                self.system.grant(uid, bid)
                if cfg.api.token_sha256:
                    self.system.add_token_hash(uid, bid, cfg.api.token_sha256, "aus config.toml")

    def context(self, book_id: int) -> BookContext | None:
        row = self.system.book(book_id)
        if row is None:
            return None
        key = (row["name"], row["url"], row["timezone"], row["backup_file"], row["profile"], row["import_settings"])
        with self._lock:
            cached = self._contexts.get(book_id)
            if cached and cached[0] == key:
                return cached[1]
            if cached:  # settings changed
                cached[1].book.dispose()
            book = Book(row["url"], row["timezone"], self.cfg.book.account_separator)
            appdb = AppDB(self.data / "books" / f"{book_id}.sqlite")
            book.profile = get_profile(row["profile"] or "de", self.cfg.checkpoints.patterns)
            ctx = BookContext(book_id, row["name"], self.cfg, book, appdb,
                              Importer(book, appdb, self.import_config(row)))
            from .backup import BackupWriter

            # always present: the local file is optional, Nextcloud targets can be added at any time
            ctx.backup = BackupWriter(book, row["backup_file"], self.cfg.backup.keep,
                                      remote=lambda bid=book_id: self.nextcloud_uploads(bid),
                                      work_dir=self.data / "backup" / "tmp")
            book.after_write.append(ctx.backup.request)
            self._contexts[book_id] = (key, ctx)
            return ctx

    IMPORT_KEYS = ("fallback_account", "transit_account", "transit_between", "accounts", "iban_map",
                   "match_days", "transfer_match_days")

    def import_config(self, row):
        """[import] from config.toml, overridden by the book's own import settings."""
        import dataclasses
        import json

        try:
            own = json.loads(row["import_settings"] or "{}")
        except ValueError:
            own = {}
        own = {k: v for k, v in own.items() if k in self.IMPORT_KEYS and v not in (None, "", [], {})}
        return dataclasses.replace(self.cfg.importer, **own)

    def drop_context(self, book_id: int):
        with self._lock:
            cached = self._contexts.pop(book_id, None)
        if cached:
            cached[1].book.dispose()

    def create_book(self, name: str, content: str = "empty", upload_path: str | None = None,
                    users=(), backup: bool = True) -> tuple[int, dict]:
        """New PostgreSQL role + database + GnuCash book. Returns (book id, credentials for GnuCash Desktop)."""
        from sqlalchemy.engine import make_url

        from . import provision

        admin_url = self.cfg.postgres.admin_url
        if not admin_url:
            raise UserError(_("Neue Datenbanken anlegen ist aus: [postgres] admin_url fehlt in config.toml."))
        if not name.strip():
            raise UserError(_("Bitte einen Namen angeben."))
        if any(b["name"] == name.strip() for b in self.system.books()):
            raise UserError(_("Ein Buch „{a0}“ gibt es schon.", a0=name.strip()))
        dbname = provision.db_name_for(name)
        try:
            url, password = provision.create_role_and_db(admin_url, dbname)
        except provision.ProvisionError as exc:
            raise UserError(str(exc))
        try:
            if content == "upload":
                provision.import_sqlite_book(upload_path, url)
            else:
                provision.create_empty_book(url, template="simple" if content == "simple" else "none")
        except Exception as exc:
            provision.drop_role_and_db(admin_url, dbname)  # nothing half-created stays behind
            raise UserError(_("Buch konnte nicht angelegt werden: {a0}", a0=exc))
        bid = self.system.add_book(name, url, self.cfg.book.timezone,
                                   self.default_backup_file(name) if backup else "", managed_db=dbname)
        self.system.set_book_users(bid, users)
        u = make_url(url)
        creds = {"host": self.cfg.postgres.client_host or u.host, "port": u.port or 5432, "database": dbname,
                 "user": dbname, "password": password, "book": name}
        return bid, creds

    def remove_book(self, book_id: int, drop_database: bool = False) -> str | None:
        """Disconnect a book; optionally drop its managed database after writing a .gnucash copy."""
        from . import provision
        from .backup import export_gnucash_file

        row = self.system.book(book_id)
        if row is None:
            return None
        saved = None
        if drop_database:
            if not row["managed_db"] or not self.cfg.postgres.admin_url:
                raise UserError(_("Nur von gnubook angelegte Datenbanken können hier gelöscht werden."))
            from datetime import datetime

            ctx = self.context(book_id)
            target = self.data / "backup" / "deleted" / f"{slug(row['name'])}-{datetime.now():%Y%m%d-%H%M%S}.gnucash"
            export_gnucash_file(ctx.book, target)
            saved = str(target)
        self.drop_context(book_id)
        self.system.delete_book(book_id)
        if drop_database:
            provision.drop_role_and_db(self.cfg.postgres.admin_url, row["managed_db"])
        return saved

    def nextcloud_uploads(self, book_id: int) -> list[dict]:
        """Upload targets of a book with decrypted passwords (only ever held in memory)."""
        if not self.cfg.nextcloud.enabled:
            return []
        out = []
        for row in self.system.nextcloud_uploads(book_id):
            item = dict(row)
            try:
                item["app_password"] = self.system.nextcloud_password(row)
            except Exception as exc:  # noqa: BLE001 – key changed: report per user instead of failing all
                item["app_password"], item["error"] = "", str(exc)
            out.append(item)
        return out

    def default_backup_file(self, name: str) -> str:
        return str(self.data / "backup" / f"{slug(name)}.gnucash")

    def dispose(self):
        with self._lock:
            for _key, ctx in self._contexts.values():
                ctx.book.dispose()
            self._contexts.clear()
