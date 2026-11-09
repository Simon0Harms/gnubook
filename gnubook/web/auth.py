"""Login (users from system.sqlite), book selection, session handling and CSRF protection."""
from __future__ import annotations

from ..i18n import gettext as _

import hmac
import secrets
import time
from functools import wraps

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

from ..system import UserError
from . import registry

bp = Blueprint("auth", __name__)

MAX_FAILURES = 8
FAILURE_WINDOW = 15 * 60


def csrf_token() -> str:
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf"] = tok
    return tok


def check_csrf():
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("csrf") or ""
    if not expected or not hmac.compare_digest(sent, expected):
        abort(400, description=_("Ungültiges oder fehlendes CSRF-Token – bitte Seite neu laden."))


def _load_user():
    uid = session.get("user_id")
    if not uid:
        return None
    user = registry().system.user(uid)
    if user is None or not user["active"]:
        session.clear()
        return None
    g.user = user
    return user


def _select_book(user):
    """Book of this session: the chosen one if still allowed, else the user's first book."""
    reg = registry()
    books = reg.system.user_books(user["id"])
    wanted = session.get("book_id")
    chosen = next((b for b in books if b["id"] == wanted), books[0] if books else None)
    if chosen is None:
        g.ctx = None
        return None
    session["book_id"] = chosen["id"]
    g.ctx = reg.context(chosen["id"])
    return g.ctx


def login_required(view=None, *, book: bool = True, admin: bool = False, demo_ok: bool = False,
                   demo_never: bool = False):
    """Requires a logged-in user; by default also a book the user may use (else: page 'keine Bücher')."""
    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = _load_user()
            if user is None:
                return redirect(url_for("auth.login",
                    next=request.script_root + request.full_path if request.method == "GET" else None))
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                check_csrf()
                # the shared demo is read-only unless [app] demo_writable; some things never (demo_never)
                writable = registry().cfg.app.demo_writable and not demo_never
                if user["is_demo"] and not demo_ok and not writable:
                    flash(_("Die Demo ist schreibgeschützt – Änderungen sind nicht möglich."), "warning")
                    return redirect(_safe_next(_referrer_path()))
            if admin and not user["is_admin"]:
                abort(403)
            ctx = _select_book(user)
            if book and ctx is None:
                return render_template("no_books.html"), 200
            return fn(*args, **kwargs)
        return wrapper
    return decorate(view) if view is not None else decorate


def _client_ip() -> str:
    return request.remote_addr or "?"


def _referrer_path() -> str | None:
    """Path of the referring page of this site (for going back after a refused change)."""
    from urllib.parse import urlsplit

    ref = urlsplit(request.referrer or "")
    if ref.netloc and ref.netloc != request.host:
        return None
    return (ref.path + ("?" + ref.query if ref.query else "")) or None


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target.rstrip("?")
    return url_for("views.dashboard")


@bp.app_context_processor
def inject_csrf():
    from .. import matrix

    return {"csrf_token": csrf_token, "matrix_login_enabled": lambda: matrix.login_enabled(registry().cfg)}


@bp.route("/login", methods=["GET", "POST"])
def login():
    system = registry().system
    if not system.users():
        return render_template("auth/login.html", no_users=True)
    if request.method == "POST":
        check_csrf()
        ip = _client_ip()
        now = time.time()
        if system.login_failures(ip, FAILURE_WINDOW, now) >= MAX_FAILURES:
            flash(_("Zu viele Fehlversuche – bitte 15 Minuten warten."), "danger")
            return render_template("auth/login.html"), 429
        user = system.authenticate(request.form.get("username", "").strip(), request.form.get("password", ""))
        if user is not None:
            system.clear_login_failures(ip)
            _start_session(user)
            return redirect(_safe_next(request.args.get("next")))
        system.add_login_failure(ip, now)
        time.sleep(0.5)
        flash(_("Benutzername oder Passwort falsch."), "danger")
        return render_template("auth/login.html"), 401
    if _load_user() is not None:
        return redirect(url_for("views.dashboard"))
    return render_template("auth/login.html")


def _start_session(user):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["user"] = user["username"]
    csrf_token()


