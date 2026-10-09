"""Booking a Portfolio Performance file into the GnuCash book and keeping it in sync."""
import json
import os
import subprocess
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import text

from gnubook.book import WriteLockError
from gnubook.ledger import balances, load_transaction
from gnubook.money import gnc_decimal
from gnubook.pp.settings import PPSettings
from gnubook.pp.sync import SLOT, SyncError, sync

from .pp_fixtures import A1, ETF, P1, P2, SHARE, find, make_export, without

GNUCASH_PYTHON = os.environ.get("GNUCASH_PYTHON", "/usr/bin/python3")


def _settings(**kw):
    return PPSettings(enabled=True, **kw)


def _tx(state, key):
    return state.appdb.pp_records()[key]["tx_guid"]


def _splits(state, tx_guid):
    idx = state.book.load_accounts()
    with state.book.connect() as conn:
        tx = load_transaction(conn, state.book, idx, tx_guid)
    return {(s.account.full_name, s.quantity == 0 and s.value != 0): (s.value, s.quantity) for s in tx.splits}, tx


def test_first_sync_books_everything_and_second_changes_nothing(state):
    r = sync(state, _settings(), make_export())
    assert r.ok and r.created == 14 and r.updated == r.deleted == 0, r.summary()
    assert r.accounts_created > 0 and r.commodities_created == 2
    idx = state.book.load_accounts()
    etf1 = idx.find("Aktiva:Wertpapiere:Depot Musterbank:Musterwelt Aktien ETF")
    etf2 = idx.find("Aktiva:Wertpapiere:Depot Beispielbank:Musterwelt Aktien ETF")
    share = idx.find("Aktiva:Wertpapiere:Depot Beispielbank:Muster- Industrie AG")
    assert etf1.type == etf2.type == "MUTUAL" and share.type == "STOCK"
    assert idx.find("Aktiva:Wertpapiere:Depot Musterbank").placeholder
    assert etf1.commodity.namespace == "Portfolio Performance" and etf1.commodity.mnemonic == "MWE.DE"
    assert share.commodity.fraction == 10 and share.commodity_scu == 10  # 2.5 shares need one decimal
    with state.book.connect() as conn:
        bal = balances(conn, state.book)
        cusip = conn.execute(text("SELECT cusip FROM commodities WHERE guid = :g"), {"g": etf1.commodity_guid}).scalar()
    assert cusip == "DE000MUSTER1"
    assert bal[etf1.guid] == 0 and bal[etf2.guid] == 3 and bal[share.guid] == D("42.5")
    clearing = idx.find("Aktiva:Wertpapier-Verrechnung")
    assert clearing is not None and clearing.type == "ASSET"
    # cash side: -1001.50 -600 +1500 +20 +3 -10 -4 +2 +1 -0.50 -31.25
    assert bal[clearing.guid] == D("-121.25")
    assert bal[idx.find("Erträge:Kursgewinne").guid] == D("-310.00")
    assert bal[idx.find("Erträge:Dividenden").guid] == D("-25")
    assert bal[idx.find("Aufwendungen:Kapitalertragsteuer").guid] == D("55")  # 48 + 5 + 4 - 2
    assert bal[idx.find("Aufwendungen:Wertpapiergebühren").guid] == D("12.50")  # 1.50 + 2 + 10 - 1
    assert bal[idx.find("Aufwendungen:Sollzinsen").guid] == D("0.50")
    assert bal[idx.find("Anfangsbestand").guid] != 0  # deliveries
    r2 = sync(state, _settings(), make_export())
    assert (r2.created, r2.updated, r2.deleted, r2.unchanged) == (0, 0, 0, 14) and not r2.wrote


def test_sale_transaction_in_gnucash_terms(state):
    sync(state, _settings(), make_export())
    splits, tx = _splits(state, _tx(state, "sell-1-p"))
    assert tx.description == "Verkauf Musterwelt Aktien ETF" and tx.day == date(2024, 1, 20)
    acc = "Aktiva:Wertpapiere:Depot Musterbank:Musterwelt Aktien ETF"
    assert splits[(acc, False)] == (D("-1550"), D("-12"))
    assert splits[(acc, True)] == (D("310"), D("0"))  # realised gain, like GnuCash's stock assistant
    assert splits[("Erträge:Kursgewinne", False)] == (D("-310"), D("-310"))
    assert not tx.editable  # security transactions are changed in PP (or GnuCash Desktop), not in gnubook
    with state.book.connect() as conn:
        slot = conn.execute(text("SELECT string_val FROM slots WHERE obj_guid = :g AND name = :n"),
                            {"g": tx.guid, "n": SLOT}).scalar()
        notes = conn.execute(text("SELECT string_val FROM slots WHERE obj_guid = :g AND name = 'notes'"),
                             {"g": _tx(state, "buy-1-p")}).scalar()
    assert slot == "sell-1-p"
    assert "Beleg: Kauf_2024-01-03.pdf" in notes


