"""Matrix: link a gnubook user with a Matrix account, log in with a link sent per Matrix, and notifications
(failed Belegeingang runs).

gnubook itself has no Matrix dependency. A sidecar (deploy/matrix/gnubook_matrix_bot.py, matrix-nio with E2EE)
is logged in as the bot and talks to gnubook through two spool directories under data_dir/matrix – the same
principle as in Portriga:

    outbox/  gnubook -> sidecar   {id, roomId, body, createdAt, leave?}
    inbox/   sidecar -> gnubook   {eventId, roomId, sender, body, ts, encrypted}
                                  or {type: "leave", eventId, roomId, sender, ts}

One file per message, written atomically (tmp + rename). The sender of an inbox message is authenticated by the
homeserver, which is what proves that a Matrix ID belongs to the user who shows the link code.

Linking: the user asks for a code "GB-XXXX-XXXX" under "Konto und Sprache" and sends it to the bot in a direct
chat. The sender's Matrix ID and the chat are then stored with the user (users.mxid, users.matrix_room).

Login: on the login page (user name or Matrix ID) or by writing "login" to the bot, the bot sends a one-time
link (valid [matrix] login_minutes). Opening it shows a button; only the POST logs in, so link previews of a
Matrix client cannot use the link up.

Codes and login tokens are stored only as HMAC (key: [app] secret_key) in system.sqlite (matrix_codes).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

from .i18n import translate

log = logging.getLogger("gnubook.matrix")

MXID_RE = re.compile(r"^@[^\s:]+:[^\s:]+$")
CODE_ALPHA = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
LINK_RE = re.compile(r"GB-?([A-Z2-9]{4})-?([A-Z2-9]{4})", re.I)
LOGIN_CMD_RE = re.compile(r"^\s*!?(login|anmelden)\s*$", re.I)
HELP_CMD_RE = re.compile(r"^\s*!?(hilfe|help|\?)\s*$", re.I)
PREFIX = "📒 gnubook"
LOGIN_RATE = 30      # seconds between two login links for the same user
HELP_RATE = 60       # seconds between two help answers in the same room
NOTIFY_STATE = "notify-state.json"


def enabled(cfg) -> bool:
    return bool(cfg.matrix.bot_mxid and MXID_RE.match(cfg.matrix.bot_mxid))


def login_enabled(cfg) -> bool:
    return enabled(cfg) and cfg.matrix.login and bool(cfg.matrix.public_url)


def matrix_to(cfg) -> str:
    return "https://matrix.to/#/" + cfg.matrix.bot_mxid


def base_dir(cfg) -> Path:
    return Path(cfg.app.data_dir) / "matrix"


def _dirs(cfg) -> tuple[Path, Path]:
    root = base_dir(cfg)
    inbox, outbox = root / "inbox", root / "outbox"
    for d in (root, inbox, outbox):
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError:
            pass
    return inbox, outbox


def _write_spool(directory: Path, obj: dict) -> str:
    fid = f"{int(time.time() * 1000):x}-{secrets.token_hex(8)}"
    tmp = directory / f".{fid}.tmp"
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, directory / f"{fid}.json")  # the sidecar only ever sees complete files
    return fid


def enqueue(cfg, room_id: str, body: str, leave: bool = False) -> str | None:
    """Put a message for the sidecar into the outbox (works from the web app and from the command line)."""
    if not enabled(cfg) or not room_id:
        return None
    _inbox, outbox = _dirs(cfg)
    msg = {"roomId": room_id, "body": body, "createdAt": int(time.time() * 1000)}
    if leave:
        msg["leave"] = True
    msg["id"] = _write_spool(outbox, msg)
    return msg["id"]


def _lang(cfg, user) -> str:
    return (user["language"] if user is not None and "language" in user.keys() else "") or cfg.app.language or "de"


def _url(cfg, path: str) -> str:
    return cfg.matrix.public_url.rstrip("/") + path


# ---------------------------------------------------------------------------------------------- codes
def _hash(cfg, purpose: str, value: str) -> str:
    return hmac.new(cfg.app.secret_key.encode(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def _new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHA) for _ in range(8))


def _prune(c):
    c.execute("DELETE FROM matrix_codes WHERE expires < ?", (time.time() - 3600,))


def create_link_code(reg, user_id: int) -> tuple[str, int]:
    """New code "GB-XXXX-XXXX" for linking the user's Matrix account; older open codes of the user end."""
    cfg = reg.cfg
    raw = _new_code()
    now = time.time()
    with reg.system.conn() as c:
        _prune(c)
        c.execute("DELETE FROM matrix_codes WHERE purpose = 'link' AND user_id = ? AND used_at IS NULL", (user_id,))
        cid = c.execute("INSERT INTO matrix_codes (purpose, code_hash, user_id, expires, created_at) "
                        "VALUES ('link', ?, ?, ?, ?)",
                        (_hash(cfg, "link", raw), user_id, now + cfg.matrix.link_minutes * 60, now)).lastrowid
    return f"GB-{raw[:4]}-{raw[4:]}", int(cid)


