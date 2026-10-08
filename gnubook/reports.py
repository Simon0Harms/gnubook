"""Reports (read-only): income/expenses with category totals, monthly trend, Sankey flow, budget vs. actual;
net worth over time.

All amounts are converted into the book currency (root account commodity) with the latest prices. Income is
reported positive (GnuCash stores it as credit, i.e. negative), expenses positive. Book-closing transactions
(slot ``book_closing``) can be left out.
"""
from __future__ import annotations

import bisect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import text

from .book import (ASSET_TYPES, BALANCE_SHEET_TYPES, LIABILITY_TYPES, Account, AccountIndex, Book, convert,
                   latest_prices)
from .money import ZERO, fmt, gnc_decimal

KINDS = ("INCOME", "EXPENSE")
CLOSING_SLOTS = ("book_closing", "book-closing")
PERIODS = ("month", "last_month", "ytd", "12m", "last_year", "custom")
# colours for categories (good contrast in light and dark mode)
PALETTE = ("#2f6fb0", "#e07b39", "#3d9a5b", "#c0504d", "#8064a2", "#4bacc6", "#b58b00", "#d0679d",
           "#5c7c8a", "#7f9c2b", "#a0522d", "#6a5acd")
OTHER_COLOR = "#9aa0a6"


# ------------------------------------------------------------------------------------------ periods

def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def period_range(period: str, today: date, start: date | None = None, end: date | None = None):
    """(first day, last day) of a named reporting period."""
    if period == "last_month":
        first = add_months(month_start(today), -1)
        return first, month_start(today) - timedelta(days=1)
    if period == "ytd":
        return date(today.year, 1, 1), today
    if period == "12m":
        return add_months(month_start(today), -11), today
    if period == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if period == "custom" and start and end:
        return (start, end) if start <= end else (end, start)
    return month_start(today), today  # "month"


def month_keys(start: date, end: date) -> list[str]:
    keys, d = [], month_start(start)
    while d <= end:
        keys.append(f"{d.year:04d}-{d.month:02d}")
        d = add_months(d, 1)
    return keys


# ------------------------------------------------------------------------------------------ flows

def has_closing(conn) -> bool:
    return conn.execute(text("SELECT COUNT(*) FROM slots WHERE name IN ('book_closing', 'book-closing')")).scalar() > 0


@dataclass
class Flows:
    """Signed values (income and expense positive) per account and month, in book currency."""
    by_account: dict = field(default_factory=lambda: defaultdict(lambda: ZERO))
    by_month: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(lambda: ZERO)))  # key -> guid -> v
    unconverted: int = 0


def flows(conn, book: Book, index: AccountIndex, start: date, end: date, exclude_closing: bool = True,
          prices: dict | None = None) -> Flows:
    """Income and expense values per account between `start` and `end` (both inclusive)."""
    out = Flows()
    kinds = {g: a for g, a in index.by_guid.items() if a.type in KINDS}
    if not kinds:
        return out
    if prices is None:
        prices = latest_prices(conn)
    base = index.root.commodity
    sql = ("SELECT s.account_guid, s.value_num, s.value_denom, t.currency_guid, t.post_date FROM splits s "
           "JOIN transactions t ON t.guid = s.tx_guid WHERE t.post_date >= :start AND t.post_date < :end")
    if exclude_closing:
        sql += (" AND t.guid NOT IN (SELECT obj_guid FROM slots WHERE name IN ('book_closing', 'book-closing'))")
    params = {"start": book.day_start_utc(start), "end": book.day_end_utc(end)}
    for ag, vn, vd, cur, pd in conn.execute(text(sql), params):
        acc = kinds.get(ag)
        if acc is None:
            continue
        v = gnc_decimal(int(vn), int(vd))
        v = convert(v, index.commodities.get(cur), base, prices)
        if v is None:
            out.unconverted += 1
            continue
        if acc.type == "INCOME":
            v = -v
        d = book.day_of(pd)
        out.by_account[ag] += v
        out.by_month[f"{d.year:04d}-{d.month:02d}"][ag] += v
    return out


