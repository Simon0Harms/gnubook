from decimal import Decimal as D

import pytest
from sqlalchemy import text

from gnubook.demo import german_iban
from gnubook.ledger import load_transaction

MIETE_IBAN = german_iban("30030030", "0000333444")
OWN_GIRO2 = german_iban("20020020", "0987654321")
OWN_GIRO = german_iban("10010010", "0123456789")


@pytest.fixture
def ids(api):
    r = api("GET", "/accounts?type=asset&page=1&limit=250")
    assert r.status_code == 200
    return {a["attributes"]["name"]: int(a["id"]) for a in r.get_json()["data"]}


def withdrawal(account_id, day, amount, desc, name, iban=None, **extra):
    t = {"type": "withdrawal", "date": day, "amount": amount, "description": desc, "source_id": account_id,
         "destination_name": name, **extra}
    if iban:
        t["destination_iban"] = iban
    return {"apply_rules": True, "error_if_duplicate_hash": True, "transactions": [t]}


def deposit(account_id, day, amount, desc, name, iban=None, **extra):
    t = {"type": "deposit", "date": day, "amount": amount, "description": desc, "destination_id": account_id,
         "source_name": name, **extra}
    if iban:
        t["source_iban"] = iban
    return {"apply_rules": True, "error_if_duplicate_hash": True, "transactions": [t]}


def counter_of(state, tx_guid, own_name):
    idx = state.book.load_accounts()
    with state.book.connect() as conn:
        tx = load_transaction(conn, state.book, idx, tx_guid)
    return [s.account.full_name for s in tx.splits if s.account.full_name != own_name], tx


def test_auth(api):
    assert api("GET", "/accounts", token="falsch").status_code == 401
    assert api("GET", "/accounts", token="").status_code == 401


def test_accounts_format(api, ids):
    r = api("GET", "/accounts?type=asset&page=1&limit=250").get_json()
    assert r["meta"]["pagination"]["total_pages"] == 1
    a = next(x for x in r["data"] if x["attributes"]["name"] == "Aktiva:Barvermögen:Girokonto Musterbank")
    attrs = a["attributes"]
    assert a["id"].isdigit() and attrs["type"] == "asset" and attrs["currency_code"] == "EUR"
    assert isinstance(attrs["current_balance"], str) and attrs["iban"] is None  # not exposed by default
    assert "Ausgleichskonto-EUR" not in ids and not any("Aufwendungen" in n for n in ids)
    assert api("GET", "/accounts?type=expense").get_json()["data"] == []


def test_import_flow(api, ids, state):
    giro_name = "Aktiva:Barvermögen:Girokonto Musterbank"
    giro, giro2 = ids[giro_name], ids["Aktiva:Barvermögen:Girokonto Beispielbank"]

    r = api("POST", "/transactions", withdrawal(giro, "2026-10-01", 950.0, "MIETE", "Hausverwaltung Sonnenhof", MIETE_IBAN))
    assert r.status_code == 200
    data = r.get_json()["data"]
    t = data["attributes"]["transactions"][0]
    # field types the importer (strict PHP typed properties) relies on
    assert isinstance(t["amount"], str) and t["amount"] == "950.00"
    assert isinstance(t["currency_decimal_places"], int) and isinstance(t["source_name"], str)
    assert isinstance(t["destination_name"], str) and isinstance(t["tags"], list)
    guid = t["gnucash_guid"]
    others, tx = counter_of(state, guid, giro_name)
    assert others == ["Aufwendungen:Wohnen:Miete"]  # learned from the book history (IBAN in memo)
    assert tx.description == "MIETE; Hausverwaltung Sonnenhof"
    bank = next(s for s in tx.splits if s.account.full_name == giro_name)
    assert bank.memo == f"Konto {MIETE_IBAN}" and bank.value == D("-950.00")

    # GnuCash's Bayesian import map
    r = api("POST", "/transactions", withdrawal(giro, "2026-10-02", 23.45, "REWE Markt Karte", "REWE"))
    assert counter_of(state, r.get_json()["data"]["attributes"]["transactions"][0]["gnucash_guid"], giro_name)[0] == [
        "Aufwendungen:Lebensmittel"]

    # unknown counterparty -> fallback account, listed for review
    r = api("POST", "/transactions", withdrawal(giro, "2026-10-02", 9.99, "Abo", "Neu GmbH", german_iban("60060060", "1")))
    assert counter_of(state, r.get_json()["data"]["attributes"]["transactions"][0]["gnucash_guid"], giro_name)[0] == [
        "Ausgleichskonto-EUR"]

    # transfer between own accounts: created once, the other bank's line is linked, not duplicated
    r1 = api("POST", "/transactions", withdrawal(giro, "2026-10-05", 200, "Umbuchung", "Max Mustermann", OWN_GIRO2))
    r2 = api("POST", "/transactions", deposit(giro2, "2026-10-07", 200, "Umbuchung", "Max Mustermann", OWN_GIRO))
    g1 = r1.get_json()["data"]["attributes"]["transactions"][0]["gnucash_guid"]
    g2 = r2.get_json()["data"]["attributes"]["transactions"][0]["gnucash_guid"]
    assert g1 == g2
    assert counter_of(state, g1, giro_name)[0] == ["Aktiva:Barvermögen:Girokonto Beispielbank"]

    # 0,00 closing line -> single split
    r = api("POST", "/transactions", withdrawal(giro, "2026-10-31", 0, "ENTGELTABSCHLUSS **ENDSALDO** 1,00H STAND30.10.2026 1,00H", ""))
    others, tx = counter_of(state, r.get_json()["data"]["attributes"]["transactions"][0]["gnucash_guid"], giro_name)
    assert others == [] and len(tx.splits) == 1 and tx.splits[0].value == 0

    # the importer runs again over the same period -> Firefly-like duplicate error
    r = api("POST", "/transactions", withdrawal(giro, "2026-10-01", 950.0, "MIETE", "Hausverwaltung Sonnenhof", MIETE_IBAN))
    assert r.status_code == 422 and "Duplikat" in r.get_json()["errors"]["transactions.0.description"][0]

    statuses = [rec["status"] for rec in state.appdb.imports(limit=500)]
    assert statuses.count("matched") == 1 and statuses.count("created") == 5


