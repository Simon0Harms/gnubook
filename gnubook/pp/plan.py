"""From a pp-core export to the GnuCash bookings gnubook keeps in sync – pure computation, no database.

The rules (see docs/PORTFOLIO-PERFORMANCE.md):

* Every securities account (PP "Depot") becomes a placeholder below the securities root, every security held
  in it an account of type Aktie/Fonds (STOCK/MUTUAL) with the security as commodity.
* The cash side of buys, sales, dividends, interest, fees and taxes goes to the clearing account
  ("Wertpapier-Verrechnung"), or to the GnuCash account chosen for that PP cash account. The bank import books
  the bank lines of the cash account against the same clearing account, so nothing is booked twice.
* Deposits, withdrawals and transfers between PP cash accounts are not booked: the bank import has them.
* Fees and taxes go to their expense accounts. A sale also books the realised gain (FIFO, per securities
  account, on the values before fees) like GnuCash's stock assistant: a split without shares on the security's
  account and the gain on the income account. Optionally purchase fees are capitalised instead.
* With a start date ("sync_from") earlier transactions are not booked; the holdings on the day before become
  one opening booking per security, valued at their FIFO cost.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext

from ..i18n import _

ZERO = Decimal(0)
CENT = Decimal("0.01")

# role names of the fixed accounts (see settings.PPSettings)
ROLES = ("clearing", "dividends", "interest", "gains", "fees", "taxes", "interest_charge", "delivery")

ACCOUNT_TYPES = ("DIVIDENDS", "INTEREST", "INTEREST_CHARGE", "FEES", "FEES_REFUND", "TAXES", "TAX_REFUND")
NOT_BOOKED = ("DEPOSIT", "REMOVAL")
PURCHASE = ("BUY", "TRANSFER_IN", "DELIVERY_INBOUND")

DESCRIPTIONS = {
    "BUY": "Kauf {sec}", "SELL": "Verkauf {sec}", "DIVIDENDS": "Dividende {sec}", "INTEREST": "Zinsen",
    "INTEREST_CHARGE": "Sollzinsen", "FEES": "Gebühren", "FEES_REFUND": "Gebührenerstattung",
    "TAXES": "Steuern", "TAX_REFUND": "Steuererstattung", "TRANSFER": "Depotübertrag {sec}",
    "DELIVERY_INBOUND": "Einlieferung {sec}", "DELIVERY_OUTBOUND": "Auslieferung {sec}",
    "OPENING": "Anfangsbestand {sec}",
}
MUTUAL_HINTS = ("etf", "fonds", "fund", "ucits", "etc ", "sicav", "index", "msci", "ishares", "xtrackers",
                "vanguard", "amundi", "lyxor", "invesco", "spdr", "wisdomtree")


def dec(value) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value))


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def mutual_type(name: str) -> str:
    n = f" {(name or '').casefold()} "
    return "MUTUAL" if any(h in n for h in MUTUAL_HINTS) else "STOCK"


def clean_name(name: str) -> str:
    """Account names must not contain GnuCash's separator."""
    return " ".join((name or "?").replace(":", "-").split())[:200] or "?"


# ------------------------------------------------------------------------------------------ data

@dataclass
class Security:
    uuid: str
    name: str
    currency: str | None
    isin: str | None
    wkn: str | None
    ticker: str | None
    feed: str | None
    retired: bool
    prices: list  # [(date, value in security currency, value in base currency or None)]
    latest: tuple | None  # (date, value)
    decimals: int = 0  # decimals needed for its share counts

    @property
    def symbol(self) -> str:
        t = (self.ticker or "").strip()
        return t or (self.wkn or "").strip() or (self.isin or "").strip() or clean_name(self.name)[:12]


@dataclass
class Leg:
    """One split. ref: ('stock', portfolio, security) | ('role', name) | ('cash', pp account)."""
    ref: tuple
    value: Decimal
    quantity: Decimal | None = None
    memo: str = ""

    def canon(self):
        return [list(self.ref), str(self.value), None if self.quantity is None else str(self.quantity), self.memo]


