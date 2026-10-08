"""User-interface language (German or English).

The German text is the message id: ``_("Konten")`` returns "Konten" or "Accounts". Placeholders use
str.format syntax and are filled after the lookup: ``_("{n} offen", n=3)``.

The language of a request comes from the ``gnubook_lang`` cookie (set by the switch in the user menu),
else from ``[app] language`` in config.toml. Outside a request (CLI, background jobs) it is German.
Number and date formats stay German in both languages, because amount input is parsed the German way.
"""
from __future__ import annotations

from .i18n_en import EN

LANGUAGES = {"de": "Deutsch", "en": "English"}
DEFAULT_LANGUAGE = "de"
COOKIE = "gnubook_lang"


def normalize(lang: str | None) -> str | None:
    lang = (lang or "").strip().lower()[:2]
    return lang if lang in LANGUAGES else None


def current_language() -> str:
    try:
        from flask import current_app, g, has_request_context, request
    except ImportError:  # pragma: no cover
        return DEFAULT_LANGUAGE
    if not has_request_context():
        return DEFAULT_LANGUAGE
    lang = g.get("lang")
    if lang is None:
        lang = normalize(request.cookies.get(COOKIE))
        if lang is None:
            reg = current_app.extensions.get("gnubook")
            cfg_lang = getattr(getattr(getattr(reg, "cfg", None), "app", None), "language", None)
            lang = normalize(cfg_lang) or DEFAULT_LANGUAGE
        g.lang = lang
    return lang


def gettext(message: str, **kwargs) -> str:
    text = EN.get(message, message) if current_language() == "en" else message
    return text.format(**kwargs) if kwargs else text


_ = gettext