def test_change_in_pp_updates_the_same_transaction(state):
    sync(state, _settings(), make_export())
    guid = _tx(state, "buy-1-p")
    e = make_export(revision="rev-2")
    find(e, "buy-1-p").update(amount="1002.00", units=[{"type": "FEE", "amount": "2.00", "currency": "EUR"}])
    find(e, "buy-1-a")["amount"] = "1002.00"
    r = sync(state, _settings(), e)
    assert r.updated == 1 and r.created == 0, r.summary()
    assert _tx(state, "buy-1-p") == guid
    splits, _tx_view = _splits(state, guid)
    assert splits[("Aufwendungen:Wertpapiergebühren", False)][0] == D("2")
    assert splits[("Aktiva:Wertpapier-Verrechnung", False)][0] == D("-1002")


def test_deleted_in_pp_is_deleted_in_the_book(state):
    sync(state, _settings(), make_export())
    guid = _tx(state, "fee-1")
    r = sync(state, _settings(), without(make_export(), "fee-1"))
    assert r.deleted == 1
    assert "fee-1" not in state.appdb.pp_records()
    with state.book.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM transactions WHERE guid = :g"), {"g": guid}).scalar() == 0


def test_edit_in_gnucash_wins_until_forced(state):
    sync(state, _settings(), make_export())
    guid = _tx(state, "int-1")
    with state.book.engine.begin() as conn:
        conn.execute(text("UPDATE transactions SET description = 'Zinsen Tagesgeld' WHERE guid = :g"), {"g": guid})
    r = sync(state, _settings(), make_export())
    assert r.unchanged == 14 and state.appdb.pp_records()["int-1"]["status"] == "edited"
    e = make_export()
    find(e, "int-1")["amount"] = "3.50"
    r = sync(state, _settings(), e)
    assert r.conflicts == 1 and r.updated == 0
    assert state.appdb.pp_records()["int-1"]["status"] == "conflict"
    r = sync(state, _settings(), e, force=["int-1"])
    assert r.updated == 1
    splits, tx = _splits(state, guid)
    assert tx.description == "Zinsen" and splits[("Erträge:Zinsen", False)][0] == D("-3.50")
    assert state.appdb.pp_records()["int-1"]["status"] == "booked"


def test_reconciled_booking_is_not_changed(state):
    sync(state, _settings(), make_export())
    guid = _tx(state, "tax-1")
    with state.book.engine.begin() as conn:
        conn.execute(text("UPDATE splits SET reconcile_state = 'y' WHERE tx_guid = :g"), {"g": guid})
    e = make_export()
    find(e, "tax-1")["amount"] = "5"
    r = sync(state, _settings(), e)
    assert r.conflicts == 1 and "abgeglichen" in state.appdb.pp_records()["tax-1"]["message"]


def test_deleted_in_gnucash_is_not_recreated_unless_forced(state):
    sync(state, _settings(), make_export())
    guid = _tx(state, "taxr-1")
    with state.book.engine.begin() as conn:
        conn.execute(text("DELETE FROM splits WHERE tx_guid = :g"), {"g": guid})
        conn.execute(text("DELETE FROM slots WHERE obj_guid = :g"), {"g": guid})
        conn.execute(text("DELETE FROM transactions WHERE guid = :g"), {"g": guid})
    r = sync(state, _settings(), make_export())
    assert r.gone == 1 and r.created == 0
    assert state.appdb.pp_records()["taxr-1"]["status"] == "gone"
    r = sync(state, _settings(), make_export(), force=["taxr-1"])
    assert r.created == 1 and state.appdb.pp_records()["taxr-1"]["status"] == "booked"


def test_detached_booking_is_left_alone(state):
    sync(state, _settings(), make_export())
    state.appdb.pp_set_status("int-1", "detached", "test")
    e = make_export()
    find(e, "int-1")["amount"] = "9"
    r = sync(state, _settings(), e)
    assert r.updated == 0 and r.conflicts == 0


def test_gnucash_lock_blocks_the_write(state):
    with state.book.engine.begin() as conn:
        conn.execute(text("INSERT INTO gnclock (hostname, pid) VALUES ('desktop', 4711)"))
    with pytest.raises(WriteLockError):
        sync(state, _settings(), make_export())
    assert not state.appdb.pp_records()
    with state.book.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM slots WHERE name = :n"), {"n": SLOT}).scalar() == 0