# ------------------------------------------------------------------------------------------------- Matrix
@bp.route("/login/matrix", methods=["POST"])
def login_matrix():
    """Send a login link to the Matrix chat linked with the user. The answer is always the same (no hint which
    user names exist or have Matrix linked)."""
    from .. import matrix

    reg = registry()
    if not matrix.login_enabled(reg.cfg):
        abort(404)
    check_csrf()
    ip, now = _client_ip(), time.time()
    if reg.system.login_failures(ip, FAILURE_WINDOW, now) >= MAX_FAILURES:
        flash(_("Zu viele Fehlversuche – bitte 15 Minuten warten."), "danger")
        return redirect(url_for("auth.login"))
    login_name = request.form.get("login", "").strip()[:200]
    user = matrix.user_for_login(reg, login_name)
    if user is None:
        reg.system.add_login_failure(ip, now)  # guessing names counts like a wrong password
    else:
        matrix.send_login_link(reg, user)
    flash(_("Wenn zu „{a0}“ ein Matrix-Konto verknüpft ist, kommt der Login-Link gleich per Matrix.",
            a0=login_name), "info")
    return redirect(url_for("auth.login"))


@bp.route("/login/matrix/<token>", methods=["GET", "POST"])
def login_matrix_token(token):
    """GET only shows a button (link previews of Matrix clients must not use the link up), POST logs in."""
    from .. import matrix

    reg = registry()
    if not matrix.login_enabled(reg.cfg):
        abort(404)
    ip, now = _client_ip(), time.time()
    if reg.system.login_failures(ip, FAILURE_WINDOW, now) >= MAX_FAILURES:
        flash(_("Zu viele Fehlversuche – bitte 15 Minuten warten."), "danger")
        return render_template("auth/login_link.html", valid=False), 429
    if request.method == "POST":
        check_csrf()
        user = matrix.check_login_token(reg, token, consume=True)
        if user is None:
            reg.system.add_login_failure(ip, now)
            flash(_("Der Login-Link ist ungültig, abgelaufen oder schon benutzt."), "danger")
            return render_template("auth/login_link.html", valid=False), 404
        reg.system.clear_login_failures(ip)
        _start_session(user)
        return redirect(url_for("views.dashboard"))
    user = matrix.check_login_token(reg, token, consume=False)
    if user is None:
        reg.system.add_login_failure(ip, now)
        return render_template("auth/login_link.html", valid=False), 404
    return render_template("auth/login_link.html", valid=True, username=user["username"])


def _matrix_or_404():
    from .. import matrix

    if not matrix.enabled(registry().cfg):
        abort(404)
    return matrix


@bp.route("/account/matrix/link", methods=["POST"])
@login_required(book=False, demo_never=True)
def matrix_link():
    matrix = _matrix_or_404()
    code, code_id = matrix.create_link_code(registry(), g.user["id"])
    session["matrix_link"] = {"code": code, "id": code_id}
    return redirect(url_for("auth.change_password") + "#matrix")


@bp.route("/account/matrix/cancel", methods=["POST"])
@login_required(book=False, demo_never=True)
def matrix_cancel():
    session.pop("matrix_link", None)
    return redirect(url_for("auth.change_password") + "#matrix")


@bp.route("/account/matrix/unlink", methods=["POST"])
@login_required(book=False, demo_never=True)
def matrix_unlink():
    matrix = _matrix_or_404()
    if matrix.unlink(registry(), g.user["id"]):
        flash(_("Matrix-Verknüpfung gelöst."), "success")
    return redirect(url_for("auth.change_password") + "#matrix")


@bp.route("/account/matrix/notify", methods=["POST"])
@login_required(book=False, demo_never=True)
def matrix_notify():
    matrix = _matrix_or_404()
    matrix.set_notify(registry(), g.user["id"], request.form.get("on") == "1")
    flash(_("Gespeichert."), "success")
    return redirect(url_for("auth.change_password") + "#matrix")


@bp.route("/account/matrix/test", methods=["POST"])
@login_required(book=False, demo_never=True)
def matrix_test():
    matrix = _matrix_or_404()
    if matrix.send_test(registry(), g.user):
        flash(_("Testnachricht ist unterwegs – sie sollte in wenigen Sekunden im Matrix-Chat ankommen."), "info")
    else:
        flash(_("Kein Matrix-Chat bekannt. Schreib dem Bot „login“ oder verknüpfe neu."), "warning")
    return redirect(url_for("auth.change_password") + "#matrix")