def link_status(reg, user_id: int, code_id: int) -> str:
    """pending | done | expired | <error text> of a link code of this user."""
    with reg.system.conn() as c:
        r = c.execute("SELECT * FROM matrix_codes WHERE id = ? AND user_id = ? AND purpose = 'link'",
                      (code_id, user_id)).fetchone()
    if r is None:
        return "expired"
    if r["used_at"]:
        return r["status"] or "done"
    return "pending" if r["expires"] > time.time() else "expired"


def unlink(reg, user_id: int) -> bool:
    cfg = reg.cfg
    u = reg.system.user(user_id)
    if u is None or not u["mxid"]:
        return False
    room = u["matrix_room"]
    with reg.system.conn() as c:
        c.execute("UPDATE users SET mxid = '', matrix_room = '' WHERE id = ?", (user_id,))
        shared = room and c.execute("SELECT 1 FROM users WHERE matrix_room = ? AND id <> ?", (room, user_id)).fetchone()
    if room:
        lang = _lang(cfg, u)
        text = translate(lang, "Die Verknüpfung mit dem gnubook-Benutzer „{a0}“ wurde gelöst.", a0=u["username"])
        if not shared:
            text += "\n" + translate(lang, "Ich verlasse diesen Chat.")
        enqueue(cfg, room, f"{PREFIX}\n{text}", leave=not shared)
    log.info("Matrix-Verknüpfung gelöst: %s (%s)", u["username"], u["mxid"])
    return True


def set_notify(reg, user_id: int, on: bool):
    with reg.system.conn() as c:
        c.execute("UPDATE users SET matrix_notify = ? WHERE id = ?", (int(bool(on)), user_id))


def user_for_login(reg, login: str):
    login = (login or "").strip()
    if not login:
        return None
    with reg.system.conn() as c:
        if login.startswith("@"):
            return c.execute("SELECT * FROM users WHERE lower(mxid) = lower(?) AND is_demo = 0", (login,)).fetchone()
        return c.execute("SELECT * FROM users WHERE username = ? AND is_demo = 0", (login,)).fetchone()


def send_login_link(reg, user) -> bool:
    """Send a one-time login link to the user's Matrix chat. False when not possible or rate-limited."""
    cfg = reg.cfg
    if not login_enabled(cfg) or user is None or not user["active"] or not user["mxid"] or not user["matrix_room"]:
        return False
    now = time.time()
    token = secrets.token_urlsafe(32)
    with reg.system.conn() as c:
        _prune(c)
        last = c.execute("SELECT MAX(created_at) FROM matrix_codes WHERE purpose = 'login' AND user_id = ?",
                         (user["id"],)).fetchone()[0]
        if last and now - last < LOGIN_RATE:
            return False
        c.execute("INSERT INTO matrix_codes (purpose, code_hash, user_id, expires, created_at) "
                  "VALUES ('login', ?, ?, ?, ?)",
                  (_hash(cfg, "login", token), user["id"], now + cfg.matrix.login_minutes * 60, now))
    lang = _lang(cfg, user)
    text = translate(lang, "Login-Link für „{a0}“ (gültig {a1} Minuten, nur einmal verwendbar):",
                     a0=user["username"], a1=cfg.matrix.login_minutes)
    hint = translate(lang, "Nicht angefordert? Dann einfach ignorieren.")
    enqueue(cfg, user["matrix_room"], f"{PREFIX}\n{text}\n{_url(cfg, '/login/matrix/' + token)}\n{hint}")
    log.info("Login-Link per Matrix an %s (%s)", user["username"], user["mxid"])
    return True


