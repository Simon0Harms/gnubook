"""Income/expense report, Sankey, budgets."""
from datetime import date
from decimal import Decimal as D

from sqlalchemy import text

from gnubook import reports as rp
from gnubook.book import latest_prices


def _sum_month(state, start, end, closing=False):
    idx = state.book.load_accounts()
    with state.book.connect() as conn:
        fl = rp.flows(conn, state.book, idx, start, end, exclude_closing=not closing)
    return idx, fl, rp.rollup(idx, fl.by_account)


def test_period_ranges():
    t = date(2026, 3, 15)
    assert rp.period_range("month", t) == (date(2026, 3, 1), t)
    assert rp.period_range("last_month", t) == (date(2026, 2, 1), date(2026, 2, 28))
    assert rp.period_range("ytd", t) == (date(2026, 1, 1), t)
    assert rp.period_range("12m", t) == (date(2025, 4, 1), t)
    assert rp.period_range("last_year", t) == (date(2025, 1, 1), date(2025, 12, 31))
    assert rp.period_range("custom", t, date(2026, 2, 1), date(2026, 1, 1)) == (date(2026, 1, 1), date(2026, 2, 1))
    assert rp.month_keys(date(2025, 11, 5), date(2026, 2, 1)) == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_flows_signs_and_rollup(state):
    idx, fl, totals = _sum_month(state, date(2025, 9, 1), date(2025, 9, 30))
    assert totals[idx.find("Aufwendungen:Wohnen:Miete").guid] == D("950.00")
    assert totals[idx.find("Erträge:Gehalt").guid] == D("3150.00")
    wohnen = idx.find("Aufwendungen:Wohnen")
    assert totals[wohnen.guid] == D("950.00") + D("62.00") + D("39.99")
    roots = rp.category_roots(idx, "EXPENSE")
    assert "Wohnen" in [a.name for a in roots] and "Aufwendungen" not in [a.name for a in roots]
    level2 = [a.name for a in rp.categories_at(idx, "EXPENSE", 2)]
    assert "Miete" in level2 and "Lebensmittel" in level2 and "Wohnen" not in level2


def test_closing_transactions_are_excluded(state):
    idx = state.book.load_accounts()
    miete = idx.find("Aufwendungen:Wohnen:Miete")
    with state.book.engine.begin() as conn:
        tx = conn.execute(text("SELECT s.tx_guid FROM splits s JOIN transactions t ON t.guid = s.tx_guid "
                               "WHERE s.account_guid = :a ORDER BY t.post_date LIMIT 1"), {"a": miete.guid}).scalar()
        conn.execute(text("INSERT INTO slots (obj_guid, name, slot_type, int64_val) VALUES (:o, 'book_closing', 1, 1)"),
                     {"o": tx})
    _, _, with_closing = _sum_month(state, date(2025, 8, 1), date(2025, 8, 31), closing=True)
    _, _, without = _sum_month(state, date(2025, 8, 1), date(2025, 8, 31))
    assert with_closing[miete.guid] - without[miete.guid] == D("950.00")


def test_sankey_balances():
    class A:
        def __init__(self, n):
            self.guid, self.name, self.type, self.children = n, n, "X", []
    inc = [rp.Category(A("Gehalt"), D(3000))]
    exp = [rp.Category(A("Miete"), D(1000)), rp.Category(A("Essen"), D(500))]
    s = rp.sankey(inc, exp)
    labels = [n.label for n in s["nodes"]]
    assert "saving" in labels and len(s["links"]) == 4
    saving = next(n for n in s["nodes"] if n.label == "saving")
    assert saving.value == D(1500)
    s = rp.sankey([rp.Category(A("Gehalt"), D(100))], exp)
    assert any(n.label == "deficit" and n.value == D(1400) for n in s["nodes"])
    assert rp.sankey([], []) is None


def test_budget_periods_and_amounts(state):
    idx = state.book.load_accounts()
    with state.book.connect() as conn:
        budgets = rp.load_budgets(conn)
        assert len(budgets) == 1
        b = budgets[0]
        assert b.start == date(2026, 1, 1) and b.period_type == "month" and b.num_periods == 12
        assert b.period_range(1) == (date(2026, 2, 1), date(2026, 2, 28))
        explicit = rp.budget_amounts(conn, idx, b, [0, 1, 2], latest_prices(conn))
    rolled = rp.budget_rollup(idx, explicit)
    assert explicit[idx.find("Aufwendungen:Wohnen:Miete").guid] == D("2850")
    assert rolled[idx.find("Aufwendungen:Wohnen").guid] == D("3150")
    assert rolled[idx.find("Erträge").guid] == D("9459")
    assert rolled[idx.find("Aufwendungen:Bankgebühren").guid] is None
    y = rp.Budget("x", "y", "", 4, "year", 1, date(2024, 2, 29))
    assert y.period_start(1) == date(2025, 2, 28)


def test_report_page(client, state):
    idx = state.book.load_accounts()
    r = client.get("/reports/income-expenses?period=custom&from=2026-01-01&to=2026-03-31")
    assert r.status_code == 200
    assert "<svg class=\"sankey\"" in r.text and "Haushaltsplan 2026" in r.text and "Ersparnis" in r.text
    assert "Bankgebühren" in r.text and "ohne Budget" in r.text
    wohnen = idx.find("Aufwendungen:Wohnen")
    r = client.get(f"/reports/income-expenses?period=ytd&cat={wohnen.guid}&depth=2&bp=0")
    assert r.status_code == 200 and "Miete" in r.text and "Strom" in r.text
    for q in ["", "?period=month", "?period=last_year", "?period=bogus&depth=9", "?kind=INCOME", "?bp=all",
              "?period=custom&from=xx&to=", "?kind=INCOME&cat=" + wohnen.guid]:
        assert client.get("/reports/income-expenses" + q).status_code == 200, q


def test_report_without_budget_and_data(client, state):
    with state.book.engine.begin() as conn:
        conn.execute(text("DELETE FROM budget_amounts"))
        conn.execute(text("DELETE FROM budgets"))
    r = client.get("/reports/income-expenses?period=custom&from=2010-01-01&to=2010-02-01")
    assert r.status_code == 200 and "kein Budget" in r.text and "Keine Einnahmen oder Ausgaben" in r.text
