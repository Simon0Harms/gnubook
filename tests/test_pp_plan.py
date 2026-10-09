"""Booking rules for Portfolio Performance transactions (pure computation)."""
from datetime import date
from decimal import Decimal as D

from gnubook.pp.plan import Lots, build_plan, mutual_type

from .pp_fixtures import A1, ETF, P1, P2, SHARE, find, make_export


def _plan(**kw):
    return build_plan(make_export(), "EUR", **kw)


def _legs(b):
    return {(leg.ref[0], leg.ref[1] if leg.ref[0] != "stock" else (leg.ref[1], leg.ref[2]), leg.quantity == 0):
            leg.value for leg in b.legs}


def test_every_booking_balances_and_skips_are_explained():
    plan = _plan()
    for b in plan.bookings:
        if not b.skip:
            assert b.balance == 0, b
            assert b.legs
    skipped = {b.key: b.skip for b in plan.bookings if b.skip}
    assert skipped == {"dep-1": "bankimport", "buy-usd-p": "currency"}


def test_buy_books_gross_value_and_fees():
    b = _plan().by_key()["buy-1-p"]
    assert b.kind == "BUY" and b.day == date(2024, 1, 3)
    assert b.description == "Kauf Musterwelt Aktien ETF"
    assert "Sparplan" in b.notes and "Beleg: Kauf_2024-01-03.pdf" in b.notes
    legs = {leg.ref[0] + ":" + leg.ref[-1]: (leg.value, leg.quantity) for leg in b.legs}
    assert legs[f"stock:{ETF}"] == (D("1000.00"), D("10"))
    assert legs["role:clearing"] == (D("-1001.50"), None)
    assert legs["role:fees"] == (D("1.50"), None)


def test_sale_realises_fifo_gain_on_values_before_fees():
    b = _plan().by_key()["sell-1-p"]
    # 10 shares at 100 + 2 shares at 120 = 1240 cost; gross proceeds 1500 + 2 fees + 48 tax = 1550
    assert b.gain == D("310.00")
    stock = [leg for leg in b.legs if leg.ref[0] == "stock"]
    assert sorted((leg.value, leg.quantity) for leg in stock) == [(D("-1550"), D("-12")), (D("310.00"), D("0"))]
    roles = {leg.ref[1]: leg.value for leg in b.legs if leg.ref[0] == "role"}
    assert roles == {"clearing": D("1500"), "fees": D("2"), "taxes": D("48"), "gains": D("-310.00")}


def test_without_realized_gains_no_gain_splits():
    b = _plan(realized_gains=False).by_key()["sell-1-p"]
    assert b.gain == D("310.00")
    assert not any(leg.ref == ("role", "gains") for leg in b.legs)
    assert b.balance == 0


def test_capitalized_fees_go_into_the_cost_basis():
    plan = _plan(capitalize_fees=True)
    buy = plan.by_key()["buy-1-p"]
    assert [(leg.ref[0], leg.value) for leg in buy.legs] == [("stock", D("1001.50")), ("role", D("-1001.50"))]
    sell = plan.by_key()["sell-1-p"]
    # cost 1001.50 + 2/5 * 600 = 1241.50; proceeds after fees 1548
    assert sell.gain == D("306.50")
    assert sell.balance == 0


def test_dividend_with_tax_is_linked_to_the_holding():
    b = _plan().by_key()["div-1"]
    roles = {leg.ref[1]: leg.value for leg in b.legs if leg.ref[0] == "role"}
    assert roles == {"clearing": D("20"), "dividends": D("-25"), "taxes": D("5")}
    link = [leg for leg in b.legs if leg.ref[0] == "stock"]
    assert link and link[0].ref == ("stock", P1, ETF) and link[0].value == 0 and link[0].quantity == 0


def test_cash_account_transactions():
    by = _plan().by_key()
    assert {leg.ref[1]: leg.value for leg in by["int-1"].legs} == {"clearing": D("3"), "interest": D("-3")}
    assert {leg.ref[1]: leg.value for leg in by["fee-1"].legs} == {"clearing": D("-10"), "fees": D("10")}
    assert by["fee-1"].description == "Gebühren Musterwelt Aktien ETF"
    assert {leg.ref[1]: leg.value for leg in by["tax-1"].legs} == {"clearing": D("-4"), "taxes": D("4")}
    assert {leg.ref[1]: leg.value for leg in by["taxr-1"].legs} == {"clearing": D("2"), "taxes": D("-2")}
    assert {leg.ref[1]: leg.value for leg in by["feer-1"].legs} == {"clearing": D("1"), "fees": D("-1")}
    assert {leg.ref[1]: leg.value for leg in by["intc-1"].legs} == {"clearing": D("-0.50"),
                                                                     "interest_charge": D("0.50")}


