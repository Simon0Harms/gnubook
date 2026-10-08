"""Creating books in PostgreSQL. Needs GNUBOOK_TEST_PG_ADMIN_URL (role with CREATEROLE + CREATEDB)."""
import io
import os
import re

import pytest
from sqlalchemy import create_engine, text

from gnubook import create_app
from gnubook.demo import create_demo_book
from gnubook.ledger import balances

from .test_multiuser import _login
from .conftest import PASSWORD

ADMIN = os.environ.get("GNUBOOK_TEST_PG_ADMIN_URL", "")
pytestmark = pytest.mark.skipif(not ADMIN, reason="GNUBOOK_TEST_PG_ADMIN_URL not set")


@pytest.fixture
def papp(cfg):
    cfg.postgres.admin_url = ADMIN
    app = create_app(cfg)
    app.testing = True
    yield app
    reg = app.extensions["gnubook"]
    for b in reg.system.books():
        if b["managed_db"]:
            reg.remove_book(b["id"], drop_database=True)
    reg.dispose()


def _exists(name):
    eng = create_engine(ADMIN)
    with eng.connect() as c:
        r = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar()
    eng.dispose()
    return bool(r)


def test_create_user_with_own_book_and_drop(papp):
    reg = papp.extensions["gnubook"]
    admin = _login(papp, "tester", PASSWORD)
    r = admin.post("/admin/users", data={"csrf_token": admin.csrf, "username": "berta", "password": "berta-passwort",
                                         "own_book": "1", "content": "simple"})
    assert r.status_code == 302
    page = admin.get("/admin/books").text
    assert "nur jetzt sichtbar" in page and "gnucash_berta" in page
    pw = re.search(r"Passwort</th><td[^>]*>([^<]+)<", page).group(1)
    assert "nur jetzt sichtbar" not in admin.get("/admin/books").text  # shown once
    book = next(b for b in reg.system.books() if b["name"] == "berta")
    assert book["managed_db"] == "gnucash_berta" and _exists("gnucash_berta")
    # the new role can log in with the shown password and sees a GnuCash book
    ctx = reg.context(book["id"])
    assert pw in ctx.book.url and ctx.book.schema_info()["supported"]
    assert ctx.book.load_accounts().find("Aktiva:Barvermögen:Girokonto") is not None
    berta = _login(papp, "berta", "berta-passwort")
    assert "berta" in berta.get("/").text
    # dropping needs the exact name
    r = admin.post(f"/admin/books/{book['id']}", data={"csrf_token": admin.csrf, "action": "drop", "confirm_name": "x"},
                   follow_redirects=True)
    assert "genau eintippen" in r.text and _exists("gnucash_berta")
    r = admin.post(f"/admin/books/{book['id']}", data={"csrf_token": admin.csrf, "action": "drop",
                                                       "confirm_name": "berta"}, follow_redirects=True)
    assert "Datenbank gelöscht" in r.text and not _exists("gnucash_berta")
    assert list((reg.data / "backup" / "deleted").glob("berta-*.gnucash"))


def test_upload_sqlite_book(papp, tmp_path):
    reg = papp.extensions["gnubook"]
    src = create_demo_book(str(tmp_path / "up.gnucash"), seed=3)
    admin = _login(papp, "tester", PASSWORD)
    data = {"csrf_token": admin.csrf, "name": "Upload", "content": "upload", "backup": "1",
            "file": (io.BytesIO(open(tmp_path / "up.gnucash", "rb").read()), "up.gnucash")}
    r = admin.post("/admin/books/new", data=data, content_type="multipart/form-data")
    assert r.status_code == 302
    book = next(b for b in reg.system.books() if b["name"] == "Upload")
    ctx = reg.context(book["id"])
    from gnubook.book import Book
    orig = Book(src)
    with orig.connect() as c1, ctx.book.connect() as c2:
        assert balances(c1, orig) == balances(c2, ctx.book)
    orig.dispose()
    # an XML/garbage file is refused and nothing is left behind
    data = {"csrf_token": admin.csrf, "name": "Kaputt", "content": "upload",
            "file": (io.BytesIO(b"<?xml version='1.0'?><gnc-v2/>"), "x.gnucash")}
    r = admin.post("/admin/books/new", data=data, content_type="multipart/form-data", follow_redirects=True)
    assert "SQLite-Format" in r.text and not _exists("gnucash_kaputt")
