import sqlite3

from gnubook import create_app
from gnubook.demo import create_demo_book

from .conftest import PASSWORD, csrf_from


def _login(app, user, pw):
    c = app.test_client()
    page = c.get("/login")
    r = c.post("/login", data={"username": user, "password": pw, "csrf_token": csrf_from(page.text)})
    assert r.status_code == 302, r.text
    c.csrf = csrf_from(c.get("/account/password").text)
    return c


def _setup(app, tmp_path):
    reg = app.extensions["gnubook"]
    url2 = create_demo_book(str(tmp_path / "anna.gnucash"), seed=11, with_deviation=False)
    admin = _login(app, "tester", PASSWORD)
    r = admin.post("/admin/books", data={"csrf_token": admin.csrf, "name": "Anna", "url": url2,
                                         "timezone": "Europe/Berlin", "backup": "1"})
    assert r.status_code == 302
    anna_book = next(b for b in reg.system.books() if b["name"] == "Anna")
    r = admin.post("/admin/users", data={"csrf_token": admin.csrf, "username": "anna", "password": "anna-passwort-1",
                                         "books": str(anna_book["id"])})
    assert r.status_code == 302
    return reg, admin, anna_book


def test_users_see_only_their_books(app, tmp_path, state):
    reg, admin, anna_book = _setup(app, tmp_path)
    main_book = state.id
    anna = _login(app, "anna", "anna-passwort-1")
    assert "Anna" in anna.get("/").text
    # anna may not switch to the main book, nor administrate
    assert anna.post(f"/book/{main_book}", data={"csrf_token": anna.csrf}).status_code == 403
    assert anna.get("/admin/users").status_code == 403
    assert anna.get("/admin/books").status_code == 403
    # the admin has both books (creator is added automatically) and can switch
    assert admin.post(f"/book/{anna_book['id']}", data={"csrf_token": admin.csrf}).status_code == 302
    assert "Buch wechseln" in admin.get("/").text
    # each book has its own account list
    acc_main = state.book.load_accounts().find("Aktiva:Barvermögen:Girokonto Musterbank").guid
    assert anna.get(f"/accounts/{acc_main}").status_code == 404
    # backup file configured for the new book
    assert reg.system.book(anna_book["id"])["backup_file"].endswith("anna.gnucash")


def test_api_token_is_bound_to_one_book(app, tmp_path, state):
    reg, admin, anna_book = _setup(app, tmp_path)
    anna_user = reg.system.user_by_name("anna")
    token = reg.system.create_token(anna_user["id"], anna_book["id"], "test")
    c = app.test_client()
    h = {"Authorization": f"Bearer {token}"}
    names = [a["attributes"]["name"] for a in c.get("/api/v1/accounts?type=asset", headers=h).get_json()["data"]]
    assert "Aktiva:Barvermögen:Girokonto Musterbank" in names
    ctx = reg.context(anna_book["id"])
    giro = ctx.appdb.account_id(ctx.book.load_accounts().find("Aktiva:Barvermögen:Girokonto Musterbank").guid)
    body = {"transactions": [{"type": "withdrawal", "date": "2026-10-02", "amount": 5, "description": "Bäcker",
                              "source_id": giro, "destination_name": "Bäckerei"}]}
    n_main = len(state.appdb.imports(limit=100))
    assert c.post("/api/v1/transactions", json=body, headers=h).status_code == 200
    assert len(ctx.appdb.imports(limit=100)) == 1 and len(state.appdb.imports(limit=100)) == n_main
    # revoking the user's access to the book invalidates the token
    reg.system.set_book_users(anna_book["id"], [])
    assert c.get("/api/v1/accounts", headers=h).status_code == 401


def test_password_change_and_last_admin(app, tmp_path):
    reg, admin, _ = _setup(app, tmp_path)
    r = admin.post("/account/password", data={"csrf_token": admin.csrf, "old": PASSWORD, "new": "ganz-neues-pw-1",
                                              "new2": "ganz-neues-pw-1"})
    assert r.status_code == 302
    _login(app, "tester", "ganz-neues-pw-1")
    me = reg.system.user_by_name("tester")
    r = admin.post(f"/admin/users/{me['id']}", data={"csrf_token": admin.csrf, "action": "save", "active": "1"},
                   follow_redirects=True)
    assert "mindestens ein aktiver Administrator" in r.text


def test_takeover_of_single_user_setup(cfg, tmp_path):
    """Old config with [app] username/password_hash and data/gnubook.sqlite keeps working."""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(data / "gnubook.sqlite") as c:
        c.execute("CREATE TABLE marker (x)")
    app = create_app(cfg)
    reg = app.extensions["gnubook"]
    assert [u["username"] for u in reg.system.users()] == ["tester"]
    book = reg.system.books()[0]
    assert (data / "books" / f"{book['id']}.sqlite").exists() and not (data / "gnubook.sqlite").exists()
    reg.dispose()
