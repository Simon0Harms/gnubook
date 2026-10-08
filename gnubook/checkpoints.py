"""Bank balance checkpoints.

Banks put the statement balance into the booking text of the monthly closing line, e.g.

    ENTGELTABSCHLUSS **ENDSALDO** 1.234,56H STAND29.05.2026 1.239,51H      (Sparkassen-style)
    ... Kontostand am 28.03.2024 47,11 + ...                                 (direct-bank style)

STAND/Kontostand is the balance at the end of the given day *before* the closing line itself,
ENDSALDO the balance *after* it (fees included). gnubook recomputes both from the book after every
change and reports differences (difference = book - bank).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import text

from .book import AccountIndex, Book
from .money import ZERO, gnc_decimal

_AMOUNT = r"(\d{1,3}(?:\.\d{3})+,\d{2}|\d+,\d{2})"
_NOT_LETTER_BEFORE = r"(?<![^\W\d_])"   # not preceded by a letter
_NO_ALNUM_AFTER = r"(?![^\W_])"          # not followed by a letter or digit

RX_STAND = re.compile(_NOT_LETTER_BEFORE + r"STAND\s*(\d{2})\.(\d{2})\.(\d{4})\s+" + _AMOUNT + r"\s*([HS])"
                      + _NO_ALNUM_AFTER, re.IGNORECASE)
RX_KONTOSTAND = re.compile(_NOT_LETTER_BEFORE + r"Kontostand\s+am\s+(\d{2})\.(\d{2})\.(\d{4})\s+(-?)" + _AMOUNT
                           + r"\s*([+-])?(?![\d,])", re.IGNORECASE)
RX_ENDSALDO = re.compile(_NOT_LETTER_BEFORE + r"\*{0,2}ENDSALDO\*{0,2}\s*" + _AMOUNT + r"\s*([HS])"
                         + _NO_ALNUM_AFTER, re.IGNORECASE)


def _amount(s: str) -> Decimal:
    return Decimal(s.replace(".", "").replace(",", "."))


def _date(d: str, m: str, y: str) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


@dataclass(frozen=True)
class ParsedCheckpoint:
    stand_date: date
    stand: Decimal
    endsaldo: Decimal | None = None


def parse(description: str | None) -> ParsedCheckpoint | None:
    """Extract (STAND date, STAND amount, ENDSALDO amount) from a booking text; None if there is none."""
    if not description:
        return None
    stand_date = stand = None
    m = RX_STAND.search(description)
    if m:
        stand_date = _date(m.group(1), m.group(2), m.group(3))
        if stand_date is not None:
            stand = _amount(m.group(4))
            if m.group(5).upper() == "S":
                stand = -stand
    if stand_date is None:
        m = RX_KONTOSTAND.search(description)
        if m:
            stand_date = _date(m.group(1), m.group(2), m.group(3))
            if stand_date is not None:
                stand = _amount(m.group(5))
                if m.group(4) == "-" or m.group(6) == "-":
                    stand = -stand
    if stand_date is None:
        return None
    endsaldo = None
    m = RX_ENDSALDO.search(description)
    if m:
        endsaldo = _amount(m.group(1))
        if m.group(2).upper() == "S":
            endsaldo = -endsaldo
    return ParsedCheckpoint(stand_date, stand, endsaldo)


@dataclass
class Checkpoint:
    account_guid: str
    tx_guid: str
    tx_day: date
    description: str
    stand_date: date
    bank_stand: Decimal
    book_stand: Decimal
    bank_end: Decimal | None
    book_end: Decimal | None
    tx_amount: Decimal
    accepted: dict | None = None   # acceptance record (diffs at acceptance time, note)
    prev_diff: Decimal = ZERO      # stand difference of the previous checkpoint of the account

    @property
    def diff_stand(self) -> Decimal:
        return self.book_stand - self.bank_stand

    @property
    def diff_end(self) -> Decimal:
        if self.bank_end is None or self.book_end is None:
            return ZERO
        return self.book_end - self.bank_end

    @property
    def ok(self) -> bool:
        return self.diff_stand == 0 and self.diff_end == 0

    @property
    def is_accepted(self) -> bool:
        return (not self.ok and self.accepted is not None
                and Decimal(self.accepted["diff_stand"]) == self.diff_stand
                and Decimal(self.accepted["diff_end"]) == self.diff_end)

    @property
    def status(self) -> str:
        if self.ok:
            return "ok"
        return "accepted" if self.is_accepted else "open"

    @property
    def new_diff(self) -> Decimal:
        """Part of the difference that appeared since the previous checkpoint."""
        return self.diff_stand - self.prev_diff


@dataclass
class AccountCheck:
    account_guid: str
    checkpoints: list[Checkpoint] = field(default_factory=list)

    @property
    def counts(self) -> dict:
        c = {"ok": 0, "accepted": 0, "open": 0}
        for cp in self.checkpoints:
            c[cp.status] += 1
        return c

    @property
    def last(self) -> Checkpoint | None:
        return self.checkpoints[-1] if self.checkpoints else None


CANDIDATE_SQL = ("SELECT guid, description, post_date FROM transactions "
                 "WHERE lower(description) LIKE '%stand%'")


def find_checkpoint_transactions(conn, book: Book, index: AccountIndex, account_guids=None):
    """{account_guid: [(tx_guid, tx_day, description, parsed)]} for balance-sheet accounts."""
    found = defaultdict(list)
    candidates = [(g, d, book.day_of(pd)) for g, d, pd in conn.execute(text(CANDIDATE_SQL))]
    parsed = {g: (d, day, parse(d)) for g, d, day in candidates}
    parsed = {g: v for g, v in parsed.items() if v[2] is not None}
    if not parsed:
        return found
    tx_guids = list(parsed)
    for i in range(0, len(tx_guids), 400):
        chunk = tx_guids[i:i + 400]
        names = {f"t{j}": g for j, g in enumerate(chunk)}
        clause = "(" + ", ".join(":" + n for n in names) + ")"
        seen = set()
        for tg, ag in conn.execute(text(f"SELECT tx_guid, account_guid FROM splits WHERE tx_guid IN {clause}"),
                                   names):
            acc = index.get(ag)
            if acc is None or not acc.is_balance_sheet or (tg, ag) in seen:
                continue
            if account_guids is not None and ag not in account_guids:
                continue
            seen.add((tg, ag))
            desc, day, p = parsed[tg]
            found[ag].append((tg, day, desc, p))
    return found


def evaluate(conn, book: Book, index: AccountIndex, account_guids=None, acceptances: dict | None = None
             ) -> dict[str, AccountCheck]:
    """Evaluate all checkpoints (optionally only for some accounts)."""
    acceptances = acceptances or {}
    found = find_checkpoint_transactions(conn, book, index, account_guids)
    result = {}
    for ag, items in found.items():
        # day-wise sums of the account
        per_day = defaultdict(lambda: ZERO)
        per_tx = defaultdict(lambda: ZERO)
        tx_day = {}
        for tg, pd, qn, qd in conn.execute(text(
                "SELECT s.tx_guid, t.post_date, s.quantity_num, s.quantity_denom FROM splits s "
                "JOIN transactions t ON t.guid = s.tx_guid WHERE s.account_guid = :a"), {"a": ag}):
            d = book.day_of(pd)
            v = gnc_decimal(qn, qd)
            per_day[d] += v
            per_tx[tg] += v
            tx_day[tg] = d
        days = sorted(per_day)
        cumulative = []
        running = ZERO
        for d in days:
            running += per_day[d]
            cumulative.append((d, running))

        def balance_through(day: date) -> Decimal:
            # binary search: sum of all days <= day
            lo, hi = 0, len(cumulative)
            while lo < hi:
                mid = (lo + hi) // 2
                if cumulative[mid][0] <= day:
                    lo = mid + 1
                else:
                    hi = mid
            return cumulative[lo - 1][1] if lo else ZERO

        check = AccountCheck(ag)
        for tg, day, desc, p in sorted(items, key=lambda x: (x[3].stand_date, x[1], x[0])):
            own = per_tx[tg]
            book_stand = balance_through(p.stand_date)
            if tx_day.get(tg, day) <= p.stand_date:
                book_stand -= own  # the closing line itself is not part of STAND
            book_end = book_stand + own if p.endsaldo is not None else None
            cp = Checkpoint(ag, tg, day, desc, p.stand_date, p.stand, book_stand, p.endsaldo, book_end, own,
                            accepted=acceptances.get((ag, tg)))
            check.checkpoints.append(cp)
        prev = ZERO
        for cp in check.checkpoints:
            cp.prev_diff = prev
            prev = cp.diff_stand
        result[ag] = check
    return result


def summary(checks: dict[str, AccountCheck]) -> dict:
    total = {"ok": 0, "accepted": 0, "open": 0, "accounts": len(checks)}
    for check in checks.values():
        for k, v in check.counts.items():
            total[k] += v
    total["checked"] = total["ok"] + total["accepted"] + total["open"]
    return total
