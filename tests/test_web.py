import re
from decimal import Decimal as D

from sqlalchemy import text

from gnubook.ledger import balances, load_transaction

from .conftest import PASSWORD, csrf_from


def _giro(state):
    return state.book.load_accounts().find("Aktiva:Barvermögen:Girokonto Musterbank")


def test_login_required_and_csrf(app):
    c = app.test_client()
    r = c.get("/accounts")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    page = c.get("/login")
    assert c.post("/login", data={"username": "tester", "password": PASSWORD}).status_code == 400  # no CSRF token
    r = c.post("/login", data={"username": "tester", "password": "falsch", "csrf_token": csrf_from(page.text)})
    assert r.status_code == 401
    r = c.post("/login", data={"username": "tester", "password": PASSWORD, "csrf_token": csrf_from(page.text),
                               }, query_string={"next": "//evil.example/"})
    assert r.headers["Location"] == "/"


def test_security_headers(client):
    r = client.get("/")
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"


def test_pages_render(client, state):
    giro = _giro(state)
    for url in ["/", "/accounts", "/accounts?hidden=1", f"/accounts/{giro.guid}", f"/accounts/{giro.guid}?q=REWE",
                f"/accounts/{giro.guid}?page=2", "/search?q=Miete", "/search?q=950,00", "/checkpoints",
                "/checkpoints?all=1", "/imports", "/imports?all=1", "/settings", "/transactions/new",
                f"/transactions/new?account={giro.guid}"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert client.get("/accounts/doesnotexist").status_code == 404
    assert "1 offen" in client.get("/").text


def _form(csrf, date, desc, rows, **extra):
    data = {"csrf_token": csrf, "date": date, "description": desc, "num": "", "notes": ""}
    for i, (acc, debit, credit) in enumerate(rows):
        data[f"split-{i}-account"] = acc
        data[f"split-{i}-debit"] = debit
        data[f"split-{i}-credit"] = credit
        data[f"split-{i}-memo"] = ""
    data.update(extra)
    return data


def test_create_edit_delete_via_forms(client, state):
    idx = state.book.load_accounts()
    giro = idx.find("Aktiva:Barvermögen:Girokonto Musterbank")
    food = idx.find("Aufwendungen:Lebensmittel")
    home = idx.find("Aufwendungen:Haushalt")
    with state.book.connect() as conn:
        before = balances(conn, state.book)[giro.guid]

    # unbalanced -> rejected with message
    r = client.post("/transactions/new", data=_form(client.csrf, "2026-10-03", "x", [(giro.guid, "", "10"), (food.guid, "9", "")]))
    assert r.status_code == 422 and "nicht ausgeglichen" in r.text
    # missing CSRF token
    assert client.post("/transactions/new", data={"date": "2026-10-03"}).status_code == 400

    r = client.post("/transactions/new", data=_form(client.csrf, "03.10.2026", "Wochenmarkt",
                                                     [(giro.guid, "", "12,50+4,30"), (food.guid, "12,50", ""),
                                                      (home.guid, "4,30", "")]))
    assert r.status_code == 302
    guid = r.headers["Location"].rsplit("/", 1)[-1]
    with state.book.connect() as conn:
        assert balances(conn, state.book)[giro.guid] == before - D("16.80")
        tx = load_transaction(conn, state.book, idx, guid)
    assert tx.description == "Wochenmarkt" and len(tx.splits) == 3
    page = client.get(f"/transactions/{guid}/edit")
    assert page.status_code == 200 and tx.fingerprint in page.text

    bank = next(s for s in tx.splits if s.account_guid == giro.guid)
    data = _form(client.csrf, "2026-10-03", "Wochenmarkt (geändert)", [(giro.guid, "", "20"), (food.guid, "20", "")],
                 fingerprint=tx.fingerprint)
    data["split-0-guid"] = bank.guid
    r = client.post(f"/transactions/{guid}/edit", data=data)
    assert r.status_code == 302
    # the same (now stale) form again -> conflict
    r = client.post(f"/transactions/{guid}/edit", data=data)
    assert r.status_code == 422 and "inzwischen" in r.text
    with state.book.connect() as conn:
        tx2 = load_transaction(conn, state.book, idx, guid)
    assert len(tx2.splits) == 2 and any(s.guid == bank.guid for s in tx2.splits)

    r = client.post(f"/transactions/{guid}/delete", data={"csrf_token": client.csrf, "fingerprint": tx2.fingerprint})
    assert r.status_code == 302
    with state.book.connect() as conn:
        assert balances(conn, state.book)[giro.guid] == before
    actions = [a["action"] for a in state.appdb.audit_log()]
    assert actions[:3] == ["delete", "update", "create"]


def test_checkpoint_warning_and_acceptance(client, state):
    idx = state.book.load_accounts()
    giro = idx.find("Aktiva:Barvermögen:Girokonto Musterbank")
    food = idx.find("Aufwendungen:Lebensmittel")
    # a booking in an already checked month breaks the following statements
    r = client.post("/transactions/new", data=_form(client.csrf, "2026-09-15", "vergessen", [(giro.guid, "", "5"), (food.guid, "5", "")]),
                    follow_redirects=True)
    assert "Saldo-Prüfpunkt weicht ab" in r.text
    page = client.get("/checkpoints").text
    tx_guids = re.findall(r'name="tx" value="([0-9a-f]{32})"', page)
    assert tx_guids
    r = client.post("/checkpoints/accept", data={"csrf_token": client.csrf, "account": giro.guid, "tx": tx_guids[0],
                                                 "note": "Test"}, follow_redirects=True)
    assert "akzeptiert" in r.text


def test_locked_book_blocks_forms(client, state):
    with state.book.engine.begin() as conn:
        conn.execute(text("INSERT INTO gnclock (hostname, pid) VALUES ('desktop-pc', 1)"))
    page = client.get("/transactions/new").text
    assert "GnuCash Desktop hat das Buch geöffnet" in page
    idx = state.book.load_accounts()
    giro, food = idx.find("Aktiva:Barvermögen:Girokonto Musterbank"), idx.find("Aufwendungen:Lebensmittel")
    r = client.post("/transactions/new", data=_form(client.csrf, "2026-10-03", "x", [(giro.guid, "", "1"), (food.guid, "1", "")]))
    assert r.status_code == 422 and "gesperrt" in r.text
    r = client.post("/settings/unlock", data={"csrf_token": client.csrf, "confirm": "yes"}, follow_redirects=True)
    assert "entfernt" in r.text and not state.book.is_locked()


def test_imports_needing_attention(client, state, api):
    idx = state.book.load_accounts()
    giro = idx.find("Aktiva:Barvermögen:Girokonto Musterbank")
    ids = {a["attributes"]["name"]: int(a["id"]) for a in api("GET", "/accounts?type=asset").get_json()["data"]}
    body = {"transactions": [{"type": "withdrawal", "date": "2026-10-02", "amount": 9.99, "description": "Abo",
                              "source_id": ids[giro.full_name], "destination_name": "Neu GmbH"}]}
    r = api("POST", "/transactions", body)
    guid = r.get_json()["data"]["attributes"]["transactions"][0]["gnucash_guid"]
    rewe = {"transactions": [{"type": "withdrawal", "date": "2026-10-03", "amount": 5, "description": "REWE Markt",
                              "source_id": ids[giro.full_name], "destination_name": "REWE"}]}
    assert api("POST", "/transactions", rewe).status_code == 200
    page = client.get("/imports").text
    assert "Abo; Neu GmbH" in page and "REWE Markt" not in page  # only the one on the fallback account
    assert "1 importierte Buchung(en)" in client.get("/").text
    # assign the right account in the editor -> no longer listed
    with state.book.connect() as conn:
        tx = load_transaction(conn, state.book, idx, guid)
    bank = next(s for s in tx.splits if s.account_guid == giro.guid)
    other = next(s for s in tx.splits if s.account_guid != giro.guid)
    data = _form(client.csrf, "2026-10-02", tx.description,
                 [(giro.guid, "", "9,99"), (idx.find("Aufwendungen:Freizeit").guid, "9,99", "")],
                 fingerprint=tx.fingerprint)
    data["split-0-guid"], data["split-1-guid"] = bank.guid, other.guid
    assert client.post(f"/transactions/{guid}/edit", data=data).status_code == 302
    assert "Abo; Neu GmbH" not in client.get("/imports").text
    assert "Abo; Neu GmbH" in client.get("/imports?all=1").text


def test_chart_of_accounts_type_indicators(client):
    html = client.get("/accounts").text
    assert 'data-group="asset"' in html and 'data-group="expense"' in html
    assert 'class="type-badge t-asset"' in html
    assert 'data-type-group="income"' in html  # type filter chip
    assert "group-card t-asset" in html  # class summary