def _matrix_context():
    """What the account page shows about Matrix."""
    from .. import matrix

    reg = registry()
    if not matrix.enabled(reg.cfg) or g.user["is_demo"]:
        return {"matrix_on": False}
    pending = session.get("matrix_link")
    link_state = None
    if pending:
        link_state = matrix.link_status(reg, g.user["id"], int(pending.get("id") or 0))
        if link_state != "pending":
            session.pop("matrix_link", None)
            if link_state == "done":
                flash(_("Matrix-Konto verknüpft."), "success")
                link_state, pending = None, None
    user = reg.system.user(g.user["id"])
    return {"matrix_on": True, "matrix_user": user, "matrix_pending": pending, "matrix_link_state": link_state,
            "matrix_bot": reg.cfg.matrix.bot_mxid, "matrix_to": matrix.matrix_to(reg.cfg),
            "matrix_login": matrix.login_enabled(reg.cfg),
            "matrix_notify_on": reg.cfg.matrix.notify and reg.cfg.postbox.enabled,
            "matrix_link_minutes": reg.cfg.matrix.link_minutes}


@bp.route("/demo", methods=["POST"])
def demo():
    """Log in as the shared demo user ([app] demo); read-only unless [app] demo_writable."""
    reg = registry()
    if not reg.cfg.app.demo:
        abort(404)
    check_csrf()
    user = reg.demo_user()
    session.clear()
    session.permanent = False
    session["user_id"] = user["id"]
    session["user"] = user["username"]
    csrf_token()
    return redirect(url_for("views.dashboard"))


def is_demo_user() -> bool:
    user = g.get("user") or _load_user()
    return bool(user is not None and user["is_demo"])


@bp.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    session.clear()
    flash(_("Abgemeldet."), "info")
    return redirect(url_for("auth.login"))


@bp.route("/book/<int:book_id>", methods=["POST"])
@login_required(book=False, demo_ok=True)
def switch_book(book_id):
    if not registry().system.may_use(g.user["id"], book_id):
        abort(403)
    session["book_id"] = book_id
    return redirect(url_for("views.dashboard"))


@bp.route("/language/<code>", methods=["POST"])
def set_language(code):
    """Switch the user-interface language: cookie (works before login), plus the account of a logged-in user."""
    from ..i18n import LANGUAGES

    check_csrf()
    if code not in LANGUAGES:
        abort(404)
    resp = redirect(_safe_next(request.form.get("next")))
    resp.set_cookie("gnubook_lang", code, max_age=365 * 86400, samesite="Lax", httponly=True)
    user = _load_user()
    if user is not None and not user["is_demo"]:
        registry().system.set_language(user["id"], code)
    return resp


@bp.route("/account/language", methods=["POST"])
@login_required(book=False, demo_ok=True)
def account_language():
    from ..i18n import LANGUAGES

    code = request.form.get("language", "")
    if not g.user["is_demo"]:  # the shared demo user keeps no language; the cookie is enough
        registry().system.set_language(g.user["id"], code)
    resp = redirect(url_for("auth.change_password"))
    if code in LANGUAGES:
        resp.set_cookie("gnubook_lang", code, max_age=365 * 86400, samesite="Lax", httponly=True)
    else:
        resp.delete_cookie("gnubook_lang")
    return resp


@bp.route("/account/password", methods=["GET", "POST"])
@login_required(book=False, demo_never=True)
def change_password():
    system = registry().system
    if request.method == "POST":
        if system.authenticate(g.user["username"], request.form.get("old", "")) is None:
            flash(_("Das bisherige Passwort stimmt nicht."), "danger")
        elif request.form.get("new", "") != request.form.get("new2", ""):
            flash(_("Die neuen Passwörter stimmen nicht überein."), "danger")
        else:
            try:
                system.set_password(g.user["id"], request.form.get("new", ""))
                flash(_("Passwort geändert."), "success")
                return redirect(url_for("views.dashboard"))
            except UserError as exc:
                flash(str(exc), "danger")
    return render_template("auth/password.html", **_matrix_context())
