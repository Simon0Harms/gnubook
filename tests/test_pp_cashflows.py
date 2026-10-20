"""Deposits and removals from the GnuCash book into the Portfolio Performance file (pp/cashflows.py)."""
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from gnubook.pp import cashflows
from gnubook.pp.settings import PPSettings
from gnubook.pp.sync import sync

from .pp_fixtures import A1, FakePPCore, fake, make_export, pp_app  # noqa: F401  (fixtures)

DEPOT = "Aktiva:Depotkonto Musterbank"
GIRO = "Aktiva:Barvermögen:Girokonto Musterbank"


def _settings(**kw):
    kw.setdefault("cash_accounts", {A1: DEPOT})
    return PPSettings(enabled=True, cashflows=True, **kw)


def _account(state, full_name, typ="BANK"):
    """Create an account (parents must exist) and return the fresh index."""
    from piecash import Account

    parent_name, _sep, name = full_name.rpartition(":")
    idx = state.book.load_accounts()
    with state.book.exclusive():
        pc = state.book.piecash_book()
        try:
            parent = pc.session.query(Account).filter_by(guid=idx.find(parent_name).guid).one()
            Account(name=name, type=typ, parent=parent, commodity=pc.default_currency)
            pc.save()
        finally:
            pc.close()
    return state.book.load_accounts()


def _book(state, day, description, *splits):
    """A GnuCash transaction: splits = (account full name, value)."""
    from piecash import Account, Split, Transaction

    idx = state.book.load_accounts()
    with state.book.exclusive():
        pc = state.book.piecash_book()
        try:
            s = pc.session
            tx = Transaction(currency=pc.default_currency, description=description, post_date=day,
                             splits=[Split(account=s.query(Account).filter_by(guid=idx.find(a).guid).one(),
                                           value=D(v)) for a, v in splits])
            pc.save()
            return tx.guid
        finally:
            pc.close()


def _change(state, tx_guid, value_by_account):
    from piecash import Transaction

    idx = state.book.load_accounts()
    with state.book.exclusive():
        pc = state.book.piecash_book()
        try:
            tx = pc.session.query(Transaction).filter_by(guid=tx_guid).one()
            for sp in tx.splits:
                name = idx.get(sp.account.guid).full_name
                if name in value_by_account:
                    sp.value = sp.quantity = D(value_by_account[name])
            pc.save()
        finally:
            pc.close()


def _delete(state, tx_guid):
    from piecash import Transaction

    with state.book.exclusive():
        pc = state.book.piecash_book()
        try:
            pc.delete(pc.session.query(Transaction).filter_by(guid=tx_guid).one())
            pc.save()
        finally:
            pc.close()


@pytest.fixture
def setup(state):
    """PP file booked into the book with A1 on its own GnuCash account; returns the fake pp-core."""
    _account(state, DEPOT)
    pp = FakePPCore(make_export())
    r = sync(state, _settings(), pp.export("book-1"))
    assert r.ok and r.created == 14, r.summary()
    return pp


def _mirror(state, pp, settings=None):
    return cashflows.plan_mirror(state.book, state.book.load_accounts(), settings or _settings(),
                                 pp.export("book-1", prices="none"))


def _pp_flows(pp):
    return sorted((t["type"], t["amount"], t["date"][:10], t.get("note")) for t in pp.export_data["transactions"]
                  if (t.get("source") or "").startswith(cashflows.SOURCE_PREFIX))


def test_bookings_from_pp_are_no_cash_flows(state, setup):
    m = _mirror(state, setup)
    assert m.flows == [] and not m.pending


def test_deposits_removals_interest_fees_and_taxes_go_to_pp(state, setup):
    _book(state, date(2024, 1, 1), "Einzahlung", (GIRO, "-5000"), (DEPOT, "5000"))
    _book(state, date(2024, 3, 1), "Auszahlung", (DEPOT, "-200"), (GIRO, "200"))
    _book(state, date(2024, 3, 2), "Kontoführung", (DEPOT, "-3"), ("Aufwendungen:Wertpapiergebühren", "3"))
    _book(state, date(2024, 3, 3), "Zinsgutschrift", (DEPOT, "1.20"), ("Erträge:Zinsen", "-1.20"))
    _book(state, date(2024, 3, 4), "Steuer", (DEPOT, "-0.30"), ("Aufwendungen:Kapitalertragsteuer", "0.30"))
    m = _mirror(state, setup)
    assert len(m.create) == 5 and not m.delete
    cashflows.apply(setup, "book-1", m)
    assert _pp_flows(setup) == [
        ("DEPOSIT", "5000", "2024-01-01", "Einzahlung"), ("FEES", "3", "2024-03-02", "Kontoführung"),
        ("INTEREST", "1.2", "2024-03-03", "Zinsgutschrift"), ("REMOVAL", "200", "2024-03-01", "Auszahlung"),
        ("TAXES", "0.3", "2024-03-04", "Steuer")]
    assert all(t["owner"] == A1 for t in setup.export_data["transactions"] if t["uuid"].startswith("cf-"))
    # second run: nothing to do
    m2 = _mirror(state, setup)
    assert m2.unchanged == 5 and not m2.pending
    # and the PP -> GnuCash direction does not book them back
    r = sync(state, _settings(), setup.export("book-1"))
    assert r.ok and r.created == 0 and r.unchanged == 14, r.summary()


def test_change_and_deletion_in_gnucash_are_repeated_in_pp(state, setup):
    t1 = _book(state, date(2024, 1, 1), "Einzahlung", (GIRO, "-5000"), (DEPOT, "5000"))
    t2 = _book(state, date(2024, 1, 2), "Einzahlung 2", (GIRO, "-100"), (DEPOT, "100"))
    cashflows.apply(setup, "book-1", _mirror(state, setup))
    _change(state, t1, {GIRO: "-4500", DEPOT: "4500"})
    _delete(state, t2)
    m = _mirror(state, setup)
    assert len(m.create) == 1 and len(m.delete) == 2 and m.updated == 1
    cashflows.apply(setup, "book-1", m)
    assert _pp_flows(setup) == [("DEPOSIT", "4500", "2024-01-01", "Einzahlung")]