def rollup(index: AccountIndex, values: dict) -> dict[str, Decimal]:
    """Value of every account including all sub-accounts."""
    total: dict[str, Decimal] = {}

    def visit(acc: Account) -> Decimal:
        v = values.get(acc.guid, ZERO) + sum((visit(c) for c in acc.children), ZERO)
        total[acc.guid] = v
        return v

    for top in index.top_level():
        visit(top)
    return total


# ------------------------------------------------------------------------------------------ categories

def category_roots(index: AccountIndex, kind: str) -> list[Account]:
    """Level-1 categories of a kind. A single top-level account ("Aufwendungen") is skipped over."""
    tops = [a for a in index.top_level() if a.type == kind]
    while len(tops) == 1 and tops[0].children:
        tops = [c for c in tops[0].children if c.type == kind]
    return tops


def categories_at(index: AccountIndex, kind: str, depth: int) -> list[Account]:
    """Categories down to `depth` levels; accounts without children stay on their level."""
    level = category_roots(index, kind)
    for _ in range(depth - 1):
        nxt = []
        for a in level:
            kids = [c for c in a.children if c.type == kind]
            nxt.extend(kids if kids else [a])
        level = nxt
    return level


@dataclass
class Category:
    account: Account
    value: Decimal
    share: float = 0.0
    color: str = OTHER_COLOR
    has_children: bool = False
    path: str = ""


def category_list(accounts: list[Account], totals: dict, kind: str) -> list[Category]:
    rows = [Category(a, totals.get(a.guid, ZERO), has_children=any(c.type == kind for c in a.children))
            for a in accounts]
    rows = [r for r in rows if r.value != 0]
    rows.sort(key=lambda r: r.value, reverse=True)
    positive = sum((r.value for r in rows if r.value > 0), ZERO)
    for i, r in enumerate(rows):
        r.color = PALETTE[i % len(PALETTE)]
        r.share = float(r.value / positive) if positive > 0 and r.value > 0 else 0.0
    return rows


def breadcrumb(index: AccountIndex, acc: Account | None, kind: str) -> list[Account]:
    """Path from the level-1 category down to `acc`."""
    roots = {a.guid for a in category_roots(index, kind)}
    path = []
    while acc is not None:
        path.append(acc)
        if acc.guid in roots:
            break
        acc = index.get(acc.parent_guid)
    else:
        return []
    return list(reversed(path))


# ------------------------------------------------------------------------------------------ charts (SVG data)

def donut(categories: list[Category], limit: int = 8, radius: float = 70.0):
    """Segments for an SVG donut (circle stroke-dasharray). Small categories are merged into "other"."""
    pos = [c for c in categories if c.value > 0]
    total = sum((c.value for c in pos), ZERO)
    if total <= 0:
        return []
    shown = pos[:limit]
    rest = sum((c.value for c in pos[limit:]), ZERO)
    circ = 2 * math.pi * radius
    segs, offset = [], 0.0
    items = [(c.account.name, c.value, c.color, c) for c in shown]
    if rest > 0:
        items.append((None, rest, OTHER_COLOR, None))
    for name, value, color, cat in items:
        frac = float(value / total)
        length = frac * circ
        segs.append({"name": name, "value": value, "color": color, "cat": cat, "share": frac,
                     "dash": f"{length:.2f} {circ - length:.2f}", "offset": f"{-offset:.2f}"})
        offset += length
    return segs


@dataclass
class SankeyNode:
    key: str
    label: str
    value: Decimal
    color: str
    x: float = 0
    y: float = 0
    h: float = 0
    ly: float = 0  # label position (spread so labels never overlap)
    href: str | None = None
    side: str = "left"


@dataclass
class SankeyLink:
    path: str
    color: str
    title: str
    value: Decimal


