"""Read-side queries: balances, registers, transactions, search."""
from __future__ import annotations

from .i18n import gettext as _

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import text

from .book import Account, AccountIndex, Book, convert
from .money import ZERO, gnc_decimal

CHUNK = 400  # stay below SQLite's bound-parameter limit
_MIN_DT = datetime.min


def _chunks(seq, n=CHUNK):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _in_clause(prefix: str, values) -> tuple[str, dict]:
    names = [f"{prefix}{i}" for i in range(len(values))]
    return "(" + ", ".join(":" + n for n in names) + ")", dict(zip(names, values))


# ---------------------------------------------------------------------------------------- balances

def balances(conn, book: Book, upto: date | None = None, before_tx: bool = False) -> dict[str, Decimal]:
    """Own balance (quantity, account commodity) per account; optionally only up to and including day `upto`."""
    params = {}
    if upto is None:
        sql = ("SELECT account_guid, quantity_denom, SUM(quantity_num) FROM splits "
               "GROUP BY account_guid, quantity_denom")
    else:
        sql = ("SELECT s.account_guid, s.quantity_denom, SUM(s.quantity_num) FROM splits s "
               "JOIN transactions t ON t.guid = s.tx_guid WHERE t.post_date < :end "
               "GROUP BY s.account_guid, s.quantity_denom")
        params["end"] = book.day_end_utc(upto)
    out: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for guid, denom, num in conn.execute(text(sql), params):
        out[guid] += gnc_decimal(int(num or 0), int(denom or 1))
    return dict(out)


@dataclass
class Total:
    value: Decimal
    complete: bool = True  # False when a sub-account in another commodity could not be converted


def tree_totals(index: AccountIndex, own: dict[str, Decimal], prices: dict | None = None) -> dict[str, Total]:
    """Balance of each account including its sub-accounts, in the account's own commodity."""
    totals: dict[str, Total] = {}

    def visit(acc: Account) -> Total:
        total = Total(own.get(acc.guid, ZERO))
        for child in acc.children:
            sub = visit(child)
            conv = convert(sub.value, child.commodity, acc.commodity, prices)
            if conv is None:
                total.complete = False
            else:
                total.value += conv
            total.complete = total.complete and sub.complete
        totals[acc.guid] = total
        return total

    for top in index.top_level():
        visit(top)
    return totals


# ---------------------------------------------------------------------------------------- register

MULTI = "— Mehrere Konten —"


@dataclass
class RegisterRow:
    split_guid: str
    tx_guid: str
    day: date
    num: str
    description: str
    memo: str
    reconcile: str
    amount: Decimal          # in the account's commodity, GnuCash sign (debit positive)
    balance: Decimal = ZERO  # running balance after this row
    enter_date: object = None
    counter: str = ""
    counter_guid: str | None = None
    others: list = field(default_factory=list)
    has_notes: bool = False


def register_rows(conn, book: Book, index: AccountIndex, account: Account,
                  include_children: bool = False) -> list[RegisterRow]:
    """All splits of an account in chronological order with running balance."""
    guids = [account.guid] + ([a.guid for a in index.descendants(account)] if include_children else [])
    rows: list[RegisterRow] = []
    for chunk in _chunks(guids):
        clause, params = _in_clause("a", chunk)
        res = conn.execute(text(
            "SELECT s.guid, s.tx_guid, t.post_date, t.num, t.description, s.memo, s.reconcile_state, "
            "s.quantity_num, s.quantity_denom, t.enter_date, s.account_guid "
            f"FROM splits s JOIN transactions t ON t.guid = s.tx_guid WHERE s.account_guid IN {clause}"), params)
        for r in res:
            amount = gnc_decimal(r[7], r[8])
            if include_children and r[10] != account.guid:
                child = index.get(r[10])
                conv = convert(amount, child.commodity if child else None, account.commodity)
                amount = conv if conv is not None else ZERO
            rows.append(RegisterRow(r[0], r[1], book.day_of(r[2]), r[3] or "", r[4] or "", r[5] or "",
                                    r[6] or "n", amount, enter_date=book.to_utc_naive(r[9])))
    rows.sort(key=lambda x: (x.day, x.enter_date or _MIN_DT, x.tx_guid, x.split_guid))
    running = ZERO
    for row in rows:
        running += row.amount
        row.balance = running
    return rows


