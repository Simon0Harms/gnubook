"""Per-book settings of the Portfolio Performance link (stored as JSON in system.sqlite, table books)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date

from ..book import AccountIndex

# default account names below the top-level account of the right type (German / English charts)
DEFAULT_NAMES = {
    "de": {"securities_root": "Wertpapiere", "clearing": "Wertpapier-Verrechnung", "dividends": "Dividenden",
           "interest": "Zinsen", "gains": "Kursgewinne", "fees": "Wertpapiergebühren",
           "taxes": "Kapitalertragsteuer", "interest_charge": "Sollzinsen", "delivery": "Wertpapier-Einlieferungen",
           "top": {"ASSET": "Aktiva", "INCOME": "Erträge", "EXPENSE": "Aufwendungen", "EQUITY": "Eigenkapital"}},
    "en": {"securities_root": "Investments", "clearing": "Investment clearing", "dividends": "Dividends",
           "interest": "Interest", "gains": "Capital gains", "fees": "Investment fees",
           "taxes": "Capital gains tax", "interest_charge": "Interest charges", "delivery": "Securities deliveries",
           "top": {"ASSET": "Assets", "INCOME": "Income", "EXPENSE": "Expenses", "EQUITY": "Equity"}},
}
# GnuCash type of each role account
ROLE_TYPES = {"clearing": "ASSET", "dividends": "INCOME", "interest": "INCOME", "gains": "INCOME",
              "fees": "EXPENSE", "taxes": "EXPENSE", "interest_charge": "EXPENSE", "delivery": "EQUITY"}
ROLE_LABELS = {
    "clearing": "Wertpapier-Verrechnung (Zwischenkonto)", "dividends": "Dividenden", "interest": "Zinserträge",
    "gains": "Realisierte Kursgewinne/-verluste", "fees": "Gebühren", "taxes": "Steuern",
    "interest_charge": "Sollzinsen", "delivery": "Einlieferungen und Anfangsbestände",
}


@dataclass
class PPSettings:
    enabled: bool = False
    # parent of the securities accounts (one placeholder per PP securities account below it)
    securities_root: str = ""
    # fixed accounts by role (full names or GUIDs); empty = default name, created when needed
    accounts: dict = field(default_factory=dict)
    # PP cash account uuid -> GnuCash account for its cash side (default: the clearing account)
    cash_accounts: dict = field(default_factory=dict)
    # GnuCash bank accounts (full names or GUIDs) whose bank-import lines go to the clearing account
    bank_accounts: list = field(default_factory=list)
    # earlier PP transactions are not booked; the holdings of the day before become opening bookings
    sync_from: str = ""
    realized_gains: bool = True
    capitalize_fees: bool = False
    prices: bool = True
    # prices of the last N days daily, older ones one per month
    price_days: int = 400
    # GnuCash namespace ("Typ") for securities gnubook creates
    namespace: str = "Portfolio Performance"

    @classmethod
    def from_json(cls, text: str | None) -> "PPSettings":
        try:
            data = json.loads(text or "{}")
        except ValueError:
            data = {}
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        s = cls(**known)
        s.accounts = {k: v for k, v in (s.accounts or {}).items() if k in ROLE_TYPES and v}
        s.cash_accounts = {k: v for k, v in (s.cash_accounts or {}).items() if v}
        s.bank_accounts = [v for v in (s.bank_accounts or []) if v]
        return s

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @property
    def start(self) -> date | None:
        try:
            return date.fromisoformat(self.sync_from) if self.sync_from else None
        except ValueError:
            return None

    def booking_fingerprint(self) -> str:
        """Settings that change the bookings (a change re-checks every booking)."""
        import hashlib

        relevant = {k: getattr(self, k) for k in ("securities_root", "accounts", "cash_accounts", "sync_from",
                                                  "realized_gains", "capitalize_fees")}
        return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()


def chart_language(index: AccountIndex) -> str:
    names = {a.name.casefold() for a in index.top_level()}
    return "en" if names & {"assets", "income", "expenses", "equity"} and not names & {"aktiva", "erträge"} else "de"


def top_account(index: AccountIndex, typ: str):
    """Top-level account of a type (placeholder preferred), or None."""
    tops = [a for a in index.top_level() if a.type == typ]
    tops.sort(key=lambda a: (not a.placeholder, a.name.casefold()))
    return tops[0] if tops else None


def default_name(index: AccountIndex, role: str) -> str:
    lang = chart_language(index)
    names = DEFAULT_NAMES[lang]
    typ = "ASSET" if role in ("clearing", "securities_root") else ROLE_TYPES[role]
    top = top_account(index, typ)
    top_name = top.full_name if top is not None else names["top"][typ]
    leaf = names["securities_root"] if role == "securities_root" else names[role]
    if top is not None and not top.placeholder and typ == "EQUITY":
        # e.g. a top-level "Anfangsbestand" equity account that takes bookings directly
        return top.full_name
    return f"{top_name}{index.separator}{leaf}"


def resolved(settings: PPSettings, index: AccountIndex) -> dict:
    """role -> account full name (configured, else the default name)."""
    out = {}
    for role in ROLE_TYPES:
        ref = settings.accounts.get(role, "")
        acc = index.find(ref) if ref else None
        out[role] = acc.full_name if acc is not None else (ref or default_name(index, role))
    root = index.find(settings.securities_root) if settings.securities_root else None
    out["securities_root"] = root.full_name if root is not None else (
        settings.securities_root or default_name(index, "securities_root"))
    return out
