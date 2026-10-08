"""Bank profiles: everything that depends on the country or the bank.

A profile decides
  * how balance lines in booking texts are recognised (balance checkpoints),
  * how an imported bank line becomes a GnuCash booking text and bank-split memo,
  * which own account an IBAN belongs to beyond the generic rules (e.g. German bank code + account number).

The core (gnubook.checkpoints, gnubook.importer) only calls these hooks. Profiles: "de" (default) and
"generic"; additional balance patterns can be configured in [checkpoints] patterns for every profile.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class ParsedCheckpoint:
    stand_date: date
    stand: Decimal
    endsaldo: Decimal | None = None


class BalancePattern:
    """A configurable balance line: regex with named groups day, month, year, amount and optional sign/marker."""

    def __init__(self, stand: str, end: str = "", keyword: str = "", decimal: str = ",", thousands: str = ".",
                 credit: str = "H", debit: str = "S", date_order: str = "dmy"):
        self.rx_stand = re.compile(stand, re.IGNORECASE)
        self.rx_end = re.compile(end, re.IGNORECASE) if end else None
        for group in ("day", "month", "year", "amount"):
            if group not in self.rx_stand.groupindex:
                raise ValueError(f"[checkpoints] pattern: group (?P<{group}>…) missing in {stand!r}")
        self.keyword = (keyword or "").lower()
        self.decimal, self.thousands = decimal, thousands
        self.credit, self.debit = credit.upper(), debit.upper()

    def _amount(self, m) -> Decimal | None:
        raw = m.group("amount").replace(self.thousands, "").replace(self.decimal, ".") if self.thousands else \
            m.group("amount").replace(self.decimal, ".")
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return None
        gd = m.groupdict()
        sign = (gd.get("sign") or "").strip().upper()
        if sign in ("-", self.debit):
            value = -value
        return value

    def parse(self, text: str) -> ParsedCheckpoint | None:
        m = self.rx_stand.search(text)
        if not m:
            return None
        try:
            year = int(m.group("year"))
            d = date(year + 2000 if year < 100 else year, int(m.group("month")), int(m.group("day")))
        except ValueError:
            return None
        stand = self._amount(m)
        if stand is None:
            return None
        end = None
        if self.rx_end is not None:
            me = self.rx_end.search(text)
            if me:
                end = self._amount(me)
        return ParsedCheckpoint(d, stand, end)


class BankProfile:
    name = "generic"
    label = "International / generic"

    def __init__(self, extra_patterns=()):
        self.extra_patterns = list(extra_patterns)

    # -------------------------------------------------- balance checkpoints
    def checkpoint_keywords(self) -> list[str]:
        """Lower-case words one of which a balance line must contain (SQL prefilter)."""
        return [p.keyword for p in self.extra_patterns if p.keyword]

    def own_checkpoint(self, text: str) -> ParsedCheckpoint | None:
        return None

    def parse_checkpoint(self, text: str | None) -> ParsedCheckpoint | None:
        if not text:
            return None
        found = self.own_checkpoint(text)
        if found is not None:
            return found
        for p in self.extra_patterns:
            found = p.parse(text)
            if found is not None:
                return found
        return None

    # -------------------------------------------------- import
    def booking_text(self, purpose: str, name: str) -> str:
        if name and name.casefold() not in purpose.casefold():
            return f"{name} – {purpose}" if purpose else name
        return purpose

    def bank_memo(self, iban: str) -> str:
        return f"IBAN {iban}" if iban else ""

    def own_accounts_for_iban(self, conn, iban: str) -> set[str]:
        """Account GUIDs that this profile can identify for an IBAN (besides iban_map and account codes)."""
        return set()


def profile_names() -> dict[str, str]:
    from .de import GermanProfile

    return {GermanProfile.name: GermanProfile.label, BankProfile.name: BankProfile.label}


def get_profile(name: str, patterns=()) -> BankProfile:
    from .de import GermanProfile

    extra = [p if isinstance(p, BalancePattern) else BalancePattern(**p) for p in patterns]
    if name in ("", "de", None):
        return GermanProfile(extra)
    if name == "generic":
        return BankProfile(extra)
    raise ValueError(f"Unbekanntes Bankprofil: {name}")
