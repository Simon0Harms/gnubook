"""German banks: STAND/ENDSALDO/"Kontostand am" balance lines, AqBanking-style booking texts, BLZ + Kontonummer."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from sqlalchemy import text

from . import BankProfile, ParsedCheckpoint

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


class GermanProfile(BankProfile):
    name = "de"
    label = "Deutschland (STAND/ENDSALDO, BLZ)"

    def checkpoint_keywords(self) -> list[str]:
        return ["stand"] + super().checkpoint_keywords()   # covers STAND and Kontostand

    def own_checkpoint(self, description: str) -> ParsedCheckpoint | None:
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

    def booking_text(self, purpose: str, name: str) -> str:
        """Same format as GnuCash's AqBanking import: "<Verwendungszweck>; <Name>"."""
        if name and name.casefold() not in purpose.casefold():
            return f"{purpose}; {name}" if purpose else name
        return purpose

    def bank_memo(self, iban: str) -> str:
        return f"Konto {iban}" if iban else ""

    def own_accounts_for_iban(self, conn, iban: str) -> set[str]:
        """German IBAN = DE + check digits + Bankleitzahl + Kontonummer; GnuCash's online banking stores both."""
        if not (iban.startswith("DE") and len(iban) == 22):
            return set()
        blz, kto = iban[4:12], iban[12:].lstrip("0")
        hbci: dict[str, dict] = {}
        for acc_guid, name, value in conn.execute(text(
                "SELECT f.obj_guid, c.name, c.string_val FROM slots f JOIN slots c ON c.obj_guid = f.guid_val "
                "WHERE f.name = 'hbci' AND f.slot_type = 9 AND c.name IN ('hbci/account-id', 'hbci/bank-code')")):
            hbci.setdefault(acc_guid, {})[name] = (value or "").strip()
        return {g for g, v in hbci.items()
                if v.get("hbci/bank-code") == blz and v.get("hbci/account-id", "").lstrip("0") == kto}