def annotate_rows(conn, index: AccountIndex, rows: list[RegisterRow]):
    """Fill counter account and notes flag for the given (displayed) rows."""
    tx_guids = sorted({r.tx_guid for r in rows})
    splits_by_tx = defaultdict(list)
    notes = set()
    for chunk in _chunks(tx_guids):
        clause, params = _in_clause("t", chunk)
        for sg, tg, ag in conn.execute(text(
                f"SELECT guid, tx_guid, account_guid FROM splits WHERE tx_guid IN {clause}"), params):
            splits_by_tx[tg].append((sg, ag))
        for (og,) in conn.execute(text(
                f"SELECT obj_guid FROM slots WHERE name = 'notes' AND obj_guid IN {clause} "
                "AND string_val IS NOT NULL AND string_val <> ''"), params):
            notes.add(og)
    for row in rows:
        others = [(sg, ag) for sg, ag in splits_by_tx[row.tx_guid] if sg != row.split_guid]
        names = []
        for _sg, ag in others:
            acc = index.get(ag)
            names.append(acc.full_name if acc else "?")
        row.others = names
        if len(others) == 1:
            row.counter = names[0]
            row.counter_guid = others[0][1]
        elif len(others) > 1:
            distinct = sorted(set(names))
            row.counter = distinct[0] if len(distinct) == 1 else MULTI
        row.has_notes = row.tx_guid in notes


# ---------------------------------------------------------------------------------------- transactions

@dataclass
class SplitView:
    guid: str
    account_guid: str
    account: Account | None
    memo: str
    action: str
    reconcile: str
    value: Decimal
    quantity: Decimal
    value_num: int
    value_denom: int
    quantity_num: int
    quantity_denom: int
    lot_guid: str | None


@dataclass
class TxView:
    guid: str
    currency_guid: str
    currency: object
    num: str
    day: date
    post_date_raw: object
    enter_date: object
    description: str
    notes: str
    splits: list[SplitView]
    txn_type: str = ""
    sched: bool = False
    fingerprint: str = ""
    readonly_reasons: list[str] = field(default_factory=list)

    @property
    def editable(self) -> bool:
        return not self.readonly_reasons

    @property
    def total_debit(self) -> Decimal:
        return sum((s.value for s in self.splits if s.value > 0), ZERO)


