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


def login_required(view=None, *, book: bool = True, admin: bool = False):
    """Requires a logged-in user; by default also a book the user may use (else: page 'keine Bücher')."""
    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = _load_user()
            if user is None:
                return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                check_csrf()
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


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target.rstrip("?")
    return url_for("views.dashboard")


@bp.app_context_processor
def inject_csrf():
    return {"csrf_token": csrf_token}


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
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            session["user"] = user["username"]
            csrf_token()
            return redirect(_safe_next(request.args.get("next")))
        system.add_login_failure(ip, now)
        time.sleep(0.5)
        flash(_("Benutzername oder Passwort falsch."), "danger")
        return render_template("auth/login.html"), 401
    if _load_user() is not None:
        return redirect(url_for("views.dashboard"))
    return render_template("auth/login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    session.clear()
    flash(_("Abgemeldet."), "info")
    return redirect(url_for("auth.login"))


@bp.route("/book/<int:book_id>", methods=["POST"])
@login_required(book=False)
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
    if user is not None:
        registry().system.set_language(user["id"], code)
    return resp


@bp.route("/account/language", methods=["POST"])
@login_required(book=False)
def account_language():
    from ..i18n import LANGUAGES

    code = request.form.get("language", "")
    registry().system.set_language(g.user["id"], code)
    resp = redirect(url_for("auth.change_password"))
    if code in LANGUAGES:
        resp.set_cookie("gnubook_lang", code, max_age=365 * 86400, samesite="Lax", httponly=True)
    else:
        resp.delete_cookie("gnubook_lang")
    return resp


@bp.route("/account/password", methods=["GET", "POST"])
@login_required(book=False)
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
    return render_template("auth/password.html")