def test_dry_run_writes_nothing(state):
    r = sync(state, _settings(), make_export(), dry_run=True)
    assert r.created == 14
    assert not state.appdb.pp_records()


def test_lost_mapping_is_rebuilt_from_the_slots(state):
    sync(state, _settings(), make_export())
    with state.appdb.conn() as c:
        c.execute("DELETE FROM pp_sync")
    r = sync(state, _settings(), make_export())
    assert r.created == 0, r.summary()
    with state.book.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM slots WHERE name = :n"), {"n": SLOT}).scalar() == 14


def _replace_book(state):
    """Like GnuCash Desktop's "Save As" over the database: another book with new GUIDs, without our bookings."""
    with state.book.engine.begin() as conn:
        txs = [g for (g,) in conn.execute(text("SELECT obj_guid FROM slots WHERE name = :n"), {"n": SLOT})]
        for g in txs:
            conn.execute(text("DELETE FROM splits WHERE tx_guid = :g"), {"g": g})
            conn.execute(text("DELETE FROM slots WHERE obj_guid = :g"), {"g": g})
            conn.execute(text("DELETE FROM transactions WHERE guid = :g"), {"g": g})
        conn.execute(text("DELETE FROM prices WHERE source = 'user:price'"))
        conn.execute(text("UPDATE books SET guid = :g"), {"g": "f" * 32})


def test_replaced_book_is_booked_again(state):
    sync(state, _settings(), make_export())
    assert state.appdb.meta("pp_book_guid")
    _replace_book(state)
    dry = sync(state, _settings(), make_export(), dry_run=True)
    assert dry.book_replaced and dry.created == 14 and dry.gone == 0
    assert state.appdb.pp_records()  # a dry run keeps the old links
    r = sync(state, _settings(), make_export())
    assert r.book_replaced and r.created == 14 and r.gone == 0, r.summary()
    assert r.prices_added > 0 and r.prices_removed == 0
    assert state.appdb.meta("pp_book_guid") == "f" * 32
    assert all(rec["status"] == "booked" for rec in state.appdb.pp_records().values())
    r2 = sync(state, _settings(), make_export())
    assert not r2.book_replaced and r2.unchanged == 14 and not r2.wrote


def test_same_book_with_deleted_bookings_is_not_treated_as_replaced(state):
    sync(state, _settings(), make_export())
    guid = _tx(state, "taxr-1")
    with state.book.engine.begin() as conn:
        conn.execute(text("DELETE FROM splits WHERE tx_guid = :g"), {"g": guid})
        conn.execute(text("DELETE FROM slots WHERE obj_guid = :g"), {"g": guid})
        conn.execute(text("DELETE FROM transactions WHERE guid = :g"), {"g": guid})
    r = sync(state, _settings(), make_export())
    assert not r.book_replaced and r.gone == 1 and r.created == 0


def test_existing_security_is_reused_by_isin(state):
    from piecash import Commodity

    with state.book.exclusive():
        pc = state.book.piecash_book()
        Commodity(namespace="XETRA", mnemonic="MWE", fullname="Musterwelt (von Hand)", fraction=1000,
                  cusip="DE000MUSTER1", book=pc)
        pc.save()
        pc.close()
    r = sync(state, _settings(), make_export())
    assert r.commodities_created == 1  # only the share
    idx = state.book.load_accounts()
    assert idx.find("Aktiva:Wertpapiere:Depot Musterbank:Musterwelt Aktien ETF").commodity.mnemonic == "MWE"


def test_start_date_books_opening_positions(state):
    r = sync(state, _settings(sync_from="2024-02-07"), make_export())
    assert r.ok
    recs = state.appdb.pp_records()
    assert "buy-1-p" not in recs and f"opening:{P2}:{ETF}" in recs
    splits, tx = _splits(state, recs[f"opening:{P2}:{ETF}"]["tx_guid"])
    assert tx.day == date(2024, 2, 6) and tx.description == "Anfangsbestand Musterwelt Aktien ETF"
    assert splits[("Aktiva:Wertpapiere:Depot Beispielbank:Musterwelt Aktien ETF", False)] == (D("360"), D("3"))


