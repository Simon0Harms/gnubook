"""Minimal translation layer: German source strings, English translations in translations_en.py.

`_("Text mit {name}", name=x)` returns the text in the language of the current request (g.lang), German
outside of a request (command line).
"""
from __future__ import annotations

from flask import g, has_request_context

LANGUAGES = {"de": "Deutsch", "en": "English"}
DEFAULT = "de"


def current_lang() -> str:
    if has_request_context():
        return g.get("lang", DEFAULT)
    return DEFAULT


def gettext(text: str, **kwargs) -> str:
    if current_lang() == "en":
        from .translations_en import EN

        text = EN.get(text, text)
    return text.format(**kwargs) if kwargs else text


_ = gettext


def missing(texts) -> list[str]:
    from .translations_en import EN

    return [t for t in texts if t not in EN]
