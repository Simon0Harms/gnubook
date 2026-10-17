"""Pause of the book connection (so that GnuCash Desktop can re-create the database) and idle connections."""
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from gnubook.book import Book, BookPaused


def _pause(client, minutes="15"):
    return client.post("/settings/pause", data={"minutes": minutes, "csrf_token": client.csrf})


def test_pause_blocks_every_connection_until_resumed(client, api, state):
    r = _pause(client)
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings/paused")

    page = client.get("/settings/paused")
    assert page.status_code == 503
    assert "pausiert" in page.text and "tester" in page.text

    # every page that needs the book shows the pause instead of an error
    r = client.get("/")
    assert r.status_code == 503 and "Verbindung fortsetzen" in r.text
    r = api("GET", "/accounts?type=asset")
    assert r.status_code == 503 and "pausiert" in r.get_json()["message"]

    # no new connection is possible, not even directly on the engine (backup thread, jobs)
    with pytest.raises(BookPaused):
        with state.book.engine.connect() as c:
            c.execute(text("SELECT 1"))

    r = client.post("/settings/resume", data={"csrf_token": client.csrf})
    assert r.status_code == 302
    assert client.get("/").status_code == 200
    assert api("GET", "/accounts?type=asset").status_code == 200


def test_pause_is_shared_through_system_db(app, client):
    """A second process (timer job) builds its own Book – it must respect the pause as well."""
    reg = app.extensions["gnubook"]
    book_id = reg.system.books()[0]["id"]
    _pause(client)
    row = reg.system.book(book_id)
    other = Book(row["url"], row["timezone"])
    other.pause_check = reg._pause_checker(book_id)
    with pytest.raises(BookPaused):
        other.load_accounts()
    reg.system.resume_book(book_id)
    other.pause_check = reg._pause_checker(book_id)
    assert other.load_accounts().root is not None
    other.dispose()


def test_pause_ends_by_itself(app, client):
    reg = app.extensions["gnubook"]
    book_id = reg.system.books()[0]["id"]
    _pause(client)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with reg.system.conn() as c:
        c.execute("UPDATE books SET paused_until = ? WHERE id = ?", (past, book_id))
    assert reg.system.book_pause(book_id) is None
    reg.context(book_id).book.pause_check.reset()
    assert client.get("/").status_code == 200
    # the paused page sends back to the settings once the pause is over
    assert client.get("/settings/paused").status_code == 302


def test_unknown_duration_falls_back_to_30_minutes(app, client):
    reg = app.extensions["gnubook"]
    book_id = reg.system.books()[0]["id"]
    _pause(client, minutes="99999")
    p = reg.system.book_pause(book_id)
    left = p["until"] - datetime.now(timezone.utc)
    assert timedelta(minutes=29) < left <= timedelta(minutes=30)


def test_settings_page_offers_the_pause(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Verbindung pausieren" in r.text


def test_close_idle_disposes_unused_pool(book_url):
    if book_url.startswith("sqlite"):
        pytest.skip("SQLite keeps no pooled connections")
    book = Book(book_url, idle_timeout=1)
    with book.connect() as c:
        c.execute(text("SELECT 1"))
    calls = []
    real_dispose = book.engine.dispose
    book.engine.dispose = lambda *a, **k: calls.append(1)
    book.close_idle()
    assert not calls  # used less than idle_timeout ago
    book._last_use = time.monotonic() - 5
    assert book.engine.pool.checkedin() == 1
    book.close_idle()
    assert calls == [1]
    with book.connect():  # a connection in use is never closed
        book._last_use = time.monotonic() - 5
        calls.clear()
        book.close_idle()
        assert not calls
    real_dispose()


def test_idle_connections_are_closed_on_the_server(book_url):
    """End to end on PostgreSQL: after idle_timeout no session of gnubook is left on the database."""
    if book_url.startswith("sqlite"):
        pytest.skip("PostgreSQL only")
    from sqlalchemy.engine import make_url

    book = Book(book_url, idle_timeout=1)
    book.load_accounts()
    dbname = make_url(book_url).database
    admin = Book(make_url(book_url).set(database="postgres").render_as_string(hide_password=False))

    def sessions():
        with admin.connect() as c:
            return c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = :d"), {"d": dbname}).scalar()

    assert sessions() >= 1
    deadline = time.monotonic() + 15
    while sessions() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert sessions() == 0
    assert book.load_accounts().root is not None  # reconnects on demand
    book.dispose()
    admin.dispose()


def test_backup_job_skips_a_paused_book(app, cfg, tmp_path):
    from click.testing import CliRunner

    from gnubook.cli import main

    reg = app.extensions["gnubook"]
    book_id = reg.system.books()[0]["id"]
    reg.system.pause_book(book_id, 15, "tester")
    config = tmp_path / "config.toml"
    config.write_text(f'[app]\ndata_dir = "{cfg.app.data_dir}"\nsecret_key = "{"x" * 40}"\n'
                      f'[book]\nurl = "{cfg.book.url}"\n', encoding="utf-8")
    out_dir = tmp_path / "bk"
    r = CliRunner().invoke(main, ["--config", str(config), "backup", "--dir", str(out_dir)])
    assert r.exit_code == 0, r.output
    assert "übersprungen" in r.output
    assert not out_dir.exists() or not any(out_dir.iterdir())
    reg.system.resume_book(book_id)
    r = CliRunner().invoke(main, ["--config", str(config), "backup", "--dir", str(out_dir)])
    assert r.exit_code == 0, r.output
    assert "Gesichert" in r.output
