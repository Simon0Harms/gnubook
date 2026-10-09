"""Reports: income and expenses with Sankey flow, category breakdown, monthly trend and budget tracking."""
from __future__ import annotations

from ..i18n import gettext as _

from decimal import Decimal

from flask import Blueprint, render_template, request, url_for

from .. import reports as rp
from ..book import latest_prices
from ..money import ZERO
from . import state
from .auth import login_required
from .views import _parse_date, index

bp = Blueprint("reports", __name__, url_prefix="/reports")

PERIOD_LABELS = {"month": "Dieser Monat", "last_month": "Letzter Monat", "ytd": "Dieses Jahr",
                 "12m": "Letzte 12 Monate", "last_year": "Letztes Jahr", "custom": "Zeitraum"}
KIND_LABELS = {"EXPENSE": "Ausgaben", "INCOME": "Einnahmen"}
MONTHS = ("Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez")


def _int(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


@bp.route("/income-expenses")
@login_required
def income_expenses():
    st = state()
    idx = index()
    today = st.book.today()
    period = request.args.get("period", "12m")
    if period not in rp.PERIODS:
        period = "12m"
    start, end = rp.period_range(period, today, _parse_date(request.args.get("from")),
                                 _parse_date(request.args.get("to")))
    depth = _int("depth", 1, 1, 3)
    kind = request.args.get("kind", "EXPENSE")
    if kind not in rp.KINDS:
        kind = "EXPENSE"
    include_closing = request.args.get("closing") == "1"
    focus = idx.get(request.args.get("cat", "")) if request.args.get("cat") else None
    if focus is not None and focus.type != kind:
        focus = None

    with st.book.connect() as conn:
        prices = latest_prices(conn)
        fl = rp.flows(conn, st.book, idx, start, end, exclude_closing=not include_closing, prices=prices)
        closing_exists = rp.has_closing(conn)
        budgets = rp.load_budgets(conn)
        budget = next((b for b in budgets if b.guid == request.args.get("budget")), budgets[0] if budgets else None)
        bsel = request.args.get("bp", "ytd")
        b_periods, b_start, b_end, b_explicit = [], None, None, {}
        if budget is not None and budget.num_periods > 0:
            all_periods = budget.periods()
            if bsel == "all":
                chosen = all_periods
            elif bsel.isdigit() and int(bsel) < budget.num_periods:
                chosen = [all_periods[int(bsel)]]
            else:
                bsel = "ytd"
                # completed periods only, so a running month does not show as missed income
                chosen = ([p for p in all_periods if p[2] < today]
                          or [p for p in all_periods if p[1] <= today][:1] or all_periods[:1])
            b_periods = [p[0] for p in chosen]
            b_start, b_end = chosen[0][1], chosen[-1][2]
            b_explicit = rp.budget_amounts(conn, idx, budget, b_periods, prices)
            b_flows = rp.flows(conn, st.book, idx, b_start, b_end, exclude_closing=not include_closing,
                               prices=prices)
    totals = rp.rollup(idx, fl.by_account)
    income_total = sum((totals.get(a.guid, ZERO) for a in rp.category_roots(idx, "INCOME")), ZERO)
    expense_total = sum((totals.get(a.guid, ZERO) for a in rp.category_roots(idx, "EXPENSE")), ZERO)
    n_months = len(rp.month_keys(start, end))

    def drill(cat):
        if cat is None or cat.value <= 0:
            return None
        if cat.has_children:
            return url_for("reports.income_expenses", **dict(request.args.to_dict(), kind=cat.account.type,
                                                             cat=cat.account.guid))
        return url_for("views.register", guid=cat.account.guid)

    flow_income = rp.category_list(rp.categories_at(idx, "INCOME", depth), totals, "INCOME")
    flow_expense = rp.category_list(rp.categories_at(idx, "EXPENSE", depth), totals, "EXPENSE")
    sankey = rp.sankey(flow_income, flow_expense, href=drill,
                       labels={"other": _("Sonstige"), "saving": _("Ersparnis"), "deficit": _("Fehlbetrag"),
                               "total": _("Einnahmen")})

    # breakdown of one kind (optionally inside a category)
    if focus is not None:
        level = [c for c in focus.children if c.type == kind]
        breakdown = rp.category_list(level, totals, kind)
        if fl.by_account.get(focus.guid):  # bookings on the parent account itself
            own = rp.Category(focus, fl.by_account[focus.guid], path=_("direkt auf {a0}", a0=focus.name))
            breakdown.append(own)
            breakdown.sort(key=lambda r: r.value, reverse=True)
    else:
        breakdown = rp.category_list(rp.category_roots(idx, kind), totals, kind)
    positive = sum((c.value for c in breakdown if c.value > 0), ZERO)
    for i, c in enumerate(breakdown):
        c.color = rp.PALETTE[i % len(rp.PALETTE)]
        c.share = float(c.value / positive) if positive > 0 and c.value > 0 else 0.0
    focus_total = totals.get(focus.guid, ZERO) if focus is not None else (
        expense_total if kind == "EXPENSE" else income_total)
    segments = rp.donut(breakdown)
    crumbs = rp.breadcrumb(idx, focus, kind) if focus is not None else []

    # monthly trend (income vs. expense, and the selected category)
    focus_guids = None
    if focus is not None:
        focus_guids = {focus.guid} | {a.guid for a in idx.descendants(focus)}
    months = []
    for key in rp.month_keys(start, end):
        vals = fl.by_month.get(key, {})
        inc = sum((v for g_, v in vals.items() if idx.get(g_).type == "INCOME"), ZERO)
        exp = sum((v for g_, v in vals.items() if idx.get(g_).type == "EXPENSE"), ZERO)
        sel = sum((v for g_, v in vals.items() if g_ in focus_guids), ZERO) if focus_guids else None
        label = _(MONTHS[int(key[5:]) - 1]) + " " + key[2:4]
        months.append({"key": key, "label": label, "income": inc, "expense": exp, "net": inc - exp, "sel": sel})
    peak = max([max(m["income"], m["expense"]) for m in months] + [Decimal(1)])
    sel_peak = max([m["sel"] for m in months if m["sel"] is not None] + [Decimal(1)])

    # budget vs. actual
    budget_view = None
    if budget is not None:
        b_totals = rp.rollup(idx, b_flows.by_account) if b_periods else {}
        rolled = rp.budget_rollup(idx, b_explicit)
        sections = []
        for k in ("EXPENSE", "INCOME"):
            rows = rp.budget_rows(idx, k, depth, rolled, b_totals)
            planned = sum((r.budget for r in rows if r.budget is not None), ZERO)
            actual_b = sum((r.actual for r in rows if r.budget is not None), ZERO)
            unbudgeted = sum((r.actual for r in rows if r.budget is None), ZERO)
            sections.append({"kind": k, "rows": rows, "planned": planned, "actual": actual_b,
                             "unbudgeted": unbudgeted})
        budget_view = {"budget": budget, "budgets": budgets, "sel": bsel, "start": b_start, "end": b_end,
                       "sections": sections, "periods": budget.periods(), "empty": not b_explicit}

    return render_template(
        "reports/income_expenses.html", period=period, start=start, end=end, depth=depth, kind=kind,
        include_closing=include_closing, closing_exists=closing_exists, focus=focus, crumbs=crumbs,
        income_total=income_total, expense_total=expense_total, n_months=n_months, sankey=sankey,
        breakdown=breakdown, focus_total=focus_total, segments=segments, months=months, peak=peak,
        sel_peak=sel_peak, budget_view=budget_view, unconverted=fl.unconverted, drill=drill,
        base=idx.root.commodity, PERIOD_LABELS=PERIOD_LABELS, KIND_LABELS=KIND_LABELS, MONTHS=MONTHS)


NW_PERIOD_LABELS = {"12m": "Letzte 12 Monate", "ytd": "Dieses Jahr", "last_year": "Letztes Jahr",
                    "3y": "Letzte 3 Jahre", "5y": "Letzte 5 Jahre", "all": "Gesamter Zeitraum", "custom": "Zeitraum"}


@bp.route("/net-worth")
@login_required
def net_worth():
    st = state()
    idx = index()
    today = st.book.today()
    period = request.args.get("period", "12m")
    if period not in rp.NW_PERIODS:
        period = "12m"
    parts = request.args.get("parts", "1") == "1"
    with st.book.connect() as conn:
        first = rp.first_booking(conn, st.book)
        start, end = rp.nw_period_range(period, today, first, _parse_date(request.args.get("from")),
                                        _parse_date(request.args.get("to")))
        nw = rp.net_worth(conn, st.book, idx, start, end, opening=period != "all")
    points = nw.points
    for p in points:
        p.label = _(MONTHS[int(p.key[5:]) - 1]) + " " + p.key[2:4]
    chart = rp.line_chart(points, ("net", "assets", "liabilities") if parts else ("net",))
    rows = []
    prev = None
    for p in points:
        rows.append({"p": p, "change": None if prev is None else p.net - prev.net})
        prev = p
    first_p, last_p = (points[0], points[-1]) if points else (None, None)
    change = last_p.net - first_p.net if points else ZERO
    pct = float(change / abs(first_p.net) * 100) if points and first_p.net else None
    groups = []
    for g in nw.groups:
        a = first_p.groups.get(g.guid, ZERO) if first_p else ZERO
        b = last_p.groups.get(g.guid, ZERO) if last_p else ZERO
        if a or b:
            groups.append({"account": g, "start": a, "end": b, "change": b - a,
                           "liability": g.type in rp.LIABILITY_TYPES})
    return render_template(
        "reports/net_worth.html", period=period, start=start, end=end, parts=parts, points=points, rows=rows,
        chart=chart, first=first_p, last=last_p, change=change, pct=pct, groups=groups,
        unconverted=nw.unconverted, base=idx.root.commodity, PERIOD_LABELS=NW_PERIOD_LABELS)


PF_PERIOD_LABELS = NW_PERIOD_LABELS


@bp.route("/portfolio")
@login_required
def portfolio():
    st = state()
    idx = index()
    today = st.book.today()
    period = request.args.get("period", "all")
    if period not in rp.PF_PERIODS:
        period = "all"
    grouped = request.args.get("grouped", "1") == "1"
    closed = request.args.get("closed") == "1"
    security = request.args.get("sec") or None
    with st.book.connect() as conn:
        first = rp.first_security_booking(conn, st.book, idx)
        start, end = rp.nw_period_range(period, today, first, _parse_date(request.args.get("from")),
                                        _parse_date(request.args.get("to")))
        known = {a.commodity_guid for a in rp.security_accounts(idx)}
        if security not in known:
            security = None
        pf = rp.portfolio(conn, st.book, idx, start, end, today, grouped=grouped, security=security,
                          opening=period != "all")
        prices, n_prices = rp.price_list(conn, st.book, idx, {security} if security else known)
    for p in pf.points:
        p.label = _(MONTHS[int(p.key[5:]) - 1]) + " " + p.key[2:4]
    points = pf.points
    has_data = any(p.value or p.cost for p in points)
    chart = rp.line_chart(points, ("value", "cost")) if has_data else None
    price_points = [p for p in points if p.price is not None]
    price_chart = rp.line_chart(price_points, ("price",), zero=False) if security and len(price_points) > 1 else None
    holdings = [h for h in pf.holdings if h.active or closed]
    active = [h for h in pf.holdings if h.active]
    first_p, last_p = (points[0], points[-1]) if points else (None, None)
    change = last_p.value - first_p.value if points else ZERO
    flows = (last_p.cost - first_p.cost) if points else ZERO  # net purchases in the period (cost basis change)
    sec_cdty = idx.commodities.get(security) if security else None
    return render_template(
        "reports/portfolio.html", period=period, start=start, end=end, grouped=grouped, closed=closed,
        security=security, sec_cdty=sec_cdty, pf=pf, holdings=holdings, active=active,
        segments=rp.allocation(pf.holdings), points=points, chart=chart, price_chart=price_chart,
        first=first_p, last=last_p, change=change, flows=flows, prices=prices, n_prices=n_prices,
        unconverted=pf.unconverted, base=idx.root.commodity, today=today, PERIOD_LABELS=PF_PERIOD_LABELS)
