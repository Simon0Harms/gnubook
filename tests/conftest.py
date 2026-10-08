import hashlib
import os
import re
import uuid

import pytest
from werkzeug.security import generate_password_hash

from gnubook import create_app
from gnubook.config import Config
from gnubook.demo import create_demo_book

PASSWORD = "test-passwort-123"
TOKEN = "test-token-xyz"


def _pg_url():
    """GNUBOOK_TEST_PG_URL=postgresql://user:pw@host:5432 (user needs CREATEDB) enables PostgreSQL runs."""
    return os.environ.get("GNUBOOK_TEST_PG_URL", "").rstrip("/")


@pytest.fixture(params=["sqlite"] + (["postgresql"] if _pg_url() else []))
def book_url(request, tmp_path):
    if request.param == "sqlite":
        yield create_demo_book(str(tmp_path / "demo.gnucash"))
        return
    url = f"{_pg_url()}/gnubook_test_{uuid.uuid4().hex[:8]}"
    created = create_demo_book(url)
    yield created
    from sqlalchemy_utils import drop_database
    drop_database(created)


@pytest.fixture
def cfg(tmp_path, book_url):
    c = Config()
    c.book.url = book_url
    c.app.secret_key = "x" * 40
    c.app.data_dir = str(tmp_path / "data")
    c.app.username = "tester"
    c.app.password_hash = generate_password_hash(PASSWORD)
    c.api.token_sha256 = hashlib.sha256(TOKEN.encode()).hexdigest()
    return c


@pytest.fixture
def app(cfg):
    application = create_app(cfg)
    application.testing = True
    yield application
    application.extensions["gnubook"].dispose()


@pytest.fixture
def state(app):
    """Context of the first book (created from the single-user config by the bootstrap)."""
    reg = app.extensions["gnubook"]
    return reg.context(reg.system.books()[0]["id"])


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture
def client(app):
    c = app.test_client()
    page = c.get("/login")
    r = c.post("/login", data={"username": "tester", "password": PASSWORD, "csrf_token": csrf_from(page.text)})
    assert r.status_code == 302
    c.csrf = csrf_from(c.get("/").text)
    return c


@pytest.fixture
def api(app):
    c = app.test_client()

    def call(method, path, body=None, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        return c.open("/api/v1" + path, method=method, json=body, headers=headers)
    return call
