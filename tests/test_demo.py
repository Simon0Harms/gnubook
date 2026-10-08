from pathlib import Path

from gnubook import create_app

from .conftest import PASSWORD, csrf_from


def _demo_app(cfg):
    cfg.app.demo = True
    app = create_app(cfg)
    app.testing = True
    return app


def _start_demo(app):
    c = app.test_client()
    page = c.get("/login")
    assert "Demo ansehen" in page.text
    r = c.post("/demo", data={"csrf_token": csrf_from(page.text)})
    assert r.status_code == 302, r.text
    c.csrf = csrf_from(c.get("/").text)
    return c


def test_demo_hidden_and_404_when_off(cfg):
    cfg.app.demo = False
    app = create_app(cfg)
    c = app.test_client()
    page = c.get("/login")
    assert "Demo ansehen" not in page.text
    assert c.post("/demo", data={"csrf_token": csrf_from(page.text)}).status_code == 404
    app.extensions["gnubook"].dispose()


def test_demo_is_shared_and_read_only(cfg):
    app = _demo_app(cfg)
    reg = app.extensions["gnubook"]
    try:
        a = _start_demo(app)
        b = _start_demo(app)
        # one shared demo user with one book
        demo = reg.system.demo_users()
        assert len(demo) == 1
        books = reg.system.user_books(demo[0]["id"])
        assert len(books) == 1 and books[0]["name"] == "Demo"
        assert Path(books[0]["url"][len("sqlite:///"):]).exists()
        assert [u["username"] for u in reg.system.users()] == ["tester"]
        home = a.get("/")
        assert home.status_code == 200 and "nur zum Ansehen" in home.text
        assert "Girokonto Musterbank" in a.get("/accounts").text
        # every change is refused
        ctx = reg.context(books[0]["id"])
        before = ctx.book.engine.connect().exec_driver_sql("SELECT COUNT(*) FROM transactions").scalar()
        r = b.post("/transactions/new", data={"csrf_token": b.csrf, "description": "x"},
                   headers={"Referer": "http://localhost/accounts"})
        assert r.status_code == 302 and r.headers["Location"].endswith("/accounts")
        assert "schreibgeschützt" in b.get("/accounts").text
        after = ctx.book.engine.connect().exec_driver_sql("SELECT COUNT(*) FROM transactions").scalar()
        assert before == after
        for path in ("/settings/token", "/account/password", "/settings/nextcloud/connect"):
            assert b.post(path, data={"csrf_token": b.csrf}).status_code in (302, 403)
        assert reg.system.tokens(books[0]["id"]) == []
        assert b.get("/settings/nextcloud/folders").status_code == 403
        assert b.get("/admin/users").status_code == 403
        main_book = next(x for x in reg.system.books() if x["name"] == "Hauptbuch")
        assert b.post(f"/book/{main_book['id']}", data={"csrf_token": b.csrf}).status_code == 403
        # language switch still works (cookie only, nothing stored for the shared user)
        r = b.post("/account/language", data={"csrf_token": b.csrf, "language": "en"})
        assert r.status_code == 302 and "gnubook_lang=en" in r.headers.get("Set-Cookie", "")
        assert reg.system.user(demo[0]["id"])["language"] == ""
        # logout keeps the demo for the others
        assert a.post("/logout", data={"csrf_token": a.csrf}).status_code == 302
        assert b.get("/").status_code == 200
        # the real login still works
        t = app.test_client()
        page = t.get("/login")
        assert t.post("/login", data={"username": "tester", "password": PASSWORD,
                                      "csrf_token": csrf_from(page.text)}).status_code == 302
    finally:
        reg.dispose()


def test_demo_book_rebuilt_for_new_month(cfg):
    app = _demo_app(cfg)
    reg = app.extensions["gnubook"]
    try:
        user = reg.demo_user()
        book = reg.system.user_books(user["id"])[0]
        reg.system.update_book(book["id"], book["name"], "sqlite:////nonexistent/demo-200001.gnucash",
                               book["timezone"], "")
        reg.demo_user()
        assert reg.system.user_books(user["id"])[0]["url"] == book["url"]
        assert len(reg.system.demo_users()) == 1
    finally:
        reg.dispose()


def test_demo_writable_when_configured(cfg):
    cfg.app.demo_writable = True
    app = _demo_app(cfg)
    reg = app.extensions["gnubook"]
    try:
        c = _start_demo(app)
        home = c.get("/")
        assert "für alle sichtbar" in home.text
        book = reg.system.user_books(reg.system.demo_users()[0]["id"])[0]
        ctx = reg.context(book["id"])
        tx = ctx.book.engine.connect().exec_driver_sql("SELECT guid FROM transactions LIMIT 1").scalar()
        from gnubook.ledger import load_transaction

        with ctx.book.connect() as conn:
            fp = load_transaction(conn, ctx.book, ctx.book.load_accounts(), tx).fingerprint
        r = c.post(f"/transactions/{tx}/delete", data={"csrf_token": c.csrf, "fingerprint": fp})
        assert r.status_code == 302
        assert ctx.book.engine.connect().exec_driver_sql(
            "SELECT COUNT(*) FROM transactions WHERE guid = ?", (tx,)).scalar() == 0
        # still never: password, API tokens, Nextcloud
        r = c.post("/account/password", data={"csrf_token": c.csrf, "old": "x", "new": "y" * 12, "new2": "y" * 12})
        assert r.status_code == 302 and "schreibgeschützt" in c.get("/").text
        assert c.post("/settings/token", data={"csrf_token": c.csrf, "label": "x"}).status_code == 403
        assert c.post("/settings/nextcloud/connect", data={"csrf_token": c.csrf}).status_code == 403
        assert reg.system.tokens(book["id"]) == []
    finally:
        reg.dispose()
