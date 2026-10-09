"""Import of bank transactions sent by bnw/firefly-iii-fints-importer (Firefly III API format).

For every bank line gnubook
  1. rejects exact re-imports (hash of the request, like Firefly's duplicate hash),
  2. links it to an existing, not yet imported booking of the same account and amount (e.g. data
     imported earlier by GnuCash itself, or the other side of a transfer between two own accounts),
  3. otherwise creates a GnuCash transaction. The counter account is taken from
     own accounts (IBAN), the book history for this counterparty, GnuCash's Bayesian import map,
     or the configured fallback account – in this order.
"""
from __future__ import annotations

from .i18n import gettext as _

import hashlib
import json
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy import text

from . import checkpoints
from .appdb import AppDB
from .book import Account, AccountIndex, Book, BookError, WriteLockError
from .config import ImportConfig
from .money import fmt
from .writer import SplitInput, TxInput, ValidationError, create_transaction

IMPORTABLE_TYPES = ("BANK", "ASSET", "CASH", "CREDIT", "LIABILITY")
WEEKDAYS_DE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
BAYES_THRESHOLD = 0.90
MAX_AMOUNT = Decimal("1000000000000")  # far below GnuCash's 64-bit numerator limit
STOPWORDS = {
    "SEPA", "LASTSCHRIFT", "GUTSCHRIFT", "UEBERWEISUNG", "ÜBERWEISUNG", "ECHTZEITÜBERWEISUNG", "ECHTZEITUEBERWEISUNG",
    "KARTENZAHLUNG", "DAUERAUFTRAG", "BASISLASTSCHRIFT", "FOLGELASTSCHRIFT", "ERSTLASTSCHRIFT", "ONLINE", "UEBERW",
    "ÜBERW", "VISA", "DEBITKARTENUMSATZ", "VOM", "EUR", "DATUM", "UHR", "IBAN", "BIC", "KONTO", "BANK", "END", "REF",
    "EREF", "MREF", "CRED", "SVWZ", "ABWA", "GMBH", "AG", "UND", "DER", "DIE", "DAS", "FÜR", "FUER", "NOTPROVIDED",
}


class ImportRejected(BookError):
    def __init__(self, field_name: str, message: str):
        self.field = field_name
        super().__init__(message)


@dataclass
class Entry:
    type: str
    day: date
    amount: Decimal                 # positive
    description: str
    own: Account
    counter_own: Account | None     # set for type "transfer"
    cp_name: str = ""
    cp_iban: str = ""
    notes: str = ""
    sepa_ct_id: str = ""
    currency_code: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def signed(self) -> Decimal:
        """Amount on the own account (deposit positive)."""
        return self.amount if self.type == "deposit" else -self.amount


@dataclass
class ImportResult:
    status: str                     # created | matched | possible_duplicate | duplicate
    tx_guid: str | None
    message: str = ""
    record_id: int | None = None
    entry: Entry | None = None
    counter: Account | None = None
    source: str = ""


def is_special_account(acc: Account) -> bool:
    """GnuCash's automatic top-level accounts (imbalance/orphan, also in German)."""
    return acc.depth == 0 and acc.name.split("-")[0] in ("Ausgleichskonto", "Imbalance", "Orphan", "Waisen")


def normalize_iban(value: str | None) -> str:
    return re.sub(r"\s+", "", value or "").upper()