@dataclass
class Booking:
    key: str
    kind: str
    day: date
    description: str
    notes: str
    currency: str
    legs: list = field(default_factory=list)
    security: str | None = None
    portfolio: str | None = None
    account: str | None = None
    amount: Decimal = ZERO  # cash amount (positive) for display
    shares: Decimal | None = None
    fees: Decimal = ZERO
    taxes: Decimal = ZERO
    gain: Decimal | None = None
    skip: str = ""  # reason when not booked
    warnings: list = field(default_factory=list)
    uuids: tuple = ()  # all PP transaction uuids behind this booking

    @property
    def fingerprint(self) -> str:
        canon = [self.day.isoformat(), self.currency, self.description, self.notes,
                 sorted(leg.canon() for leg in self.legs)]
        return hashlib.sha256(json.dumps(canon, ensure_ascii=False).encode()).hexdigest()

    @property
    def balance(self) -> Decimal:
        return sum((leg.value for leg in self.legs), ZERO)


@dataclass
class Plan:
    base: str
    revision: str
    securities: dict  # uuid -> Security
    portfolios: dict  # uuid -> raw dict
    accounts: dict    # uuid -> raw dict
    bookings: list    # Booking (also the skipped ones), by date
    holdings: dict    # (portfolio, security) -> shares at the end
    warnings: list = field(default_factory=list)

    @property
    def booked(self) -> list:
        return [b for b in self.bookings if not b.skip]

    def stock_keys(self) -> set:
        """(portfolio, security) pairs that need an account in GnuCash."""
        out = set()
        for b in self.booked:
            for leg in b.legs:
                if leg.ref[0] == "stock":
                    out.add((leg.ref[1], leg.ref[2]))
        return out

    def by_key(self) -> dict:
        return {b.key: b for b in self.bookings}


# ------------------------------------------------------------------------------------------ FIFO

class Lots:
    """FIFO lots per (portfolio, security): [shares, cost, purchase day], oldest first."""

    def __init__(self):
        self.lots: dict = {}

    def add(self, key, shares: Decimal, cost: Decimal, day: date):
        if shares > 0:
            self.lots.setdefault(key, []).append([shares, cost, day])

    def _remove(self, key, shares: Decimal) -> tuple[list, Decimal, Decimal]:
        """Takes `shares` oldest first. Returns (removed lots, their cost, shares that were missing)."""
        queue = self.lots.get(key, [])
        removed, cost, rest = [], ZERO, shares
        with localcontext() as ctx:
            ctx.prec = 40
            while rest > 0 and queue:
                lot = queue[0]
                if lot[0] <= rest:
                    removed.append(list(lot))
                    cost += lot[1]
                    rest -= lot[0]
                    queue.pop(0)
                else:
                    part = lot[1] * rest / lot[0]
                    removed.append([rest, part, lot[2]])
                    lot[0] -= rest
                    lot[1] -= part
                    cost += part
                    rest = ZERO
        return removed, cost, max(rest, ZERO)

    def take(self, key, shares: Decimal) -> tuple[Decimal, Decimal]:
        """Sale or outbound delivery: (cost of the removed shares, shares that were missing)."""
        _removed, cost, missing = self._remove(key, shares)
        return cost, missing

    def move(self, src, dst, shares: Decimal) -> tuple[Decimal, Decimal]:
        """Transfer between securities accounts: the lots keep their cost and purchase day."""
        removed, cost, missing = self._remove(src, shares)
        target = self.lots.setdefault(dst, [])
        target.extend(removed)
        target.sort(key=lambda lot: lot[2])  # stable: same day keeps the order
        return cost, missing

    def shares(self, key) -> Decimal:
        return sum((lot[0] for lot in self.lots.get(key, [])), ZERO)

    def cost(self, key) -> Decimal:
        return sum((lot[1] for lot in self.lots.get(key, [])), ZERO)

    def keys(self):
        return list(self.lots)


# ------------------------------------------------------------------------------------------ build

def _units(tx: dict, base: str) -> tuple[Decimal, Decimal, list]:
    """(fees, taxes, warnings) in the transaction currency."""
    fees = taxes = ZERO
    warnings = []
    for u in tx.get("units") or []:
        amount = dec(u.get("amount"))
        if u.get("currency") and u.get("currency") != tx.get("currency"):
            warnings.append(_("Einheit in {c} ignoriert", c=u.get('currency')))
            continue
        if u.get("type") == "FEE":
            fees += amount
        elif u.get("type") == "TAX":
            taxes += amount
    return fees, taxes, warnings


def _day(tx: dict) -> date:
    return datetime.fromisoformat(tx["date"]).date()