def check_login_token(reg, token: str, consume: bool):
    """The user of a valid, unused login token (marked used when consume) or None."""
    if not token or len(token) > 100:
        return None
    h = _hash(reg.cfg, "login", token)
    now = time.time()
    with reg.system.conn() as c:
        r = c.execute("SELECT * FROM matrix_codes WHERE purpose = 'login' AND code_hash = ?", (h,)).fetchone()
        if r is None or r["used_at"] or r["expires"] < now:
            return None
        if consume:
            cur = c.execute("UPDATE matrix_codes SET used_at = ?, status = 'done' WHERE id = ? AND used_at IS NULL",
                            (now, r["id"]))
            if cur.rowcount != 1:  # used by a parallel request
                return None
        u = c.execute("SELECT * FROM users WHERE id = ?", (r["user_id"],)).fetchone()
    return u if u is not None and u["active"] and not u["is_demo"] else None


# ---------------------------------------------------------------------------------------------- inbox
class Inbox:
    """Handles the messages the sidecar put into the inbox."""

    def __init__(self, reg):
        self.reg = reg
        self.cfg = reg.cfg
        self._help_sent: dict[str, float] = {}

    def reply(self, room: str, text: str, leave: bool = False):
        enqueue(self.cfg, room, f"{PREFIX}\n{text}", leave=leave)

    def process(self) -> int:
        """All waiting inbox files; each is claimed by renaming it, so two processes never handle one twice."""
        if not enabled(self.cfg):
            return 0
        inbox, _outbox = _dirs(self.cfg)
        done = 0
        for p in sorted(inbox.glob("*.json")):
            claimed = p.with_name("." + p.name + ".work")
            try:
                os.replace(p, claimed)
            except OSError:
                continue
            try:
                msg = json.loads(claimed.read_text(encoding="utf-8"))
                if isinstance(msg, dict):
                    self.handle(msg)
                    done += 1
            except Exception:  # noqa: BLE001 – one broken message must not stop the others
                log.exception("Matrix-Inbox: %s", p.name)
            finally:
                try:
                    claimed.unlink()
                except OSError:
                    pass
        return done

    def handle(self, msg: dict) -> str:
        room, sender = str(msg.get("roomId") or ""), str(msg.get("sender") or "")
        if not room or not MXID_RE.match(sender) or sender.lower() == self.cfg.matrix.bot_mxid.lower():
            return "ignored"
        if msg.get("type") == "leave":
            return self._left(room, sender)
        body = str(msg.get("body") or "")[:2000]
        system = self.reg.system
        with system.conn() as c:
            user = c.execute("SELECT * FROM users WHERE lower(mxid) = lower(?)", (sender,)).fetchone()
        lang = _lang(self.cfg, user)

        m = LINK_RE.search(body)
        if m:
            return self._link(room, sender, (m.group(1) + m.group(2)).upper(), user)
        if LOGIN_CMD_RE.match(body):
            if user is None:
                self.reply(room, translate(lang, "Deine Matrix-ID ist mit keinem gnubook-Benutzer verknüpft. "
                                                 "Melde dich in gnubook an und verknüpfe sie unter „Konto und Sprache“."))
                return "nouser"
            if user["matrix_room"] != room:  # login from a new chat: answer there from now on
                with system.conn() as c:
                    c.execute("UPDATE users SET matrix_room = ? WHERE id = ?", (room, user["id"]))
                user = system.user(user["id"])
            if not login_enabled(self.cfg):
                self.reply(room, translate(lang, "Anmelden per Matrix ist auf diesem Server ausgeschaltet."))
                return "off"
            if not user["active"]:
                return "inactive"
            return "loginlink" if send_login_link(self.reg, user) else "rate"
        if HELP_CMD_RE.match(body) or user is not None:
            now = time.time()
            if now - self._help_sent.get(room, 0) < HELP_RATE:
                return "rate"
            self._help_sent[room] = now
            lines = [translate(lang, "Befehle:")]
            if login_enabled(self.cfg):
                lines.append(translate(lang, "login – Login-Link für gnubook schicken"))
            lines.append(translate(lang, "GB-XXXX-XXXX – Code aus gnubook („Konto und Sprache“) zum Verknüpfen"))
            self.reply(room, "\n".join(lines))
            return "help"
        return "ignored"

    def _link(self, room: str, sender: str, raw: str, current) -> str:
        cfg, system = self.cfg, self.reg.system
        now = time.time()
        with system.conn() as c:
            r = c.execute("SELECT * FROM matrix_codes WHERE purpose = 'link' AND code_hash = ?",
                          (_hash(cfg, "link", raw),)).fetchone()
            target = c.execute("SELECT * FROM users WHERE id = ?", (r["user_id"],)).fetchone() if r else None
        lang = _lang(cfg, target or current)
        if r is None or r["used_at"] or r["expires"] < now or target is None:
            self.reply(room, translate(lang, "Dieser Code ist ungültig oder abgelaufen. Fordere in gnubook unter "
                                             "„Konto und Sprache“ einen neuen an."))
            return "badcode"
        if current is not None and current["id"] != target["id"]:
            err = translate(lang, "Diese Matrix-ID ist bereits mit dem gnubook-Benutzer „{a0}“ verknüpft.",
                            a0=current["username"])
            with system.conn() as c:
                c.execute("UPDATE matrix_codes SET used_at = ?, status = ? WHERE id = ?", (now, err, r["id"]))
            self.reply(room, err + " " + translate(lang, "Die Verknüpfung wurde nicht geändert."))
            return "mxidtaken"
        old_room, old_mxid = target["matrix_room"], target["mxid"]
        with system.conn() as c:
            c.execute("UPDATE users SET mxid = ?, matrix_room = ? WHERE id = ?", (sender, room, target["id"]))
            c.execute("UPDATE matrix_codes SET used_at = ?, status = 'done' WHERE id = ?", (now, r["id"]))
            shared = old_room and old_room != room and c.execute(
                "SELECT 1 FROM users WHERE matrix_room = ?", (old_room,)).fetchone()
        log.info("Matrix verknüpft: %s -> %s", target["username"], sender)
        lines = [translate(lang, "Verknüpft: gnubook-Benutzer „{a0}“ ↔ {a1}.", a0=target["username"], a1=sender)]
        if login_enabled(cfg):
            lines.append(translate(lang, "Schreib mir „login“, um einen Login-Link zu bekommen."))
        if cfg.matrix.notify and cfg.postbox.enabled:
            lines.append(translate(lang, "Fehler beim Belegeingang deiner Bücher melde ich hier (abschaltbar unter "
                                         "„Konto und Sprache“)."))
        self.reply(room, "\n".join(lines))
        if old_room and old_room != room:
            text = translate(lang, "Die Matrix-Verknüpfung von „{a0}“ wurde auf einen anderen Chat umgestellt.",
                             a0=target["username"])
            if old_mxid and old_mxid.lower() != sender.lower():
                text = translate(lang, "Die Matrix-Verknüpfung von „{a0}“ wurde auf {a1} umgestellt.",
                                 a0=target["username"], a1=sender)
            self.reply(old_room, text, leave=not shared)
        return "linked"

    def _left(self, room: str, sender: str) -> str:
        """The user left the chat: messages cannot reach them there any more. The Matrix ID stays linked; writing
        "login" from a new chat restores the delivery."""
        with self.reg.system.conn() as c:
            n = c.execute("UPDATE users SET matrix_room = '' WHERE matrix_room = ? AND lower(mxid) = lower(?)",
                          (room, sender)).rowcount
        if n:
            log.info("Matrix: %s hat den Chat %s verlassen", sender, room)
        return "left" if n else "ignored"


