"""gnubook – a self-hosted web frontend for a GnuCash SQL book."""
from __future__ import annotations

__version__ = "0.2.0"

import logging
from datetime import timedelta

from flask import Flask

from .config import Config, ConfigError, load_config, validate_for_web
from .system import Registry

log = logging.getLogger("gnubook")


def create_app(config: Config | None = None, config_path: str | None = None, check: bool = True) -> Flask:
    cfg = config or load_config(config_path)
    if check:
        problems = validate_for_web(cfg)
        if problems:
            raise ConfigError("Konfiguration unvollständig: " + "; ".join(problems))

    app = Flask(__name__)
    app.secret_key = cfg.app.secret_key or "dev-only-not-secret"
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.app.session_cookie_secure,
        SESSION_COOKIE_NAME="gnubook_session",
        PERMANENT_SESSION_LIFETIME=timedelta(days=cfg.app.session_days),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        JSON_SORT_KEYS=False,
    )
    if cfg.app.behind_proxy:
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    app.extensions["gnubook"] = Registry(cfg)

    from .web import register

    register(app)
    return app