def _notes(tx: dict) -> str:
    parts = []
    if (tx.get("note") or "").strip():
        parts.append(tx["note"].strip())
    if (tx.get("source") or "").strip():
        parts.append(f"Beleg: {tx['source'].strip()}")
    parts.append("Portfolio Performance")
    return "\n".join(parts)[:4000]


def _decimals(value: str | None) -> int:
    if not value or "." not in value:
        return 0
    return len(value.split(".", 1)[1].rstrip("0"))


def parse_securities(export: dict) -> dict:
    out = {}
    for s in export.get("securities", []):
        prices = []
        for row in s.get("prices") or []:
            day = date.fromisoformat(row[0])
            prices.append((day, dec(row[1]), dec(row[2]) if len(row) > 2 and row[2] not in (None, "") else None))
        latest = s.get("latest")
        out[s["uuid"]] = Security(
            s["uuid"], s.get("name") or "?", s.get("currency"), s.get("isin"), s.get("wkn"), s.get("ticker"),
            s.get("feed"), bool(s.get("retired")), prices,
            (date.fromisoformat(latest["date"]), dec(latest["value"])) if latest else None)
    for t in export.get("transactions", []):
        sec = out.get(t.get("security") or "")
        if sec is not None:
            sec.decimals = max(sec.decimals, _decimals(t.get("shares")))
    return out


