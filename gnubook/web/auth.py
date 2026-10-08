"""Login (single user from the config), session handling and CSRF protection."""
from __future__ import annotations

import hmac
import secrets
import time
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from . import state

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
        abort(400, description="Ungültiges oder fehlendes CSRF-Token – bitte Seite neu laden.")


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            check_csrf()
        return view(*args, **kwargs)
    return wrapper


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
    st = state()
    if request.method == "POST":
        check_csrf()
        ip = _client_ip()
        now = time.time()
        if st.appdb.login_failures(ip, FAILURE_WINDOW, now) >= MAX_FAILURES:
            flash("Zu viele Fehlversuche – bitte 15 Minuten warten.", "danger")
            return render_template("auth/login.html"), 429
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user_ok = hmac.compare_digest(username.encode(), st.cfg.app.username.encode())
        pw_ok = bool(st.cfg.app.password_hash) and check_password_hash(st.cfg.app.password_hash, password)
        if user_ok and pw_ok:
            st.appdb.clear_login_failures(ip)
            session.clear()
            session.permanent = True
            session["user"] = st.cfg.app.username
            csrf_token()
            st.appdb.audit(st.cfg.app.username, "login", None, ip)
            return redirect(_safe_next(request.args.get("next")))
        st.appdb.add_login_failure(ip, now)
        time.sleep(0.5)
        flash("Benutzername oder Passwort falsch.", "danger")
        return render_template("auth/login.html"), 401
    if session.get("user"):
        return redirect(url_for("views.dashboard"))
    return render_template("auth/login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    session.clear()
    flash("Abgemeldet.", "info")
    return redirect(url_for("auth.login"))
