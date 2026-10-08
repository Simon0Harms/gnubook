"""Flask blueprints, template filters and security headers."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from flask import Flask, current_app, g

from .. import __version__
from ..money import fmt, symbol_for


def state():
    return current_app.extensions["gnubook"]


def register(app: Flask):
    from . import api, auth, views

    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.register_blueprint(api.bp)

    @app.template_filter("money")
    def money_filter(value, mnemonic: str | None = None, places: int = 2, sign: bool = False):
        if value is None or value == "":
            return ""
        return fmt(Decimal(value), places, symbol_for(mnemonic) if mnemonic else None, sign=sign)

    @app.template_filter("amount_class")
    def amount_class(value):
        if value is None:
            return ""
        v = Decimal(value)
        return "text-success" if v > 0 else ("text-danger" if v < 0 else "text-body-secondary")

    @app.template_filter("iban")
    def iban_filter(value):
        v = (value or "").replace(" ", "")
        if len(v) >= 15 and v[:2].isalpha() and v[2:4].isdigit():
            return " ".join(v[i:i + 4] for i in range(0, len(v), 4))
        return value or ""

    @app.template_filter("neg_class")
    def neg_class(value):
        return "text-danger" if value is not None and Decimal(value) < 0 else ""

    @app.template_filter("de_date")
    def de_date(value):
        if value is None or value == "":
            return ""
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return value
        if isinstance(value, datetime):
            value = value.date()
        return value.strftime("%d.%m.%Y") if isinstance(value, date) else str(value)

    @app.template_filter("de_datetime")
    def de_datetime(value):
        if value is None or value == "":
            return ""
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                return value
        st = state()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = st.book.local_datetime(value)
            else:
                value = value.astimezone(st.book.tz)
            return value.strftime("%d.%m.%Y %H:%M")
        return str(value)

    @app.context_processor
    def inject():
        return {"app_version": __version__, "app_title": state().cfg.app.title, "today": state().book.today()}

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if resp.mimetype == "text/html":
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                "form-action 'self'; base-uri 'self'")
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.teardown_appcontext
    def _cleanup(_exc):
        g.pop("index", None)