def looks_like_iban(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", value or ""))


def text_tokens(value: str) -> set[str]:
    return {t for t in re.findall(r"[0-9A-ZÄÖÜß]+", (value or "").upper())
            if (len(t) >= 3 or t.isdigit()) and t not in STOPWORDS}


def similarity(a: str, b: str) -> float:
    ta, tb = text_tokens(a), text_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _clean(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _parse_amount(value) -> Decimal:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ImportRejected("amount", _("Ungültiger Betrag."))
    if not amount.is_finite() or amount < 0:
        raise ImportRejected("amount", _("Der Betrag muss eine positive Zahl sein."))
    if amount >= MAX_AMOUNT:
        raise ImportRejected("amount", _("Der Betrag ist unplausibel groß."))
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _parse_date(value) -> date:
    s = str(value or "").strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        raise ImportRejected("date", _("Ungültiges Datum (erwartet JJJJ-MM-TT)."))


class Importer:
    def __init__(self, book: Book, appdb: AppDB, cfg: ImportConfig, pp=None):
        self.book = book
        self.appdb = appdb
        self.cfg = cfg
        self.pp = pp  # gnubook.pp.settings.PPSettings of the book when the Portfolio Performance link is on
        self._bayes_cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ accounts
    def importable_accounts(self, index: AccountIndex) -> list[Account]:
        if self.cfg.accounts:
            accs = [index.find(ref) for ref in self.cfg.accounts]
            return [a for a in accs if a is not None and not a.placeholder]
        fallback = self.fallback_account(index, index.root.commodity_guid) if index.root.commodity_guid else None
        return [a for a in index.walk() if a.type in IMPORTABLE_TYPES and not a.placeholder and not a.hidden
                and a.commodity is not None and a.commodity.is_currency and not is_special_account(a)
                and (fallback is None or a.guid != fallback.guid)]

    def fallback_account(self, index: AccountIndex, currency_guid: str) -> Account | None:
        if self.cfg.fallback_account:
            acc = index.find(self.cfg.fallback_account)
            return acc if acc is not None and acc.commodity_guid == currency_guid else None
        mnemonic = index.commodities[currency_guid].mnemonic if currency_guid in index.commodities else "EUR"
        for name in (f"Ausgleichskonto-{mnemonic}", f"Imbalance-{mnemonic}"):
            acc = index.find(name)
            if acc is not None and not acc.placeholder and acc.commodity_guid == currency_guid:
                return acc
        return None

    def own_account_for_iban(self, conn, index: AccountIndex, iban: str) -> Account | None:
        """Own account for a counterparty IBAN: [import] iban_map > GnuCash online-banking data > account code."""
        iban = normalize_iban(iban)
        if not looks_like_iban(iban):
            return None
        for key, ref in (self.cfg.iban_map or {}).items():
            if normalize_iban(key) == iban:
                return index.find(ref)
        found = {g for g in self.profile.own_accounts_for_iban(conn, iban) if g in index}
        if len(found) == 1:
            return index.get(found.pop())
        if found:
            return None  # ambiguous
        by_code = [a for a in index.walk() if normalize_iban(a.code) == iban and not a.placeholder]
        return by_code[0] if len(by_code) == 1 else None

    # ------------------------------------------------------------------ parsing
    def parse(self, body: dict, index: AccountIndex) -> Entry:
        txs = body.get("transactions") if isinstance(body, dict) else None
        if not isinstance(txs, list) or not txs:
            raise ImportRejected("transactions", _("Keine Buchung im Request."))
        if len(txs) > 1:
            raise ImportRejected("transactions", _("gnubook nimmt pro Request genau eine Buchung an."))
        t = txs[0]
        if not isinstance(t, dict):
            raise ImportRejected("transactions.0", _("Ungültige Buchung."))
        typ = _clean(t.get("type")).lower()
        if typ not in ("withdrawal", "deposit", "transfer"):
            raise ImportRejected("type", _("Typ muss withdrawal, deposit oder transfer sein."))
        day = _parse_date(t.get("date"))
        amount = _parse_amount(t.get("amount"))
        importable = {a.guid: a for a in self.importable_accounts(index)}

        def own_by_id(key):
            guid = self.appdb.account_guid(t.get(key)) if t.get(key) not in (None, "") else None
            acc = importable.get(guid) if guid else None
            if acc is None:
                raise ImportRejected(key, _("Konto unbekannt oder nicht für den Import freigegeben (API-ID siehe gnubook → Konto)."))
            return acc

        counter_own = None
        if typ == "withdrawal":
            own = own_by_id("source_id")
            cp_name, cp_iban = _clean(t.get("destination_name")), normalize_iban(t.get("destination_iban"))
        elif typ == "deposit":
            own = own_by_id("destination_id")
            cp_name, cp_iban = _clean(t.get("source_name")), normalize_iban(t.get("source_iban"))
        else:
            own = own_by_id("source_id")
            counter_own = own_by_id("destination_id")
            cp_name, cp_iban = counter_own.name, ""
            typ = "withdrawal"  # booked from the source account's point of view
        currency_code = _clean(t.get("currency_code")).upper()
        if currency_code and currency_code != own.mnemonic:
            raise ImportRejected("currency_code", _("Währung {a0} passt nicht zum Konto ({a1}).", a0=currency_code, a1=own.mnemonic))
        return Entry(typ, day, amount, _clean(t.get("description")), own, counter_own, cp_name, cp_iban,
                     notes=_clean(t.get("notes")), sepa_ct_id=_clean(t.get("sepa_ct_id")),
                     currency_code=currency_code, raw=t)

    @staticmethod
    def entry_hash(entry: Entry) -> str:
        t = entry.raw
        keys = ("type", "date", "description", "currency_code", "source_id", "source_name", "source_iban",
                "destination_id", "destination_name", "destination_iban", "sepa_ct_id", "notes")
        canon = {k: (None if t.get(k) in (None, "") else str(t.get(k))) for k in keys}
        canon["amount"] = str(entry.amount)
        return hashlib.sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @property
    def profile(self):
        return checkpoints._profile(self.book)

    def build_description(self, entry: Entry) -> str:
        return self.profile.booking_text(entry.description, entry.cp_name) or "(ohne Beschreibung)"

    # ------------------------------------------------------------------ counter account
    def history_counter(self, conn, index: AccountIndex, own: Account, iban: str, name: str) -> Account | None:
        """Counter account the book used before for this counterparty (IBAN in the bank split memo + name)."""
        rows = []
        if iban:
            rows = conn.execute(text(
                "SELECT s.tx_guid, t.post_date, t.description FROM splits s JOIN transactions t ON t.guid = s.tx_guid "
                "WHERE s.account_guid = :a AND s.memo LIKE :p"), {"a": own.guid, "p": f"%{iban}%"}).fetchall()
        elif name and len(name) >= 4:
            pat = "%" + name.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            rows = conn.execute(text(
                "SELECT s.tx_guid, t.post_date, t.description FROM splits s JOIN transactions t ON t.guid = s.tx_guid "
                "WHERE s.account_guid = :a AND lower(t.description) LIKE :p ESCAPE '\\'"),
                {"a": own.guid, "p": pat}).fetchall()
        if not rows:
            return None
        rows.sort(key=lambda r: str(r[1]), reverse=True)
        rows = rows[:60]
        tx_guids = [r[0] for r in rows]
        others = defaultdict(list)
        for i in range(0, len(tx_guids), 400):
            chunk = tx_guids[i:i + 400]
            names = {f"t{j}": g for j, g in enumerate(chunk)}
            clause = "(" + ", ".join(":" + n for n in names) + ")"
            for tg, ag in conn.execute(text(f"SELECT tx_guid, account_guid FROM splits WHERE tx_guid IN {clause}"),
                                       names):
                if ag != own.guid:
                    others[tg].append(ag)
        fallback = self.fallback_account(index, own.commodity_guid)

        def usable(ag):
            acc = index.get(ag)
            return (acc is not None and not acc.placeholder and acc.commodity_guid == own.commodity_guid
                    and (fallback is None or acc.guid != fallback.guid) and acc.type != "ROOT")

        def vote(selected):
            counts = Counter()
            for r in selected:
                accs = others.get(r[0], [])
                if len(accs) == 1 and usable(accs[0]):
                    counts[accs[0]] += 1
            return counts

        if iban and name:
            named = [r for r in rows if name.casefold() in (r[2] or "").casefold()][:10]
            counts = vote(named)
            if counts:
                return index.get(counts.most_common(1)[0][0])
        counts = vote(rows[:10])
        if len(counts) == 1:  # the counterparty always went to the same account
            return index.get(next(iter(counts)))
        return None

    def bayes_map(self, conn, own: Account) -> dict:
        """GnuCash import map of the account: token -> {account guid or full name: count}."""
        cached = self._bayes_cache.get(own.guid)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        result = defaultdict(dict)
        prefix = "import-map-bayes/"

        def take(rows, nxt):
            for name, typ, ival, gval in rows:
                if typ == 9 and gval:
                    nxt.append(gval)
                elif typ == 1 and name.startswith(prefix) and "/" in name[len(prefix):]:
                    token, target = name[len(prefix):].rsplit("/", 1)
                    result[token][target] = result[token].get(target, 0) + int(ival or 0)

        # GnuCash stores the map flat ("import-map-bayes/<token>/<account>") on the account; nested
        # frames are followed as well in case a GnuCash version writes them that way
        frontier = []
        take(conn.execute(text(
            "SELECT name, slot_type, int64_val, guid_val FROM slots WHERE obj_guid = :a "
            "AND (name = 'import-map-bayes' OR name LIKE 'import-map-bayes/%')"), {"a": own.guid}), frontier)
        depth = 0
        while frontier and depth < 12:
            nxt = []
            for i in range(0, len(frontier), 400):
                chunk = frontier[i:i + 400]
                names = {f"f{j}": g for j, g in enumerate(chunk)}
                clause = "(" + ", ".join(":" + n for n in names) + ")"
                take(conn.execute(text(
                    f"SELECT name, slot_type, int64_val, guid_val FROM slots WHERE obj_guid IN {clause}"), names), nxt)
            frontier = nxt
            depth += 1
        data = dict(result)
        self._bayes_cache[own.guid] = (time.monotonic(), data)
        return data

    @staticmethod
    def bayes_tokens(day: date, description: str, memos) -> list[str]:
        tokens = [WEEKDAYS_DE[day.weekday()]]
        for value in [description, *memos]:
            tokens += [t for t in (value or "").split(" ") if t]
        seen, out = set(), []
        for t in tokens:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def bayes_counter(self, conn, index: AccountIndex, own: Account, tokens) -> Account | None:
        """GnuCash's naive Bayes account matching (gnc_account_imap_find_account_bayes)."""
        imap = self.bayes_map(conn, own)
        if not imap:
            return None
        probs = {}
        for token in tokens:
            accounts = imap.get(token)
            if not accounts:
                continue
            total = sum(accounts.values())
            if total <= 0:
                continue
            for target, count in accounts.items():
                p = count / total
                prod, diff = probs.get(target, (1.0, 1.0))
                probs[target] = (prod * p, diff * (1 - p))
        best, best_p = None, 0.0
        for target, (prod, diff) in probs.items():
            denom = prod + diff
            p = prod / denom if denom else 0.0
            if p > best_p:
                best, best_p = target, p
        if best is None or best_p < BAYES_THRESHOLD:
            return None
        acc = index.get(best) or index.find(best)
        if acc is None or acc.placeholder or acc.commodity_guid != own.commodity_guid or acc.guid == own.guid:
            return None
        return acc

    def choose_counter(self, conn, index: AccountIndex, entry: Entry, description: str, memo: str):
        """(counter account, source, counter_is_own)"""
        own = entry.own
        if entry.counter_own is not None:
            return entry.counter_own, "own", True
        other = self.own_account_for_iban(conn, index, entry.cp_iban) if entry.cp_iban else None
        if other is not None and other.guid != own.guid and other.commodity_guid == own.commodity_guid:
            transit = index.find(self.cfg.transit_account) if self.cfg.transit_account else None
            between = {a.guid for a in (index.find(r) for r in self.cfg.transit_between) if a is not None}
            if transit is not None and own.guid in between and other.guid in between:
                return transit, "transit", False
            return other, "own", True
        clearing = self.pp_clearing(index, own)
        if clearing is not None:
            return clearing, "pp", False
        acc = self.history_counter(conn, index, own, entry.cp_iban, entry.cp_name)
        if acc is not None and acc.guid != own.guid:
            return acc, "history", False
        acc = self.bayes_counter(conn, index, own, self.bayes_tokens(entry.day, description, [memo]))
        if acc is not None:
            return acc, "bayes", False
        acc = self.fallback_account(index, own.commodity_guid)
        if acc is None:
            raise ImportRejected("destination_name", _("Kein Auffangkonto gefunden – bitte [import] fallback_account in der gnubook-Konfiguration setzen."))
        return acc, "fallback", False

    def pp_clearing(self, index: AccountIndex, own: Account) -> Account | None:
        """Bank lines of a depot's cash account go to the clearing account of the Portfolio Performance link:
        the securities side is booked from PP against the same account, so nothing is booked twice."""
        if self.pp is None or not self.pp.bank_accounts:
            return None
        banks = {a.guid for a in (index.find(ref) for ref in self.pp.bank_accounts) if a is not None}
        if own.guid not in banks:
            return None
        from .pp.settings import resolved

        clearing = index.find(resolved(self.pp, index)["clearing"])
        if clearing is None or clearing.placeholder or clearing.commodity_guid != own.commodity_guid \
                or clearing.guid == own.guid:
            return None
        return clearing

    # ------------------------------------------------------------------ matching existing bookings
    def candidates(self, conn, entry: Entry, description: str, account: Account, signed: Decimal,
                   counter: Account | None, counter_is_own: bool):
        """Existing, not yet imported splits of `account` with value `signed` near the booking date.

        Returns (strong, weak) lists of (split_guid, tx_guid, day, description, similarity, delta, enter_date).
        """
        window = max(self.cfg.transfer_match_days if counter_is_own else 0, self.cfg.match_days)
        start = self.book.day_start_utc(entry.day - timedelta(days=window))
        end = self.book.day_end_utc(entry.day + timedelta(days=window))
        cents = int((signed * 100).to_integral_value())
        rows = conn.execute(text(
            "SELECT s.guid, s.tx_guid, t.post_date, t.description, t.enter_date FROM splits s "
            "JOIN transactions t ON t.guid = s.tx_guid WHERE s.account_guid = :a "
            "AND s.quantity_num * 100 = :c * s.quantity_denom AND t.post_date >= :s AND t.post_date < :e"),
            {"a": account.guid, "c": cents, "s": start, "e": end}).fetchall()
        if not rows:
            return [], []
        linked = self.appdb.linked_split_guids(r[0] for r in rows)
        rows = [r for r in rows if r[0] not in linked]
        if not rows:
            return [], []
        tx_accounts = defaultdict(set)
        tx_guids = list({r[1] for r in rows})
        names = {f"t{j}": g for j, g in enumerate(tx_guids)}
        clause = "(" + ", ".join(":" + n for n in names) + ")"
        for tg, ag in conn.execute(text(f"SELECT tx_guid, account_guid FROM splits WHERE tx_guid IN {clause}"), names):
            tx_accounts[tg].add(ag)
        parsed_new = checkpoints.parse(description, self.book) if entry.amount == 0 else None
        strong, weak = [], []
        for sg, tg, pd, desc, ed in rows:
            day = self.book.day_of(pd)
            delta = abs((day - entry.day).days)
            sim = similarity(description, desc or "")
            if entry.amount == 0:
                same_checkpoint = parsed_new is not None and parsed_new == checkpoints.parse(desc, self.book)
                if not same_checkpoint and delta > self.cfg.match_days:
                    continue
                is_strong = same_checkpoint or sim >= 0.5 or (delta == 0 and sim >= 0.3)
            elif counter_is_own and counter is not None and counter.guid in tx_accounts[tg]:
                is_strong = delta <= self.cfg.transfer_match_days
            else:
                if delta > self.cfg.match_days:
                    continue
                is_strong = sim >= 0.5 or (delta == 0 and sim >= 0.3)
            item = (sg, tg, day, desc or "", sim, delta, self.book.to_utc_naive(ed) or datetime.min)
            (strong if is_strong else weak).append(item)
        order = lambda x: (-x[4], x[5], x[6])  # noqa: E731
        return sorted(strong, key=order), sorted(weak, key=order)

    # ------------------------------------------------------------------ main entry point
    def import_body(self, body: dict, actor: str = "api") -> ImportResult:
        with self._lock:
            return self._import_body(body, actor)

    def _import_body(self, body: dict, actor: str) -> ImportResult:  # noqa: C901
        index = self.book.load_accounts()
        entry = self.parse(body, index)
        h = self.entry_hash(entry)
        k = self.appdb.peek_occurrence(entry.own.guid, h)
        known = self.appdb.find_import(h, k)
        if known is not None:
            self.appdb.commit_occurrence(entry.own.guid, h, k)
            tx_id = self.appdb.tx_id(known["tx_guid"]) if known["tx_guid"] else None
            return ImportResult("duplicate", known["tx_guid"], entry=entry,
                                message=f"Duplikat: bereits importiert am {known['created_at'][:10]}"
                                        + (_(" (Buchung #{a0}).", a0=tx_id) if tx_id else ".")
                                        + _(" Enthält der Auszug diese Zeile wirklich mehrfach, die weitere bitte in gnubook von Hand erfassen."))
        description = self.build_description(entry)
        # same memo format as GnuCash's AqBanking import (also used to recognise the counterparty later)
        memo = self.profile.bank_memo(entry.cp_iban) if entry.cp_iban and entry.counter_own is None else ""
        with self.book.connect() as conn:
            if entry.amount == 0:
                counter, source, counter_is_own = None, "checkpoint", False
            else:
                counter, source, counter_is_own = self.choose_counter(conn, index, entry, description, memo)
            strong, weak = self.candidates(conn, entry, description, entry.own, entry.signed, counter,
                                           counter_is_own)
            if entry.counter_own is not None and not strong:
                # transfer between two own accounts: the other side may have been imported already
                strong, _weak = self.candidates(conn, entry, description, entry.counter_own, -entry.signed,
                                            entry.own, True)
        record = dict(hash=h, occurrence=k, account_guid=entry.own.guid, counterparty_name=entry.cp_name,
                      counterparty_iban=entry.cp_iban, day=entry.day.isoformat(), amount=str(entry.signed),
                      description=description, payload=entry.raw)
        if strong:
            sg, tg = strong[0][0], strong[0][1]
            rid = self.appdb.add_import(tx_guid=tg, split_guid=sg, status="matched", source="match", **record)
            self.appdb.commit_occurrence(entry.own.guid, h, k)
            self.appdb.audit(actor, "import-match", tg, description)
            return ImportResult("matched", tg, record_id=rid, entry=entry, source="match",
                                message=_("Bereits im Buch vorhanden – verknüpft."))

        notes = entry.notes if entry.notes and entry.notes.casefold() != entry.cp_name.casefold() else ""
        splits = [SplitInput(entry.own.guid, entry.signed, memo=memo)]
        if counter is not None:
            splits.append(SplitInput(counter.guid, -entry.signed))
        draft = TxInput(day=entry.day, description=description, splits=splits, notes=notes,
                        currency_guid=entry.own.commodity_guid)
        try:
            tx_guid = create_transaction(self.book, index, draft)
        except ValidationError as exc:
            raise ImportRejected("description", "; ".join(exc.messages))
        with self.book.connect() as conn:
            split_guid = conn.execute(text(
                "SELECT guid FROM splits WHERE tx_guid = :t AND account_guid = :a"),
                {"t": tx_guid, "a": entry.own.guid}).scalar()
        status = "possible_duplicate" if weak else "created"
        rid = self.appdb.add_import(tx_guid=tx_guid, split_guid=split_guid, status=status, source=source,
                                    candidate_tx_guid=weak[0][1] if weak else None, **record)
        self.appdb.commit_occurrence(entry.own.guid, h, k)
        self.appdb.audit(actor, "import", tx_guid, f"{description} ({fmt(entry.signed)} {entry.own.mnemonic})")
        return ImportResult(status, tx_guid, record_id=rid, entry=entry, counter=counter, source=source)


__all__ = ["Importer", "ImportRejected", "ImportResult", "WriteLockError", "normalize_iban", "similarity",
           "IMPORTABLE_TYPES"]