def test_prices_are_copied_in_book_currency(state):
    s = _settings()
    sync(state, s, make_export())
    idx = state.book.load_accounts()
    share = idx.find("Aktiva:Wertpapiere:Depot Beispielbank:Muster- Industrie AG").commodity
    etf = idx.find("Aktiva:Wertpapiere:Depot Musterbank:Musterwelt Aktien ETF").commodity

    def rows(cdty):
        with state.book.connect() as conn:
            return [(state.book.day_of(d), gnc_decimal(n, dn), src, typ) for d, n, dn, src, typ in conn.execute(text(
                "SELECT date, value_num, value_denom, source, type FROM prices WHERE commodity_guid = :c "
                "ORDER BY date"), {"c": cdty.guid})]

    etf_rows = [r for r in rows(etf) if r[2] == "user:price"]
    # ETF prices are older than 400 days: one per month from a week before the first purchase on
    assert [r[0].month for r in etf_rows] == [1, 2] and all(r[3] == "last" for r in etf_rows)
    share_rows = [r for r in rows(share) if r[2] == "user:price"]
    assert len(share_rows) >= 15  # daily in the last 400 days (weekdays + latest price)
    # no cost-based prices from transfers or deliveries
    assert not [r for r in rows(etf) if r[2] == "user:split-register" and r[0] == date(2024, 2, 6)]
    # a changed price is updated, prices outside the window are removed
    e = make_export()
    e["securities"][1]["prices"] = e["securities"][1]["prices"][:5]
    e["securities"][1]["prices"][0][1] = e["securities"][1]["prices"][0][2] = "99.99"
    e["securities"][1]["latest"] = None
    r = sync(state, s, e)
    assert r.prices_updated == 1 and r.prices_removed > 0
    assert [r[1] for r in rows(share) if r[2] == "user:price"][0] == D("99.99")


def test_bank_import_books_depot_lines_against_the_clearing_account(app, state, api):
    sync(state, _settings(), make_export())
    reg = app.extensions["gnubook"]
    reg.system.set_pp_settings(state.id, _settings(bank_accounts=["Aktiva:Barvermögen:Girokonto Beispielbank"]))
    fresh = reg.context(state.id)
    ids = {a["attributes"]["name"]: int(a["id"]) for a in api("GET", "/accounts?type=asset").get_json()["data"]}
    r = api("POST", "/transactions", {"transactions": [{
        "type": "withdrawal", "date": "2024-01-03", "amount": 1001.5, "description": "WERTPAPIERKAUF DE000MUSTER1",
        "source_id": ids["Aktiva:Barvermögen:Girokonto Beispielbank"], "destination_name": "Musterbank Depot"}]})
    assert r.status_code == 200, r.get_json()
    idx = fresh.book.load_accounts()
    clearing = idx.find("Aktiva:Wertpapier-Verrechnung")
    with fresh.book.connect() as conn:
        bal = balances(conn, fresh.book)
    assert bal[clearing.guid] == D("-121.25") + D("1001.50")
    rec = fresh.appdb.imports(limit=1)[0]
    assert rec["source"] == "pp"
    # other accounts keep the normal rules
    r = api("POST", "/transactions", {"transactions": [{
        "type": "withdrawal", "date": "2024-01-04", "amount": 9.99, "description": "REWE",
        "source_id": ids["Aktiva:Barvermögen:Girokonto Musterbank"], "destination_name": "REWE Markt"}]})
    assert fresh.appdb.imports(limit=1)[0]["source"] != "pp"


def test_bad_account_settings_are_rejected(state):
    with pytest.raises(SyncError):
        sync(state, _settings(accounts={"fees": "Aufwendungen:Wohnen"}), make_export())  # a placeholder


def _have_bindings():
    try:
        return subprocess.run([GNUCASH_PYTHON, "-c", "import gnucash"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _have_bindings(), reason="GnuCash Python bindings not available")
def test_gnucash_reads_the_pp_bookings(state):
    if not state.book.is_sqlite:
        pytest.skip("checked with SQLite books")
    sync(state, _settings(), make_export())
    e = make_export(revision="r2")
    find(e, "buy-1-p")["note"] = "geändert"
    sync(state, _settings(), without(e, "fee-1"))
    path = state.book.engine.url.database
    out = subprocess.run([GNUCASH_PYTHON, str(Path(__file__).with_name("gnucash_verify.py")), f"sqlite3://{path}"],
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    gnc = json.loads(out.stdout.strip().splitlines()[-1])
    idx = state.book.load_accounts()
    with state.book.connect() as conn:
        ours = balances(conn, state.book)
        n_splits = dict(conn.execute(text("SELECT account_guid, COUNT(*) FROM splits GROUP BY account_guid")).fetchall())
    for guid, acc in idx.by_guid.items():
        assert D(gnc[guid]["balance"]) == ours.get(guid, D(0)), acc.full_name
        assert gnc[guid]["splits"] == n_splits.get(guid, 0), acc.full_name
