"""Users, books and API tokens (data_dir/system.sqlite) and the per-book runtime contexts.

Every book is a GnuCash database of its own (normally one PostgreSQL database per book). Users and books
are related n:m. Each book keeps its gnubook data (API ids, import records, accepted checkpoint differences,
audit log) in data_dir/books/<id>.sqlite.
"""
from __future__ import annotations

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
    created_at TEXT NOT NULL
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
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
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
            raise UserError(f"Das Passwort muss mindestens {MIN_PASSWORD} Zeichen haben.")

    def add_user(self, username: str, password: str | None = None, is_admin: bool = False,
                 password_hash: str | None = None) -> int:
        username = (username or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._@-]{2,64}", username):
            raise UserError("Benutzername: 2–64 Zeichen, nur Buchstaben, Ziffern und . _ @ -")
        if password_hash is None:
            self._check_password(password)
            password_hash = generate_password_hash(password)
        try:
            with self.conn() as c:
                return int(c.execute("INSERT INTO users (username, password_hash, is_admin, created_at) "
                                     "VALUES (?, ?, ?, ?)", (username, password_hash, int(is_admin),
                                                            now_iso())).lastrowid)
        except sqlite3.IntegrityError:
            raise UserError(f"Benutzer „{username}“ gibt es schon.")

    def set_password(self, user_id: int, password: str):
        self._check_password(password)
        with self.conn() as c:
            c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user_id))

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

    def add_book(self, name: str, url: str, timezone: str = "Europe/Berlin", backup_file: str = "") -> int:
        if not name.strip() or not url.strip():
            raise UserError("Name und Datenbank-URL sind nötig.")
        with self.conn() as c:
            return int(c.execute("INSERT INTO books (name, url, timezone, backup_file, created_at) "
                                 "VALUES (?, ?, ?, ?, ?)", (name.strip(), url.strip(), timezone, backup_file,
                                                            now_iso())).lastrowid)

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
        self.system = SystemDB(self.data / "system.sqlite")
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
        key = (row["name"], row["url"], row["timezone"], row["backup_file"])
        with self._lock:
            cached = self._contexts.get(book_id)
            if cached and cached[0] == key:
                return cached[1]
            if cached:  # settings changed
                cached[1].book.dispose()
            book = Book(row["url"], row["timezone"], self.cfg.book.account_separator)
            appdb = AppDB(self.data / "books" / f"{book_id}.sqlite")
            ctx = BookContext(book_id, row["name"], self.cfg, book, appdb, Importer(book, appdb, self.cfg.importer))
            if row["backup_file"]:
                from .backup import BackupWriter

                ctx.backup = BackupWriter(book, row["backup_file"], self.cfg.backup.keep)
                book.after_write.append(ctx.backup.request)
            self._contexts[book_id] = (key, ctx)
            return ctx

    def default_backup_file(self, name: str) -> str:
        return str(self.data / "backup" / f"{slug(name)}.gnucash")

    def dispose(self):
        with self._lock:
            for _, ctx in self._contexts.values():
                ctx.book.dispose()
            self._contexts.clear()