def build_plan(export: dict, base: str, *, sync_from: date | None = None, realized_gains: bool = True,
               capitalize_fees: bool = False, cash_accounts: dict | None = None) -> Plan:
    """All bookings of the export. `base` is the currency of the GnuCash book (ISO code)."""
    cash_accounts = cash_accounts or {}
    securities = parse_securities(export)
    portfolios = {p["uuid"]: p for p in export.get("portfolios", [])}
    accounts = {a["uuid"]: a for a in export.get("accounts", [])}
    txs = {t["uuid"]: t for t in export.get("transactions", [])}
    plan = Plan(base, (export.get("client") or {}).get("revision") or "", securities, portfolios, accounts, [], {})

    def sec_name(uuid):
        s = securities.get(uuid or "")
        return s.name if s else ""

    def describe(kind, sec_uuid=None):
        text = DESCRIPTIONS[kind].format(sec=sec_name(sec_uuid)).strip()
        if kind in ("FEES", "FEES_REFUND", "TAXES", "TAX_REFUND", "INTEREST") and sec_uuid:
            text = f"{text} {sec_name(sec_uuid)}"
        return text[:2000]

    def cash_ref(pp_account):
        return ("cash", pp_account) if pp_account in cash_accounts else ("role", "clearing")

    # chronological order; on the same day purchases before sales (FIFO needs the lots first)
    def order(t):
        typ = t.get("type")
        rank = 0 if typ in PURCHASE else 1
        return (t["date"], rank, t["uuid"])

    lots = Lots()
    held: dict = {}  # (portfolio, security) -> shares (for dividend links)
    opened = sync_from is None
    done = set()

    def open_positions():
        """Opening bookings for the holdings on the day before sync_from."""
        day = sync_from - timedelta(days=1)
        for key in sorted(lots.keys()):
            shares = lots.shares(key)
            if shares <= 0:
                continue
            cost = cents(lots.cost(key))
            port, sec = key
            b = Booking(f"opening:{port}:{sec}", "OPENING", day, describe("OPENING", sec),
                        f"Bestand am {day:%d.%m.%Y} (FIFO-Einstand)\nPortfolio Performance", base,
                        security=sec, portfolio=port, amount=cost, shares=shares)
            b.legs = [Leg(("stock", port, sec), cost, shares), Leg(("role", "delivery"), -cost)]
            plan.bookings.append(b)

    for t in sorted(txs.values(), key=order):
        if t["uuid"] in done:
            continue
        day = _day(t)
        if not opened and day >= sync_from:
            open_positions()
            opened = True
        before = sync_from is not None and day < sync_from
        typ = t.get("type")
        kind = t.get("kind")
        cross = t.get("cross") or {}
        other = txs.get(cross.get("uuid") or "")

        if kind == "account" and typ in ("BUY", "SELL") and cross.get("type") == "buysell":
            continue  # booked from the securities side
        if kind == "account" and (typ in NOT_BOOKED or cross.get("type") == "account-transfer"):
            done.add(t["uuid"])
            b = Booking(t["uuid"], typ, day, {"DEPOSIT": "Einlage", "REMOVAL": "Entnahme"}.get(typ, "Umbuchung"),
                        _notes(t), t.get("currency") or base, account=t.get("owner"), amount=dec(t.get("amount")),
                        skip="bankimport", uuids=(t["uuid"],) + ((other["uuid"],) if other else ()))
            if other:
                done.add(other["uuid"])
            plan.bookings.append(b)
            continue

        fees, taxes, warnings = _units(t, base)
        amount = dec(t.get("amount"))
        currency = t.get("currency") or base
        sec = t.get("security")

        if kind == "portfolio":
            port = t.get("owner")
            key = (port, sec)
            shares = dec(t.get("shares"))
            if typ in ("BUY", "SELL") and cross.get("type") == "buysell" and other is not None:
                done.update({t["uuid"], other["uuid"]})
                cash = cash_ref(other.get("owner"))
                b = Booking(t["uuid"], typ, day, describe(typ, sec), _notes(t), currency, security=sec,
                            portfolio=port, account=other.get("owner"), amount=amount, shares=shares, fees=fees,
                            taxes=taxes, warnings=warnings, uuids=(t["uuid"], other["uuid"]))
                if typ == "BUY":
                    gross = amount - fees - taxes
                    basis = amount if capitalize_fees else gross
                    lots.add(key, shares, basis, day)
                    held[key] = held.get(key, ZERO) + shares
                    b.legs = [Leg(("stock", port, sec), basis, shares), Leg(cash, -amount)]
                    if not capitalize_fees:
                        if fees:
                            b.legs.append(Leg(("role", "fees"), fees))
                        if taxes:
                            b.legs.append(Leg(("role", "taxes"), taxes))
                else:
                    gross = amount + fees + taxes
                    proceeds = gross - fees if capitalize_fees else gross
                    cost, missing = lots.take(key, shares)
                    held[key] = held.get(key, ZERO) - shares
                    if missing:
                        b.warnings.append(_("{n} Stück mehr verkauft als im Bestand (FIFO)", n=missing))
                    b.legs = [Leg(("stock", port, sec), -proceeds, -shares), Leg(cash, amount)]
                    if fees and not capitalize_fees:
                        b.legs.append(Leg(("role", "fees"), fees))
                    elif fees and capitalize_fees:
                        pass  # sale fees reduce the proceeds (already in `proceeds`)
                    if taxes:
                        b.legs.append(Leg(("role", "taxes"), taxes))
                    gain = cents(proceeds - cost)
                    b.gain = gain
                    if realized_gains and gain:
                        b.legs += [Leg(("stock", port, sec), gain, ZERO), Leg(("role", "gains"), -gain)]
            elif typ == "TRANSFER_OUT" and cross.get("type") == "portfolio-transfer" and other is not None:
                done.update({t["uuid"], other["uuid"]})
                dst = (other.get("owner"), sec)
                cost, missing = lots.move(key, dst, shares)
                held[key] = held.get(key, ZERO) - shares
                held[dst] = held.get(dst, ZERO) + shares
                cost = cents(cost)
                b = Booking(t["uuid"], "TRANSFER", day, describe("TRANSFER", sec), _notes(t), currency,
                            security=sec, portfolio=port, amount=cost, shares=shares,
                            uuids=(t["uuid"], other["uuid"]))
                if missing:
                    b.warnings.append(_("{n} Stück mehr übertragen als im Bestand (FIFO)", n=missing))
                b.legs = [Leg(("stock", port, sec), -cost, -shares), Leg(("stock", other.get("owner"), sec), cost,
                                                                          shares)]
            elif typ == "TRANSFER_IN":
                continue  # booked with its TRANSFER_OUT (or orphaned – then nothing to do)
            elif typ in ("DELIVERY_INBOUND", "BUY"):
                done.add(t["uuid"])
                gross = amount - fees - taxes
                basis = amount if capitalize_fees else gross
                lots.add(key, shares, basis, day)
                held[key] = held.get(key, ZERO) + shares
                b = Booking(t["uuid"], "DELIVERY_INBOUND", day, describe("DELIVERY_INBOUND", sec), _notes(t),
                            currency, security=sec, portfolio=port, amount=amount, shares=shares, fees=fees,
                            taxes=taxes, warnings=warnings, uuids=(t["uuid"],))
                b.legs = [Leg(("stock", port, sec), basis, shares), Leg(("role", "delivery"), -amount)]
                if not capitalize_fees:
                    if fees:
                        b.legs.append(Leg(("role", "fees"), fees))
                    if taxes:
                        b.legs.append(Leg(("role", "taxes"), taxes))
            elif typ in ("DELIVERY_OUTBOUND", "SELL"):
                done.add(t["uuid"])
                cost, missing = lots.take(key, shares)
                held[key] = held.get(key, ZERO) - shares
                cost = cents(cost)
                b = Booking(t["uuid"], "DELIVERY_OUTBOUND", day, describe("DELIVERY_OUTBOUND", sec), _notes(t),
                            currency, security=sec, portfolio=port, amount=cost, shares=shares, fees=fees,
                            taxes=taxes, warnings=warnings, uuids=(t["uuid"],))
                if missing:
                    b.warnings.append(_("{n} Stück mehr ausgeliefert als im Bestand (FIFO)", n=missing))
                b.legs = [Leg(("stock", port, sec), -cost, -shares), Leg(("role", "delivery"), cost - fees - taxes)]
                if fees:
                    b.legs.append(Leg(("role", "fees"), fees))
                if taxes:
                    b.legs.append(Leg(("role", "taxes"), taxes))
            else:
                done.add(t["uuid"])
                b = Booking(t["uuid"], typ or "?", day, typ or "?", _notes(t), currency, security=sec,
                            portfolio=port, amount=amount, shares=shares, skip="unsupported", uuids=(t["uuid"],))
        else:  # account transactions
            done.add(t["uuid"])
            pp_account = t.get("owner")
            cash = cash_ref(pp_account)
            b = Booking(t["uuid"], typ or "?", day, describe(typ, sec) if typ in DESCRIPTIONS else (typ or "?"),
                        _notes(t), currency, security=sec, account=pp_account, amount=amount,
                        shares=dec(t.get("shares")) or None, fees=fees, taxes=taxes, warnings=warnings,
                        uuids=(t["uuid"],))
            if typ in ("DIVIDENDS", "INTEREST"):
                gross = amount + fees + taxes
                role = "dividends" if typ == "DIVIDENDS" else "interest"
                b.legs = [Leg(cash, amount), Leg(("role", role), -gross)]
                if fees:
                    b.legs.append(Leg(("role", "fees"), fees))
                if taxes:
                    b.legs.append(Leg(("role", "taxes"), taxes))
                if typ == "DIVIDENDS" and sec:
                    port = _dividend_portfolio(sec, pp_account, portfolios, held)
                    if port is not None:
                        b.portfolio = port
                        b.legs.append(Leg(("stock", port, sec), ZERO, ZERO))
            elif typ in ("INTEREST_CHARGE", "FEES", "TAXES"):
                role = {"INTEREST_CHARGE": "interest_charge", "FEES": "fees", "TAXES": "taxes"}[typ]
                b.legs = [Leg(cash, -amount), Leg(("role", role), amount)]
            elif typ in ("FEES_REFUND", "TAX_REFUND"):
                role = "fees" if typ == "FEES_REFUND" else "taxes"
                b.legs = [Leg(cash, amount), Leg(("role", role), -amount)]
            else:
                b.skip = "unsupported"
        if before and not b.skip:
            b.skip = "before_start"
        if not b.skip and currency != base:
            b.skip = "currency"
        if not b.skip and b.balance != 0:
            b.skip = "unbalanced"
            b.warnings.append(_("Differenz {n}", n=b.balance))
        plan.bookings.append(b)

    if not opened:
        open_positions()
    for key in lots.keys():
        plan.holdings[key] = lots.shares(key)
    plan.bookings.sort(key=lambda b: (b.day, b.key))
    return plan


def _dividend_portfolio(sec: str, pp_account: str, portfolios: dict, held: dict) -> str | None:
    """Securities account the dividend belongs to: the one with this cash account as reference account that
    holds the security, else any that holds it."""
    holding = [p for (p, s), n in held.items() if s == sec and n > 0]
    for p in holding:
        if (portfolios.get(p) or {}).get("referenceAccount") == pp_account:
            return p
    return holding[0] if holding else None


SKIP_REASONS = {
    "bankimport": "kommt über den Bankimport (Ein-/Auszahlung, Umbuchung)",
    "before_start": "vor dem Startdatum – im Anfangsbestand enthalten",
    "currency": "Konto in Fremdwährung – bitte in GnuCash Desktop buchen",
    "unsupported": "Buchungsart wird nicht übernommen",
    "unbalanced": "geht nicht auf",
}