def test_existing_bookings_are_linked_not_duplicated(api, ids, state):
    """First import after switching from GnuCash's own online banking: bank lines already in the book."""
    giro_name = "Aktiva:Barvermögen:Girokonto Musterbank"
    idx = state.book.load_accounts()
    with state.book.connect() as conn:
        row = conn.execute(text(
            "SELECT t.guid, t.post_date FROM transactions t JOIN splits s ON s.tx_guid = t.guid "
            "WHERE s.account_guid = :a AND t.description LIKE 'DAUERAUFTRAG MIETE%' ORDER BY t.post_date DESC LIMIT 1"),
            {"a": idx.find(giro_name).guid}).fetchone()
    day = state.book.day_of(row[1]).isoformat()
    n_before = _count_tx(state)
    r = api("POST", "/transactions", withdrawal(ids[giro_name], day, 950, "MIETE", "Hausverwaltung Sonnenhof", MIETE_IBAN))
    assert r.status_code == 200 and r.get_json()["data"]["attributes"]["transactions"][0]["gnucash_guid"] == row[0]
    assert _count_tx(state) == n_before


def test_same_line_sent_again_much_later_is_a_duplicate(api, ids, state):
    giro = ids["Aktiva:Barvermögen:Girokonto Musterbank"]
    body = withdrawal(giro, "2026-10-09", 3.5, "Bäckerei Krume", "Bäckerei Krume")
    assert api("POST", "/transactions", body).status_code == 200
    with state.appdb.conn() as c:  # pretend the last request was 10 minutes ago
        c.execute("UPDATE import_state SET last_at = '2000-01-01T00:00:00+00:00'")
    assert api("POST", "/transactions", body).status_code == 422


def test_identical_lines_in_a_row(api, ids, state):
    """Two identical bank lines (same day, amount, text) are both imported – and both recognised later."""
    giro = ids["Aktiva:Barvermögen:Girokonto Musterbank"]
    body = withdrawal(giro, "2026-10-09", 3.5, "Bäckerei Krume", "Bäckerei Krume")
    n = _count_tx(state)
    assert api("POST", "/transactions", body).status_code == 200
    assert api("POST", "/transactions", body).status_code == 200
    assert _count_tx(state) == n + 2
    other = withdrawal(giro, "2026-10-09", 1.0, "anderes", "x")
    assert api("POST", "/transactions", other).status_code == 200
    # the importer runs again over the same period
    assert api("POST", "/transactions", body).status_code == 422
    assert api("POST", "/transactions", body).status_code == 422
    assert api("POST", "/transactions", other).status_code == 422
    assert _count_tx(state) == n + 3


def test_rejections(api, ids, state):
    giro = ids["Aktiva:Barvermögen:Girokonto Musterbank"]
    cases = [
        (withdrawal(999, "2026-10-01", 1, "x", "y"), "transactions.0.source_id"),
        (withdrawal(giro, "2026-13-01", 1, "x", "y"), "transactions.0.date"),
        (withdrawal(giro, "2026-10-01", -1, "x", "y"), "transactions.0.amount"),
        (withdrawal(giro, "2026-10-01", 1, "x", "y", currency_code="USD"), "transactions.0.currency_code"),
        ({"transactions": []}, "transactions.0.transactions"),
    ]
    for body, field in cases:
        r = api("POST", "/transactions", body)
        assert r.status_code == 422, body
        assert field in r.get_json()["errors"] or field.endswith(".transactions")
    with state.book.engine.begin() as conn:
        conn.execute(text("INSERT INTO gnclock (hostname, pid) VALUES ('desktop-pc', 1)"))
    r = api("POST", "/transactions", withdrawal(giro, "2026-10-01", 1, "x", "y"))
    assert r.status_code == 422 and "GnuCash" in r.get_json()["message"]


def _count_tx(state):
    with state.book.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM transactions")).scalar()


def test_similarity_keeps_short_numbers():
    from gnubook.importer import similarity
    old5 = "ECHTZEITÜBERWEISUNG SPARPLAN ETF 5; MAX MUSTERMANN"
    old7 = "ECHTZEITÜBERWEISUNG SPARPLAN ETF 7; MAX MUSTERMANN"
    new = "SPARPLAN ETF 5; MAX MUSTERMANN"
    assert similarity(new, old5) > similarity(new, old7) >= 0.5


def test_absurd_amount_rejected(api, ids):
    giro = ids["Aktiva:Barvermögen:Girokonto Musterbank"]
    r = api("POST", "/transactions", withdrawal(giro, "2026-10-01", 1e15, "x", "y"))
    assert r.status_code == 422 and "transactions.0.amount" in r.get_json()["errors"]