def load_transaction(conn, book: Book, index: AccountIndex, guid: str) -> TxView | None:
    row = conn.execute(text(
        "SELECT guid, currency_guid, num, post_date, enter_date, description FROM transactions WHERE guid = :g"),
        {"g": guid}).fetchone()
    if row is None:
        return None
    splits = []
    for r in conn.execute(text(
            "SELECT guid, account_guid, memo, action, reconcile_state, value_num, value_denom, quantity_num, "
            "quantity_denom, lot_guid FROM splits WHERE tx_guid = :g ORDER BY guid"), {"g": guid}):
        splits.append(SplitView(r[0], r[1], index.get(r[1]), r[2] or "", r[3] or "", r[4] or "n",
                                gnc_decimal(r[5], r[6]), gnc_decimal(r[7], r[8]), int(r[5]), int(r[6]),
                                int(r[7]), int(r[8]), r[9]))
    slots = {name: val for name, val in conn.execute(text(
        "SELECT name, string_val FROM slots WHERE obj_guid = :g AND name IN "
        "('notes', 'trans-txn-type', 'from-sched-xaction', 'trans-read-only')"), {"g": guid})}
    sched = conn.execute(text(
        "SELECT COUNT(*) FROM slots WHERE obj_guid = :g AND name = 'from-sched-xaction'"), {"g": guid}).scalar()
    currency = index.commodities.get(row[1])
    tx = TxView(row[0], row[1], currency, row[2] or "", book.day_of(row[3]), row[3], book.to_utc_naive(row[4]),
                row[5] or "", slots.get("notes") or "", splits, slots.get("trans-txn-type") or "", bool(sched))
    # GnuCash Desktop order: debits first, then credits
    tx.splits.sort(key=lambda s: (s.value < 0, s.account.full_name if s.account else "", s.guid))
    tx.fingerprint = fingerprint(tx)
    reasons = []
    if tx.txn_type:
        reasons.append(_("Rechnung/Zahlung aus dem GnuCash-Geschäftsmodul"))
    if slots.get("trans-read-only"):
        reasons.append(_("in GnuCash als schreibgeschützt markiert"))
    if any(s.lot_guid for s in splits):
        reasons.append("Splits sind Losen (Lots) zugeordnet")
    if currency is None or not currency.is_currency:
        reasons.append(_("unbekannte Transaktionswährung"))
    for s in splits:
        if s.account is None:
            reasons.append(_("Split auf einem unbekannten Konto"))
            break
        if s.account.commodity_guid != tx.currency_guid:
            reasons.append(_("Split in Fremdwährung/Wertpapier ({a0})", a0=s.account.full_name))
            break
    tx.readonly_reasons = reasons
    return tx


def fingerprint(tx: TxView) -> str:
    """Hash of the stored state – used to detect concurrent changes (e.g. in GnuCash Desktop)."""
    parts = [tx.guid, tx.currency_guid, tx.num, str(tx.post_date_raw), tx.description, tx.notes]
    for s in sorted(tx.splits, key=lambda s: s.guid):
        parts += [s.guid, s.account_guid, s.memo, s.action, s.reconcile, str(s.value_num), str(s.value_denom),
                  str(s.quantity_num), str(s.quantity_denom), str(s.lot_guid)]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def current_fingerprint(conn, book: Book, index: AccountIndex, guid: str) -> str | None:
    tx = load_transaction(conn, book, index, guid)
    return tx.fingerprint if tx else None


# ---------------------------------------------------------------------------------------- lists & search

@dataclass
class TxListItem:
    tx_guid: str
    day: date
    num: str
    description: str
    amount: Decimal              # sum of debit values (transaction currency)
    currency: str
    accounts: list[str]
    enter_date: object = None


def _tx_items(conn, book: Book, index: AccountIndex, tx_rows) -> list[TxListItem]:
    items = {r[0]: TxListItem(r[0], book.day_of(r[1]), r[2] or "", r[3] or "", ZERO,
                              (index.commodities.get(r[4]).mnemonic if index.commodities.get(r[4]) else ""),
                              [], book.to_utc_naive(r[5]))
             for r in tx_rows}
    for chunk in _chunks(items.keys()):
        clause, params = _in_clause("t", chunk)
        for tg, ag, vn, vd in conn.execute(text(
                f"SELECT tx_guid, account_guid, value_num, value_denom FROM splits WHERE tx_guid IN {clause}"),
                params):
            item = items[tg]
            v = gnc_decimal(vn, vd)
            if v > 0:
                item.amount += v
            acc = index.get(ag)
            name = acc.full_name if acc else "?"
            if name not in item.accounts:
                item.accounts.append(name)
    return list(items.values())


def recent_transactions(conn, book: Book, index: AccountIndex, limit: int = 10) -> list[TxListItem]:
    rows = conn.execute(text(
        "SELECT guid, post_date, num, description, currency_guid, enter_date FROM transactions "
        "ORDER BY enter_date DESC, guid LIMIT :n"), {"n": limit}).fetchall()
    items = _tx_items(conn, book, index, rows)
    items.sort(key=lambda i: (i.enter_date or _MIN_DT), reverse=True)
    return items