def sankey(income: list[Category], expense: list[Category], width: float = 900, min_height: float = 320,
           limit: int = 10, href=None, labels=None):
    """Three-column Sankey: income categories → total → expense categories (+ savings or deficit)."""
    labels = labels or {}
    href = href or (lambda cat: None)

    def side_nodes(cats, prefix):
        pos = [c for c in cats if c.value > 0]
        nodes = [SankeyNode(f"{prefix}{c.account.guid}", c.account.name, c.value, c.color, href=href(c))
                 for c in pos[:limit]]
        rest = sum((c.value for c in pos[limit:]), ZERO)
        if rest > 0:
            nodes.append(SankeyNode(f"{prefix}other", labels.get("other", "other"), rest, OTHER_COLOR))
        return nodes

    left = side_nodes(income, "i:")
    right = side_nodes(expense, "e:")
    tin = sum((n.value for n in left), ZERO)
    tout = sum((n.value for n in right), ZERO)
    if tin <= 0 and tout <= 0:
        return None
    if tin > tout:
        right.append(SankeyNode("e:saving", labels.get("saving", "saving"), tin - tout, "#2e7d32"))
    elif tout > tin:
        left.append(SankeyNode("i:deficit", labels.get("deficit", "deficit"), tout - tin, "#c62828"))
    total = max(tin, tout)
    gap = 10.0
    n = max(len(left), len(right))
    height = max(min_height, n * 40.0)
    usable = height - gap * (n - 1) if n > 1 else height
    scale = usable / float(total)
    node_w = 14.0
    x_left, x_mid, x_right = 0.0, (width - node_w) / 2, width - node_w

    def stack(nodes, x, side):
        sizes = [max(float(nd.value) * scale, 2.0) for nd in nodes]
        span = sum(sizes) + gap * (len(nodes) - 1)
        y = (height - span) / 2
        for nd, h in zip(nodes, sizes):
            nd.x, nd.y, nd.h, nd.side = x, y, h, side
            y += h + gap
        last = -1e9
        for nd in nodes:
            nd.ly = max(nd.y + nd.h / 2, last + 36)
            last = nd.ly

    stack(left, x_left, "left")
    stack(right, x_right, "right")
    mid = SankeyNode("total", labels.get("total", "total"), total, "#6c757d", x_mid, (height - float(total) * scale) / 2,
                     float(total) * scale, side="mid")
    links = []
    cursor = mid.y
    for nd in left:
        links.append(_ribbon(x_left + node_w, nd.y, x_mid, cursor, nd.h, nd.color, nd.label, nd.value))
        cursor += nd.h
    cursor = mid.y
    for nd in right:
        links.append(_ribbon(x_mid + node_w, cursor, x_right, nd.y, nd.h, nd.color, nd.label, nd.value))
        cursor += nd.h
    return {"width": width, "height": height, "nodes": left + [mid] + right, "links": links, "node_w": node_w}


def _ribbon(x0, y0, x1, y1, h, color, label, value) -> SankeyLink:
    cx = (x0 + x1) / 2
    d = (f"M{x0:.1f},{y0:.1f} C{cx:.1f},{y0:.1f} {cx:.1f},{y1:.1f} {x1:.1f},{y1:.1f} "
         f"L{x1:.1f},{y1 + h:.1f} C{cx:.1f},{y1 + h:.1f} {cx:.1f},{y0 + h:.1f} {x0:.1f},{y0 + h:.1f} Z")
    return SankeyLink(d, color, label, value)


# ------------------------------------------------------------------------------------------ budgets

@dataclass
class Budget:
    guid: str
    name: str
    description: str
    num_periods: int
    period_type: str = "month"
    mult: int = 1
    start: date | None = None

    def period_start(self, n: int) -> date:
        start = self.start or date.today().replace(month=1, day=1)
        step = self.mult * n
        if self.period_type in ("year",):
            return _add_months_keep_day(start, 12 * step)
        if self.period_type in ("week",):
            return start + timedelta(weeks=step)
        if self.period_type in ("day",):
            return start + timedelta(days=step)
        return _add_months_keep_day(start, step)  # month, end of month, nth weekday …

    def period_range(self, n: int) -> tuple[date, date]:
        return self.period_start(n), self.period_start(n + 1) - timedelta(days=1)

    def periods(self):
        return [(n, *self.period_range(n)) for n in range(self.num_periods)]


def _add_months_keep_day(d: date, n: int) -> date:
    first = add_months(d.replace(day=1), n)
    nxt = add_months(first, 1)
    return first.replace(day=min(d.day, (nxt - timedelta(days=1)).day))