def test_transfer_moves_fifo_cost_between_securities_accounts():
    b = _plan().by_key()["tr-out"]
    assert b.kind == "TRANSFER"
    assert sorted((leg.ref, leg.value, leg.quantity) for leg in b.legs) == sorted([
        (("stock", P1, ETF), D("-360.00"), D("-3")), (("stock", P2, ETF), D("360.00"), D("3"))])
    assert "tr-in" not in _plan().by_key()


def test_deliveries_use_the_delivery_account():
    by = _plan().by_key()
    inbound = {leg.ref[0]: (leg.value, leg.quantity) for leg in by["del-in"].legs}
    assert inbound == {"stock": (D("500"), D("50")), "role": (D("-500"), None)}
    outbound = {leg.ref[0]: (leg.value, leg.quantity) for leg in by["del-out"].legs}
    assert outbound == {"stock": (D("-100.00"), D("-10")), "role": (D("100.00"), None)}


def test_holdings_and_share_precision():
    plan = _plan()
    assert plan.holdings[(P1, ETF)] == 0
    assert plan.holdings[(P2, ETF)] == 3
    assert plan.holdings[(P2, SHARE)] == D("42.5")
    assert plan.securities[SHARE].decimals == 1
    assert plan.stock_keys() == {(P1, ETF), (P2, ETF), (P2, SHARE)}


def test_start_date_books_opening_positions_at_fifo_cost():
    plan = _plan(sync_from=date(2024, 1, 21))
    by = plan.by_key()
    assert by["buy-1-p"].skip == "before_start" and by["sell-1-p"].skip == "before_start"
    opening = by[f"opening:{P1}:{ETF}"]
    assert opening.day == date(2024, 1, 20)
    assert {leg.ref[0]: (leg.value, leg.quantity) for leg in opening.legs} == {
        "stock": (D("360.00"), D("3")), "role": (D("-360.00"), None)}
    assert not by["div-1"].skip  # after the start date


def test_cash_side_can_be_a_real_account():
    plan = build_plan(make_export(), "EUR", cash_accounts={A1: "Aktiva:Depotkonto"})
    b = plan.by_key()["buy-1-p"]
    assert ("cash", A1) in [leg.ref for leg in b.legs]


def test_fingerprint_changes_with_the_content_only():
    e1, e2 = make_export(), make_export()
    assert build_plan(e1, "EUR").by_key()["buy-1-p"].fingerprint == build_plan(e2, "EUR").by_key()["buy-1-p"].fingerprint
    find(e2, "buy-1-p")["note"] = "anders"
    assert build_plan(e1, "EUR").by_key()["buy-1-p"].fingerprint != build_plan(e2, "EUR").by_key()["buy-1-p"].fingerprint
    find(e2, "buy-1-p")["updatedAt"] = "2026-01-01T00:00:00Z"  # PP's change stamp alone is no change
    assert build_plan(e2, "EUR").by_key()["buy-1-p"].fingerprint == build_plan(e2, "EUR").by_key()["buy-1-p"].fingerprint


def test_selling_more_than_held_warns():
    e = make_export()
    find(e, "sell-1-p")["shares"] = "20"
    b = build_plan(e, "EUR").by_key()["sell-1-p"]
    assert b.warnings and "mehr verkauft" in b.warnings[0]
    assert b.balance == 0


def test_lots_keep_purchase_order_across_transfers():
    lots = Lots()
    lots.add("a", D(10), D(100), date(2024, 1, 1))
    lots.add("b", D(5), D(100), date(2024, 1, 5))
    lots.move("a", "b", D(4))
    cost, missing = lots.take("b", D(4))
    assert cost == D(40) and missing == 0  # the older lot (from a) goes first
    assert lots.shares("b") == D(5) and lots.cost("a") == D(60)


def test_mutual_fund_detection():
    assert mutual_type("iShares Core MSCI World UCITS ETF") == "MUTUAL"
    assert mutual_type("Siemens AG") == "STOCK"
