"""Language switch and completeness of the English texts."""
import ast
import re
from pathlib import Path

from gnubook import book, ledger
from gnubook.web import reports as report_views
from gnubook.i18n import gettext
from gnubook.translations_en import EN
from gnubook.web import views

PKG = Path(__file__).resolve().parent.parent / "gnubook"
_TEMPLATE_RE = re.compile(r"""\b_\(\s*'((?:[^'\\]|\\.)*)'""")

def _python_ids():
    ids = set()
    for path in PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                ids.add(node.args[0].value)
    return ids

def message_ids():
    ids = _python_ids()
    for path in (PKG / "templates").rglob("*.html"):
        ids.update(m.group(1) for m in _TEMPLATE_RE.finditer(path.read_text(encoding="utf-8")))
    # texts translated indirectly via _(variable)
    ids.update(book.TYPE_LABELS.values())
    ids.update(book.GROUP_LABELS.values())
    ids.update(x for pair in views.COLUMN_LABELS.values() for x in pair)
    ids.update(views.STATUS_LABELS.values())
    ids.update(views.SOURCE_LABELS.values())
    ids.update(report_views.PERIOD_LABELS.values())
    ids.update(report_views.NW_PERIOD_LABELS.values())
    ids.update(report_views.PF_PERIOD_LABELS.values())
    ids.update(report_views.KIND_LABELS.values())
    ids.update(report_views.MONTHS)
    ids.update({ledger.MULTI, "Soll", "Haben", "Beschreibung", "Nummer", "Notizen"})
    return ids

def test_every_text_has_an_english_translation():
    missing = sorted(message_ids() - EN.keys())
    assert not missing, "missing in gnubook/translations_en.py:\n" + "\n".join(missing)

def test_placeholders_match():
    for de, en in EN.items():
        assert set(re.findall(r"\{(\w*)\}", de)) == set(re.findall(r"\{(\w*)\}", en)), de

def test_gettext_without_request_is_german():
    assert gettext("Konten") == "Konten"
    assert gettext("{n} offen", n=3) == "3 offen"


def _switch(client, lang, next_url="/accounts"):
    return client.post(f"/language/{lang}", data={"csrf_token": client.csrf, "next": next_url})


def test_switch_to_english_and_back(client, state):
    r = _switch(client, "en")
    assert r.status_code == 302 and r.headers["Location"] == "/accounts"
    page = client.get("/").text
    assert '<html lang="en"' in page and "Overview" in page and "Übersicht" not in page
    giro = state.book.load_accounts().find("Aktiva:Barvermögen:Girokonto Musterbank")
    for url in ["/accounts", f"/accounts/{giro.guid}", "/search?q=Miete", "/checkpoints", "/checkpoints?all=1",
                "/imports?all=1", "/settings", "/transactions/new", f"/transactions/new?account={giro.guid}",
                "/account/password", "/admin/users", "/admin/books"]:
        r = client.get(url)
        assert r.status_code == 200, url
        assert '<html lang="en"' in r.text, url
    assert "Deposit" in client.get(f"/accounts/{giro.guid}").text
    # validation messages from the write side are translated too
    r = client.post("/transactions/new", data={"csrf_token": client.csrf, "date": "", "description": "x"})
    assert "Please enter a valid date." in r.text
    _switch(client, "de")
    assert "Übersicht" in client.get("/").text


def test_language_switch_needs_csrf_and_known_language(client):
    assert client.post("/language/en", data={"next": "/"}).status_code == 400
    assert client.post("/language/fr", data={"csrf_token": client.csrf}).status_code == 404
    r = client.post("/language/en", data={"csrf_token": client.csrf, "next": "//evil.example/"})
    assert r.headers["Location"] == "/"


def test_login_page_offers_the_switch(app):
    c = app.test_client()
    page = c.get("/login").text
    assert "English" in page and "Deutsch" in page


def test_config_default_language(app, client):
    app.extensions["gnubook"].cfg.app.language = "en"
    assert "Overview" in client.get("/").text
    _switch(client, "de")
    assert "Übersicht" in client.get("/").text


def test_language_switch_is_not_a_get(client):
    assert client.get("/language/en").status_code == 405
    assert client.get("/lang/en").status_code == 404
