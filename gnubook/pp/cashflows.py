"""Deposits and removals from the GnuCash book into the Portfolio Performance file.

The other direction of the link (pp/sync.py) books PP's securities transactions into GnuCash, but not PP's
deposits and removals. When a PP cash account has its own GnuCash account (PPSettings.cash_accounts), the money
paid into or taken out of that account is entered in GnuCash – and PP needs it for its performance figures
(without deposits a purchase drives PP's cash account below zero).

Every GnuCash transaction on such an account that gnubook did not book from PP becomes a PP cash account
transaction:

* counter account an asset, liability or equity account (e.g. the current account): DEPOSIT / REMOVAL;
* an income account: INTEREST / INTEREST_CHARGE;
* the taxes account of the link: TAXES / TAX_REFUND; the interest charges account: INTEREST_CHARGE / INTEREST;
  another expense account: FEES / FEES_REFUND;
* the clearing account: nothing (bank-import lines of securities business, booked from PP anyway).

A transaction with several counter splits gives one PP transaction per counter split. One that touches several
mapped accounts gives one DEPOSIT or REMOVAL per mapped split.

GnuCash is the master of these PP transactions. They carry gnubook's key in PP's "source" field
(``gnubook:<split guid>[:<counter split guid>]``); each run compares what GnuCash holds with what the PP file
holds and creates, replaces or deletes PP transactions until both agree. Nothing is remembered outside the two
files, so the link survives the loss of gnubook's data. PP transactions with this source are never booked back
into GnuCash (pp/plan.py).
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import text

from ..i18n import _
from ..money import ZERO, gnc_decimal
from .settings import PPSettings, resolved

SOURCE_PREFIX = "gnubook:"
SLOT = "gnubook-pp"  # the slot of the bookings made from PP (pp/sync.py)
TRANSFER_TYPES = {"ASSET", "BANK", "CASH", "LIABILITY", "CREDIT", "EQUITY", "RECEIVABLE", "PAYABLE", "STOCK",
                  "MUTUAL", "TRADING"}
REPLACEABLE = ("DEPOSIT", "REMOVAL")


@dataclass(frozen=True)
class Flow:
    """One PP cash account transaction wanted because of the GnuCash book."""
    key: str           # source in PP without the prefix
    account: str       # PP cash account uuid
    type: str          # DEPOSIT, REMOVAL, ...
    day: date
    amount: Decimal    # positive
    note: str
    tx_guid: str

    @property
    def source(self) -> str:
        return SOURCE_PREFIX + self.key

    def payload(self) -> dict:
        return {"account": self.account, "type": self.type, "date": self.day.isoformat(),
                "amount": format(self.amount, "f"), "note": self.note, "source": self.source}

    def same_as(self, t: dict) -> bool:
        return (t.get("owner") == self.account and t.get("type") == self.type
                and (t.get("date") or "")[:10] == self.day.isoformat()
                and _dec(t.get("amount")) == self.amount and (t.get("note") or "") == self.note)


@dataclass
class Mirror:
    flows: list = field(default_factory=list)       # all wanted Flows
    create: list = field(default_factory=list)      # Flows missing (or different) in PP
    delete: list = field(default_factory=list)      # PP transaction uuids to delete
    replaced: list = field(default_factory=list)    # of delete: PP's own deposits/removals (cashflows_replace)
    unchanged: int = 0
    skipped: list = field(default_factory=list)     # (day, description, reason)

    @property
    def pending(self) -> bool:
        return bool(self.create or self.delete)

    @property
    def updated(self) -> int:
        """Flows whose PP transaction is replaced (counted in create and delete)."""
        return len({f.key for f in self.create} & self._changed_keys)

    _changed_keys: set = field(default_factory=set)

    def fingerprint(self) -> str:
        rows = sorted((f.key, f.account, f.type, f.day.isoformat(), str(f.amount), f.note) for f in self.flows)
        return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def _dec(v) -> Decimal | None:
    try:
        return Decimal(str(v)).normalize() if v not in (None, "") else None
    except ArithmeticError:
        return None


def _type_for(counter, amount: Decimal, roles: dict) -> str | None:
    """PP type of a flow of amount (positive = money into the mapped account) against the counter account."""
    if counter is None:
        return None
    if counter.type in TRANSFER_TYPES:
        return "DEPOSIT" if amount > 0 else "REMOVAL"
    if counter.type == "INCOME":
        return "INTEREST" if amount > 0 else "INTEREST_CHARGE"
    if counter.type == "EXPENSE":
        if counter.full_name == roles.get("taxes"):
            return "TAX_REFUND" if amount > 0 else "TAXES"
        if counter.full_name == roles.get("interest_charge"):
            return "INTEREST" if amount > 0 else "INTEREST_CHARGE"
        return "FEES_REFUND" if amount > 0 else "FEES"
    return None


def _note(description: str, memo: str) -> str:
    description, memo = (description or "").strip(), (memo or "").strip()
    return f"{description} – {memo}" if memo and memo != description else description


def collect(book, index, settings: PPSettings, export: dict) -> tuple[list, list]:
    """The flows the GnuCash book asks for: ([Flow], [(day, description, reason)] of what is left out)."""
    base = index.root.commodity
    roles = resolved(settings, index)
    clearing = index.find(roles["clearing"])
    pp_accounts = {a["uuid"]: a for a in export.get("accounts") or []}
    mapped = {}  # GnuCash account guid -> PP cash account uuid
    for pp_uuid, ref in settings.cash_accounts.items():
        acc = index.find(ref)
        pp_acc = pp_accounts.get(pp_uuid)
        if acc is None or pp_acc is None or pp_acc.get("currency") != base.mnemonic:
            continue
        mapped.setdefault(acc.guid, pp_uuid)
    if not mapped:
        return [], []
    start = settings.cashflows_start
    today = date.today()
    flows, skipped = [], []
    with book.connect() as conn:
        names = {f"a{i}": g for i, g in enumerate(mapped)}
        clause = "(" + ", ".join(":" + n for n in names) + ")"
        tx_guids = [r[0] for r in conn.execute(text(
            f"SELECT DISTINCT tx_guid FROM splits WHERE account_guid IN {clause}"), names)]
        from_pp = {r[0] for r in conn.execute(text("SELECT obj_guid FROM slots WHERE name = :n"), {"n": SLOT})}
        tx_guids = [g for g in tx_guids if g not in from_pp]
        for i in range(0, len(tx_guids), 400):
            chunk = tx_guids[i:i + 400]
            params = {f"g{j}": g for j, g in enumerate(chunk)}
            inner = "(" + ", ".join(":" + n for n in params) + ")"
            heads = {r[0]: r for r in conn.execute(text(
                f"SELECT guid, currency_guid, post_date, description FROM transactions WHERE guid IN {inner}"),
                params)}
            splits = defaultdict(list)
            for sg, tg, ag, vn, vd, memo in conn.execute(text(
                    f"SELECT guid, tx_guid, account_guid, value_num, value_denom, memo FROM splits "
                    f"WHERE tx_guid IN {inner}"), params):
                splits[tg].append((sg, ag, gnc_decimal(int(vn), int(vd or 1)), memo or ""))
            for tg in chunk:
                _g, cur, pd, desc = heads[tg]
                day = book.day_of(pd)
                desc = desc or ""
                if start is not None and day < start:
                    continue
                if day > today:
                    skipped.append((day, desc, _("liegt in der Zukunft – wird am Buchungstag übernommen")))
                    continue
                if cur != base.guid:
                    skipped.append((day, desc, _("Buchung nicht in {c}", c=base.mnemonic)))
                    continue
                mine = [s for s in splits[tg] if s[1] in mapped]
                others = [s for s in splits[tg] if s[1] not in mapped]
                if len(mine) == 1:
                    sg, ag, _v, memo = mine[0]
                    for og, oa, ov, omemo in others:
                        amount = -ov
                        if amount == ZERO:
                            continue
                        counter = index.get(oa)
                        if clearing is not None and oa == clearing.guid:
                            skipped.append((day, desc, _("Gegenkonto ist das Zwischenkonto")))
                            continue
                        typ = _type_for(counter, amount, roles)
                        if typ is None:
                            skipped.append((day, desc, _("Gegenkonto unbekannter Art")))
                            continue
                        key = sg if len(others) == 1 else f"{sg}:{og}"
                        flows.append(Flow(key, mapped[ag], typ, day, abs(amount).normalize(),
                                          _note(desc, memo or omemo), tg))
                else:  # several mapped accounts in one transaction: each split on its own
                    for sg, ag, v, memo in mine:
                        if v == ZERO:
                            continue
                        flows.append(Flow(sg, mapped[ag], "DEPOSIT" if v > 0 else "REMOVAL", day,
                                          abs(v).normalize(), _note(desc, memo), tg))
    return flows, skipped


def plan_mirror(book, index, settings: PPSettings, export: dict) -> Mirror:
    """Compare the flows of the book with the PP file."""
    flows, skipped = collect(book, index, settings, export)
    m = Mirror(flows=flows, skipped=skipped)
    mirrored_accounts = {f.account for f in flows} | {
        u for u in settings.cash_accounts if u in {a["uuid"] for a in export.get("accounts") or []}}
    start = settings.cashflows_start
    in_pp = {}
    for t in export.get("transactions") or []:
        if t.get("kind") != "account":
            continue
        src = t.get("source") or ""
        if src.startswith(SOURCE_PREFIX):
            key = src[len(SOURCE_PREFIX):]
            if key in in_pp:  # a duplicate (e.g. the file was uploaded twice): keep one
                m.delete.append(t["uuid"])
            else:
                in_pp[key] = t
        elif (settings.cashflows_replace and t.get("type") in REPLACEABLE and not t.get("cross")
              and t.get("owner") in mirrored_accounts
              and (start is None or (t.get("date") or "")[:10] >= start.isoformat())):
            m.delete.append(t["uuid"])
            m.replaced.append(t["uuid"])
    wanted = {f.key: f for f in flows}
    for key, f in wanted.items():
        t = in_pp.get(key)
        if t is None:
            m.create.append(f)
        elif f.same_as(t):
            m.unchanged += 1
        else:
            m.delete.append(t["uuid"])
            m.create.append(f)
            m._changed_keys.add(key)
    for key, t in in_pp.items():
        if key not in wanted:
            m.delete.append(t["uuid"])
    return m


def apply(client, cid: str, mirror: Mirror) -> dict:
    """Write the differences into the PP file (one save in pp-core)."""
    if not mirror.pending:
        return {}
    return client.apply_cashflows(cid, [f.payload() for f in mirror.create], list(mirror.delete))