def search_transactions(conn, book: Book, index: AccountIndex, q: str, limit: int = 200,
                        date_from: date | None = None, date_to: date | None = None) -> list[TxListItem]:
    """Search description, num, split memos, notes and (if q is an amount) split values."""
    from .money import AmountError, parse_amount

    q = (q or "").strip()
    if not q:
        return []
    like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    guids = set()
    params = {"q": like}
    for sql in (
        "SELECT guid FROM transactions WHERE lower(description) LIKE :q ESCAPE '\\' OR lower(num) LIKE :q ESCAPE '\\'",
        "SELECT tx_guid FROM splits WHERE lower(memo) LIKE :q ESCAPE '\\'",
        "SELECT obj_guid FROM slots WHERE name = 'notes' AND lower(string_val) LIKE :q ESCAPE '\\'",
    ):
        guids.update(r[0] for r in conn.execute(text(sql), params))
    try:
        amount = parse_amount(q)
    except AmountError:
        amount = None
    if amount is not None and amount != 0:
        cents = int((abs(amount) * 100).to_integral_value())
        guids.update(r[0] for r in conn.execute(text(
            "SELECT tx_guid FROM splits WHERE value_num * 100 = :c * value_denom "
            "OR value_num * 100 = -:c * value_denom"), {"c": cents}))
    if not guids:
        return []
    rows = []
    for chunk in _chunks(sorted(guids)):
        clause, params = _in_clause("t", chunk)
        rows += conn.execute(text(
            "SELECT guid, post_date, num, description, currency_guid, enter_date FROM transactions "
            f"WHERE guid IN {clause}"), params).fetchall()
    items = _tx_items(conn, book, index, rows)
    if date_from:
        items = [i for i in items if i.day >= date_from]
    if date_to:
        items = [i for i in items if i.day <= date_to]
    items.sort(key=lambda i: (i.day, i.enter_date or _MIN_DT), reverse=True)
    return items[:limit]


def description_suggestions(conn, prefix: str, limit: int = 15) -> list[str]:
    like = prefix.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = conn.execute(text(
        "SELECT description, MAX(enter_date) AS last FROM transactions WHERE lower(description) LIKE :q ESCAPE '\\' "
        "GROUP BY description ORDER BY last DESC LIMIT :n"), {"q": like, "n": limit}).fetchall()
    return [r[0] for r in rows if r[0]]


def latest_by_description(conn, description: str) -> str | None:
    row = conn.execute(text(
        "SELECT guid FROM transactions WHERE description = :d ORDER BY post_date DESC, enter_date DESC LIMIT 1"),
        {"d": description}).fetchone()
    return row[0] if row else None


def monthly_income_expense(conn, book: Book, index: AccountIndex, months: int = 12):
    """[(YYYY-MM, income, expense)] for the last `months` months incl. the current one (book currency)."""
    today = book.today()
    y, m = today.year, today.month
    keys = []
    for _i in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    start = date(int(keys[0][:4]), int(keys[0][5:]), 1)
    inc = defaultdict(lambda: ZERO)
    exp = defaultdict(lambda: ZERO)
    acc_types = {g: a.type for g, a in index.by_guid.items() if a.type in ("INCOME", "EXPENSE")}
    if not acc_types:
        return [(k, ZERO, ZERO) for k in keys]
    res = conn.execute(text(
        "SELECT s.account_guid, s.value_num, s.value_denom, t.post_date FROM splits s "
        "JOIN transactions t ON t.guid = s.tx_guid WHERE t.post_date >= :start"),
        {"start": book.day_start_utc(start)})
    for ag, vn, vd, pd in res:
        typ = acc_types.get(ag)
        if not typ:
            continue
        d = book.day_of(pd)
        key = f"{d.year:04d}-{d.month:02d}"
        v = gnc_decimal(vn, vd)
        if typ == "INCOME":
            inc[key] -= v
        else:
            exp[key] += v
    return [(k, inc[k], exp[k]) for k in keys]