_worker_lock = threading.Lock()
_workers: dict[int, threading.Thread] = {}


def start_worker(reg):
    """Background thread of the web app that handles the inbox every [matrix] poll_seconds."""
    cfg = reg.cfg
    if not enabled(cfg) or cfg.matrix.poll_seconds <= 0:
        return None
    with _worker_lock:
        if id(reg) in _workers:
            return _workers[id(reg)]
        inbox = Inbox(reg)

        def loop():
            while not getattr(reg, "_disposed", False):
                try:
                    inbox.process()
                except Exception:  # noqa: BLE001
                    log.exception("Matrix-Inbox")
                time.sleep(cfg.matrix.poll_seconds)

        t = threading.Thread(target=loop, daemon=True, name="gnubook-matrix-inbox")
        _workers[id(reg)] = t
        t.start()
        return t


# ---------------------------------------------------------------------------------------------- notifications
def _state_path(cfg) -> Path:
    return base_dir(cfg) / NOTIFY_STATE


def _load_state(cfg) -> dict:
    try:
        return json.loads(_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(cfg, state: dict):
    _dirs(cfg)
    p = _state_path(cfg)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def recipients(reg, book_id: int) -> list:
    """Active users of the book with a linked Matrix chat and notifications on."""
    out = []
    for uid in reg.system.book_users(book_id):
        u = reg.system.user(uid)
        if u is not None and u["active"] and not u["is_demo"] and u["mxid"] and u["matrix_room"] and u["matrix_notify"]:
            out.append(u)
    return out


POSTBOX_STATUS = {"error": "Fehler", "login_failed": "Anmeldung fehlgeschlagen",
                  "approval_required": "Freigabe verlangt", "not_installed": "nicht installiert",
                  "warning": "Hinweis", "ok": "ok"}


def _notify_attention(reg, book_id: int, book_name: str, kind: str, outcome) -> int:
    """Report documents of this run that Portfolio Performance could not import on its own. The count covers only
    the documents of this run, so each message is about new documents and needs no de-duplication."""
    cfg = reg.cfg
    try:
        n = int((outcome.processed or {}).get("attention") or 0)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    sent = 0
    for u in recipients(reg, book_id):
        lang = _lang(cfg, u)
        lines = [PREFIX, translate(lang, "Belegeingang „{a0}“: {a1} zur Prüfung", a0=book_name, a1=n)]
        if cfg.matrix.public_url:
            lines.append(_url(cfg, f"/postbox/?source={kind}&filter=attention#docs"))
        if enqueue(cfg, u["matrix_room"], "\n".join(lines)):
            sent += 1
    return sent


def notify_postbox(reg, book_id: int, book_name: str, kind: str, outcome) -> int:
    """Report a failed Belegeingang run (and, once, that it works again) and documents that need a review. The
    same failure is reported only once until something changes. Returns the number of messages queued."""
    cfg = reg.cfg
    if not enabled(cfg) or not cfg.matrix.notify:
        return 0
    status = outcome.status
    if status in ("busy", "paused", "dry_run"):
        return 0
    sent = _notify_attention(reg, book_id, book_name, kind, outcome)
    errors = list((outcome.processed or {}).get("errors") or [])
    failed = outcome.failed or bool(errors)
    key = f"{int(book_id)}-{kind}"
    state = _load_state(cfg)
    last = state.get(key)
    if failed:
        detail = outcome.message if outcome.failed else "; ".join(str(e) for e in errors[:3])
        signature = f"{status}:{detail}"[:500]
        if last == signature:
            return sent
        state[key] = signature
    elif last:
        state.pop(key, None)
    else:
        return sent
    _save_state(cfg, state)
    for u in recipients(reg, book_id):
        lang = _lang(cfg, u)
        if failed:
            head = translate(lang, "Belegeingang „{a0}“: {a1}", a0=book_name,
                             a1=translate(lang, POSTBOX_STATUS.get(status, "Fehler")) if outcome.failed
                             else translate(lang, "Fehler bei der Verarbeitung"))
            body = detail
        else:
            head = translate(lang, "Belegeingang „{a0}“: läuft wieder", a0=book_name)
            body = outcome.message
        lines = [PREFIX, head]
        if body:
            lines.append(body[:1500])
        if cfg.matrix.public_url:
            lines.append(_url(cfg, "/postbox/"))
        if enqueue(cfg, u["matrix_room"], "\n".join(lines)):
            sent += 1
    return sent


def send_test(reg, user) -> bool:
    if user is None or not user["matrix_room"]:
        return False
    lang = _lang(reg.cfg, user)
    return bool(enqueue(reg.cfg, user["matrix_room"],
                        f"{PREFIX}\n" + translate(lang, "Testnachricht für „{a0}“ – die Verbindung funktioniert.",
                                                  a0=user["username"])))