def test_split_transaction_gives_one_flow_per_counter_split(state, setup):
    # 1000 from the current account, of which 2.50 fee
    _book(state, date(2024, 1, 5), "Übertrag", (GIRO, "-1000"), (DEPOT, "997.50"),
          ("Aufwendungen:Wertpapiergebühren", "2.50"))
    m = _mirror(state, setup)
    assert sorted((f.type, f.amount) for f in m.create) == [("DEPOSIT", D("1000")), ("FEES", D("2.5"))]
    assert len({f.key for f in m.create}) == 2


def test_transfer_between_two_mapped_accounts(state, setup):
    from .pp_fixtures import _t

    a3 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    setup.export_data["accounts"].append({"uuid": a3, "name": "Zweites Konto", "currency": "EUR",
                                          "retired": False, "note": None})
    setup.export_data["transactions"].append(_t("x", "account", a3, "INTEREST", "2024-01-01", "0.01"))
    _account(state, "Aktiva:Depotkonto Beispielbank")
    settings = _settings(cash_accounts={A1: DEPOT, a3: "Aktiva:Depotkonto Beispielbank"})
    _book(state, date(2024, 2, 1), "Umbuchung", (DEPOT, "-300"), ("Aktiva:Depotkonto Beispielbank", "300"))
    m = _mirror(state, setup, settings)
    assert sorted((f.account, f.type, f.amount) for f in m.create) == sorted(
        [(A1, "REMOVAL", D("300")), (a3, "DEPOSIT", D("300"))])


def test_clearing_account_future_and_start_date_are_left_out(state, setup):
    _account(state, "Aktiva:Wertpapier-Verrechnung", "ASSET")  # all PP cash goes to DEPOT here
    _book(state, date(2024, 1, 1), "Bankzeile", (DEPOT, "50"), ("Aktiva:Wertpapier-Verrechnung", "-50"))
    _book(state, date.today() + timedelta(days=3), "Dauerauftrag", (GIRO, "-10"), (DEPOT, "10"))
    _book(state, date(2023, 6, 1), "Alt", (GIRO, "-10"), (DEPOT, "10"))
    m = _mirror(state, setup, _settings(cashflows_from="2024-01-01"))
    assert m.create == []
    assert sorted(why for _d, _t, why in m.skipped) == ["Gegenkonto ist das Zwischenkonto",
                                                         "liegt in der Zukunft – wird am Buchungstag übernommen"]


def test_replace_deletes_pp_deposits_that_do_not_come_from_gnucash(state, setup):
    _book(state, date(2024, 1, 2), "Einzahlung", (GIRO, "-10000"), (DEPOT, "10000"))
    plain = _mirror(state, setup)
    assert plain.replaced == []  # without the option PP's own deposit "dep-1" stays
    m = _mirror(state, setup, _settings(cashflows_replace=True))
    assert m.replaced == ["dep-1"] and len(m.create) == 1
    cashflows.apply(setup, "book-1", m)
    deposits = [t for t in setup.export_data["transactions"] if t["type"] == "DEPOSIT"]
    assert len(deposits) == 1 and deposits[0]["source"].startswith("gnubook:")


def test_service_mirrors_before_booking_and_notices_new_gnucash_entries(pp_app, fake):
    reg = pp_app.extensions["gnubook"]
    st = reg.context(reg.system.books()[0]["id"])
    _account(st, DEPOT)
    reg.system.set_pp_settings(st.id, _settings())
    svc = reg.context(st.id).pp
    r = svc.sync(actor="test")
    assert r.ok and r.created == 14, r.summary()
    assert not svc.needs_sync()
    _book(st, date(2024, 1, 1), "Einzahlung", (GIRO, "-5000"), (DEPOT, "5000"))
    assert svc.needs_sync()
    dry = svc.sync(actor="test", dry_run=True)
    assert dry.cash_created == 1 and not any(c[0] == "cashflows" for c in fake.calls)
    r = svc.sync(actor="test")
    assert r.cash_created == 1 and "Ein-/Auszahlungen" in r.summary()
    assert _pp_flows(fake) == [("DEPOSIT", "5000", "2024-01-01", "Einzahlung")]
    assert not svc.needs_sync()


def test_settings_page_saves_and_checks_the_options(pp_app, fake):
    from .conftest import PASSWORD, csrf_from

    reg = pp_app.extensions["gnubook"]
    st = reg.context(reg.system.books()[0]["id"])
    _account(st, DEPOT)
    c = pp_app.test_client()
    c.post("/login", data={"username": "tester", "password": PASSWORD,
                           "csrf_token": csrf_from(c.get("/login").text)})
    page = c.get("/pp/settings").text
    assert 'name="cashflows"' in page
    token = csrf_from(page)
    base = {"csrf_token": token, "enabled": "1", "cashflows": "1", "cashflows_from": "2024-01-01",
            "cashflows_replace": "1"}
    c.post("/pp/settings", data={**base, f"cash_{A1}": DEPOT})
    s = reg.system.pp_settings(st.id)
    assert s.cashflows and s.cashflows_from == "2024-01-01" and s.cashflows_replace
    # without an own account for a PP cash account the option makes no sense
    r = c.post("/pp/settings", data=base, follow_redirects=True)
    assert "mindestens einem PP-Konto" in r.text
    assert reg.system.pp_settings(st.id).cash_accounts == {A1: DEPOT}