def _parse_start(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip().replace("-", "")[:8]
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (ValueError, IndexError):
        return None


def load_budgets(conn) -> list[Budget]:
    out = []
    recs = {}
    for og, mult, ptype, pstart in conn.execute(text(
            "SELECT obj_guid, recurrence_mult, recurrence_period_type, recurrence_period_start FROM recurrences")):
        recs[og] = (int(mult or 1), (ptype or "month").lower(), _parse_start(pstart))
    for guid, name, desc, num in conn.execute(text("SELECT guid, name, description, num_periods FROM budgets")):
        b = Budget(guid, name or "", desc or "", int(num or 0))
        if guid in recs:
            b.mult, b.period_type, b.start = recs[guid]
        out.append(b)
    out.sort(key=lambda b: b.name.casefold())
    return out


def budget_amounts(conn, index: AccountIndex, budget: Budget, periods: list[int], prices: dict | None = None):
    """Explicit budget amount per account (summed over `periods`, book currency, income positive)."""
    if prices is None:
        prices = latest_prices(conn)
    base = index.root.commodity
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    wanted = set(periods)
    for ag, pn, num, den in conn.execute(text(
            "SELECT account_guid, period_num, amount_num, amount_denom FROM budget_amounts WHERE budget_guid = :b"),
            {"b": budget.guid}):
        if int(pn) not in wanted:
            continue
        acc = index.get(ag)
        if acc is None or acc.type not in KINDS:
            continue
        v = convert(gnc_decimal(int(num), int(den or 1)), acc.commodity, base, prices)
        if v is None:
            continue
        # income budgets are stored either positive (classic) or negative (natural signs) – show both positive
        out[ag] += abs(v) if acc.type == "INCOME" else v
    return dict(out)


def budget_rollup(index: AccountIndex, explicit: dict) -> dict[str, Decimal | None]:
    """Budget per account: its own amount if it has one, else the sum of its sub-accounts (None = no budget)."""
    result: dict[str, Decimal | None] = {}

    def visit(acc: Account):
        kids = [visit(c) for c in acc.children]
        if acc.guid in explicit:
            v = explicit[acc.guid]
        elif any(k is not None for k in kids):
            v = sum((k for k in kids if k is not None), ZERO)
        else:
            v = None
        result[acc.guid] = v
        return v

    for top in index.top_level():
        visit(top)
    return result


@dataclass
class BudgetRow:
    account: Account
    budget: Decimal | None
    actual: Decimal
    kind: str

    @property
    def variance(self) -> Decimal:
        """Positive = better than planned (less spent / more earned)."""
        b = self.budget or ZERO
        return b - self.actual if self.kind == "EXPENSE" else self.actual - b

    @property
    def percent(self) -> float | None:
        if not self.budget:
            return None
        return float(self.actual / self.budget * 100)


def budget_rows(index: AccountIndex, kind: str, depth: int, budgets: dict, actual: dict) -> list[BudgetRow]:
    rows = []
    for acc in categories_at(index, kind, depth):
        b = budgets.get(acc.guid)
        a = actual.get(acc.guid, ZERO)
        if b is None and a == 0:
            continue
        rows.append(BudgetRow(acc, b, a, kind))
    rows.sort(key=lambda r: (r.budget is None, -(r.budget or ZERO), -r.actual))
    return rows


# ------------------------------------------------------------------------------------------ net worth

NW_PERIODS = ("12m", "ytd", "last_year", "3y", "5y", "all", "custom")


def nw_period_range(period: str, today: date, first: date | None, start: date | None = None,
                    end: date | None = None):
    """(first day, last day) of a net-worth period; "all" starts with the first booking."""
    if period == "3y":
        return add_months(month_start(today), -35), today
    if period == "5y":
        return add_months(month_start(today), -59), today
    if period == "all":
        return month_start(first or today), today
    if period in ("ytd", "last_year", "custom"):
        return period_range(period, today, start, end)
    return period_range("12m", today)


class PriceHistory:
    """All prices of the book; ``at(day)`` gives a price dict like :func:`latest_prices` as of that day."""

    def __init__(self, conn, book: Book):
        rows = defaultdict(list)
        for cg, cug, d, num, den in conn.execute(text(
                "SELECT commodity_guid, currency_guid, date, value_num, value_denom FROM prices")):
            day = book.day_of(d)
            if day is None or not den:
                continue
            rows[(cg, cug)].append((day, gnc_decimal(int(num), int(den))))
        self._rows = {}
        for key, lst in rows.items():
            lst.sort(key=lambda r: r[0])  # same day: the later row wins
            self._rows[key] = ([r[0] for r in lst], [r[1] for r in lst])

    def at(self, day: date) -> dict:
        out = {}
        for key, (days, values) in self._rows.items():
            i = bisect.bisect_right(days, day)
            # before the first known price use the earliest one rather than dropping the account
            out[key] = values[i - 1] if i else values[0]
        return out


@dataclass
class NetWorthPoint:
    key: str          # YYYY-MM
    day: date         # valuation day (month end, or the end of the period)
    assets: Decimal
    liabilities: Decimal  # positive = debt
    groups: dict = field(default_factory=dict)  # level-1 group guid -> value (liabilities positive)

    @property
    def net(self) -> Decimal:
        return self.assets - self.liabilities


@dataclass
class NetWorth:
    points: list
    groups: list          # level-1 balance-sheet accounts (asset groups first)
    unconverted: int = 0  # accounts whose value could not be converted at some point


def balance_groups(index: AccountIndex) -> list[Account]:
    """Level-1 balance-sheet groups. Top-level placeholders ("Aktiva", "Fremdkapital") are replaced by their
    sub-accounts, so the groups are the meaningful ones ("Barvermögen", "Kreditkarte")."""
    out = []
    for types in (ASSET_TYPES, LIABILITY_TYPES):
        level = []
        for top in index.top_level():
            if top.type not in types:
                continue
            kids = [c for c in top.children if c.type in types]
            level.extend(kids if top.placeholder and kids else [top])
        out.extend(sorted(level, key=lambda a: a.name.casefold()))
    return out


def first_booking(conn, book: Book) -> date | None:
    return book.day_of(conn.execute(text("SELECT MIN(post_date) FROM transactions")).scalar())


def net_worth(conn, book: Book, index: AccountIndex, start: date, end: date, opening: bool = True) -> NetWorth:
    """Assets, liabilities and net worth at the end of every month between `start` and `end` (the last point
    is `end` itself), preceded by the opening value on the day before the first month if `opening` is set.

    Balances are quantities in the account commodity, valued with the price valid on the valuation day
    (securities mark-to-market, foreign currencies at the historical rate). Hidden and placeholder accounts
    are included, like in GnuCash's balance sheet.
    """
    base = index.root.commodity
    sheet = {g: a for g, a in index.by_guid.items() if a.type in BALANCE_SHEET_TYPES}
    groups = balance_groups(index)
    group_of = {}
    for grp in groups:
        for a in [grp, *index.descendants(grp)]:
            group_of[a.guid] = grp.guid

    # opening point at the end of the day before `start`, so the change covers the whole period
    first_day = month_start(start) - timedelta(days=1)
    days = [(f"{first_day.year:04d}-{first_day.month:02d}", first_day)] if opening else []
    for key in month_keys(start, end):
        y, m = int(key[:4]), int(key[5:])
        days.append((key, min(add_months(date(y, m, 1), 1) - timedelta(days=1), end)))

    # opening balances before the first month, then movements per account and valuation day
    running: dict[str, Decimal] = defaultdict(lambda: ZERO)
    first_start = book.day_start_utc(month_start(start))
    for ag, den, num in conn.execute(text(
            "SELECT s.account_guid, s.quantity_denom, SUM(s.quantity_num) FROM splits s "
            "JOIN transactions t ON t.guid = s.tx_guid WHERE t.post_date < :s "
            "GROUP BY s.account_guid, s.quantity_denom"), {"s": first_start}):
        if ag in sheet:
            running[ag] += gnc_decimal(int(num or 0), int(den or 1))
    moves: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    ends = [d for _, d in days]
    for ag, qn, qd, pd in conn.execute(text(
            "SELECT s.account_guid, s.quantity_num, s.quantity_denom, t.post_date FROM splits s "
            "JOIN transactions t ON t.guid = s.tx_guid WHERE t.post_date >= :s AND t.post_date < :e"),
            {"s": first_start, "e": book.day_end_utc(end)}):
        if ag not in sheet:
            continue
        i = bisect.bisect_left(ends, book.day_of(pd))
        if i < len(ends):
            moves[i][ag] += gnc_decimal(int(qn), int(qd or 1))

    history = PriceHistory(conn, book)
    points, bad = [], set()
    for i, (key, day) in enumerate(days):
        for ag, v in moves.get(i, {}).items():
            running[ag] += v
        prices = history.at(day)
        assets = liabilities = ZERO
        grp_vals: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for ag, qty in running.items():
            if not qty:
                continue
            acc = sheet[ag]
            v = convert(qty, acc.commodity, base, prices)
            if v is None:
                bad.add(ag)
                continue
            if acc.type in LIABILITY_TYPES:
                liabilities -= v
                if ag in group_of:
                    grp_vals[group_of[ag]] -= v
            else:
                assets += v
                if ag in group_of:
                    grp_vals[group_of[ag]] += v
        points.append(NetWorthPoint(key, day, assets, liabilities, dict(grp_vals)))
    return NetWorth(points, groups, len(bad))


def nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """Axis ticks at multiples of 1, 2 or 5 × 10^n covering [lo, hi]."""
    if hi < lo:
        lo, hi = hi, lo
    span = hi - lo or abs(hi) or 1.0
    rough = span / max(count - 1, 1)
    mag = 10 ** math.floor(math.log10(rough))
    residual = rough / mag
    step = mag * (1 if residual <= 1.5 else 2 if residual <= 3.5 else 5 if residual <= 7.5 else 10)
    first = math.floor(lo / step) * step
    ticks, v = [], first
    while v < hi + step * 0.999:
        ticks.append(round(v, 10))
        v += step
    if len(ticks) < 2:
        ticks.append(round(first + step, 10))
    return ticks


def line_chart(points: list[NetWorthPoint], series: tuple = ("net",), width: float = 900, height: float = 300):
    """Coordinates for an SVG line chart of the net worth series (server-side, no JS library)."""
    if not points:
        return None
    vals = {s: [float(getattr(p, s)) for p in points] for s in series}
    every = [v for lst in vals.values() for v in lst] + [0.0]
    ticks = nice_ticks(min(every), max(every))
    y0, y1 = ticks[0], ticks[-1]
    n = len(points)
    step = width / (n - 1) if n > 1 else 0

    def x(i):
        return i * step if n > 1 else width / 2

    def y(v):
        return height - (v - y0) / (y1 - y0) * height if y1 != y0 else height / 2

    lines = {}
    for s, lst in vals.items():
        coords = [(x(i), y(v)) for i, v in enumerate(lst)]
        lines[s] = {"points": " ".join(f"{a:.1f},{b:.1f}" for a, b in coords),
                    "dots": [{"x": a, "y": b, "p": p} for (a, b), p in zip(coords, points)]}
    zero = y(0.0)
    if "net" in lines:
        pts = lines["net"]["points"]
        lines["net"]["area"] = f"M{x(0):.1f},{zero:.1f} L{pts.replace(' ', ' L')} L{x(n - 1):.1f},{zero:.1f} Z"
    # x labels: at most ~12, prefer January
    every_n = max(1, math.ceil(n / 12))
    labels = [{"x": x(i), "p": p} for i, p in enumerate(points)
              if n <= 12 or (i % every_n == 0 if n <= 36 else p.key.endswith(("-01", "-07")) if n <= 72
                             else p.key.endswith("-01"))]
    return {"width": width, "height": height, "lines": lines, "zero": zero,
            "ticks": [{"v": t, "y": y(t), "text": fmt(Decimal(str(t)), 0)} for t in ticks], "labels": labels, "step": step}
