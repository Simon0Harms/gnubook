"""Write side: create, change and delete transactions through piecash.

Every write runs inside Book.exclusive() (no writes while GnuCash Desktop has the book open) and is
validated here first, so piecash only ever sees balanced, single-currency transactions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from .book import AccountIndex, Book, BookError
from .i18n import _
from .ledger import TxView, current_fingerprint, load_transaction
from .money import ZERO, fmt, fraction_digits

MAX_TEXT = 2048    # varchar(2048) in GnuCash's schema
MAX_NOTES = 4096   # slots.string_val varchar(4096)
MAX_AMOUNT = Decimal("1000000000000")  # keeps num/denom far inside GnuCash's BIGINT columns


class ValidationError(BookError):
    def __init__(self, messages):
        self.messages = list(messages) if isinstance(messages, (list, tuple)) else [str(messages)]
        super().__init__("; ".join(self.messages))


class ConflictError(BookError):
    def __init__(self):
        super().__init__(_("Die Buchung wurde inzwischen an anderer Stelle geändert (z. B. in GnuCash Desktop). "
                           "Bitte neu laden und die Änderung erneut vornehmen."))


@dataclass
class SplitInput:
    account_guid: str
    value: Decimal
    memo: str = ""
    action: str = ""
    reconcile: str = "n"
    guid: str | None = None  # existing split


@dataclass
class TxInput:
    day: date
    description: str
    splits: list[SplitInput] = field(default_factory=list)
    num: str = ""
    notes: str = ""
    currency_guid: str | None = None


def validate(index: AccountIndex, tx: TxInput, existing: TxView | None = None) -> str:
    """Check a transaction draft. Returns the transaction currency GUID or raises ValidationError."""
    errors = []
    if existing is not None and not existing.editable:
        raise ValidationError([_("Diese Buchung kann nur in GnuCash Desktop geändert werden: {reasons}",
                                 reasons=", ".join(existing.readonly_reasons))])
    if not isinstance(tx.day, date) or not (1900 <= tx.day.year <= 2199):
        errors.append(_("Ungültiges Buchungsdatum."))
    for label, value, limit in (("Beschreibung", tx.description, MAX_TEXT), ("Nummer", tx.num, MAX_TEXT),
                                ("Notizen", tx.notes, MAX_NOTES)):
        if value and len(value) > limit:
            errors.append(_("{label} ist zu lang (max. {limit} Zeichen).", label=_(label), limit=limit))
    if not tx.splits:
        errors.append(_("Die Buchung braucht mindestens einen Split."))

    currency_guid = existing.currency_guid if existing is not None else tx.currency_guid
    accounts = []
    for i, sp in enumerate(tx.splits, 1):
        acc = index.get(sp.account_guid)
        if acc is None:
            errors.append(_("Zeile {n}: Konto unbekannt.", n=i))
            continue
        if acc.placeholder:
            errors.append(_("Zeile {n}: {account} ist ein Platzhalterkonto und nimmt keine Buchungen an.", n=i,
                            account=acc.full_name))
        if sp.reconcile not in ("n", "c", "y"):
            errors.append(_("Zeile {n}: ungültiger Abgleichstatus.", n=i))
        if len(sp.memo or "") > MAX_TEXT:
            errors.append(_("Zeile {n}: Memo zu lang.", n=i))
        accounts.append(acc)
    if errors:
        raise ValidationError(errors)

    if currency_guid is None:
        currency_guid = accounts[0].commodity_guid
    currency = index.commodities.get(currency_guid)
    if currency is None or not currency.is_currency:
        raise ValidationError([_("{account} wird nicht in einer Währung geführt – "
                                 "Wertpapierbuchungen bitte in GnuCash Desktop erfassen.", account=accounts[0].full_name)])
    places = fraction_digits(currency.fraction)
    total = ZERO
    for i, (sp, acc) in enumerate(zip(tx.splits, accounts), 1):
        if acc.commodity_guid != currency_guid:
            errors.append(_("Zeile {n}: {account} wird in {acc_currency} geführt, die Buchung in {tx_currency}. "
                            "Buchungen über mehrere Währungen bitte in GnuCash Desktop erfassen.", n=i,
                            account=acc.full_name, acc_currency=acc.mnemonic, tx_currency=currency.mnemonic))
        if sp.value != sp.value.quantize(Decimal(1).scaleb(-places)):
            errors.append(_("Zeile {n}: höchstens {places} Nachkommastellen.", n=i, places=places))
        elif abs(sp.value) >= MAX_AMOUNT:
            errors.append(_("Zeile {n}: Betrag unplausibel groß.", n=i))
        total += sp.value
    if total != 0:
        errors.append(_("Die Buchung ist nicht ausgeglichen (Differenz {diff} {currency}).", diff=fmt(total, places),
                        currency=currency.mnemonic))

    # reconciled splits ('y') must stay as they are
    if existing is not None:
        old = {s.guid: s for s in existing.splits}
        new_guids = {sp.guid for sp in tx.splits if sp.guid}
        for s in existing.splits:
            if s.reconcile == "y" and s.guid not in new_guids:
                errors.append(_("Abgeglichener Split auf {account} darf nicht gelöscht werden.",
                                account=s.account.full_name))
        for sp in tx.splits:
            o = old.get(sp.guid) if sp.guid else None
            if sp.guid and o is None:
                errors.append(_("Ein Split gehört nicht (mehr) zu dieser Buchung – bitte neu laden."))
            elif o is not None and o.reconcile == "y" and (
                    o.account_guid != sp.account_guid or o.value != sp.value or sp.reconcile != "y"):
                errors.append(_("Abgeglichener Split auf {account} kann nur in GnuCash geändert werden.",
                                account=o.account.full_name))
            elif (o is None or o.reconcile != "y") and sp.reconcile == "y":
                errors.append(_("Den Status „abgeglichen“ (y) setzt nur der Kontenabgleich in GnuCash."))
    if errors:
        raise ValidationError(errors)
    return currency_guid


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _pc_objects(pc_book, currency_guid, account_guids):
    from piecash import Account as PcAccount, Commodity as PcCommodity

    s = pc_book.session
    cur = s.query(PcCommodity).filter_by(guid=currency_guid).one()
    accs = {g: s.query(PcAccount).filter_by(guid=g).one() for g in set(account_guids)}
    return cur, accs


def _places(index, currency_guid):
    return fraction_digits(index.commodities[currency_guid].fraction)


def create_transaction(book: Book, index: AccountIndex, tx: TxInput) -> str:
    currency_guid = validate(index, tx)
    places = _places(index, currency_guid)
    q = Decimal(1).scaleb(-places)
    with book.exclusive():
        from piecash import Split, Transaction

        pc = book.piecash_book()
        try:
            cur, accs = _pc_objects(pc, currency_guid, [s.account_guid for s in tx.splits])
            splits = [Split(account=accs[s.account_guid], value=s.value.quantize(q), memo=s.memo or "",
                            action=s.action or "", reconcile_state=s.reconcile or "n") for s in tx.splits]
            new = Transaction(currency=cur, description=tx.description or "", post_date=tx.day,
                              num=tx.num or "", enter_date=_now_utc(), splits=splits)
            if tx.notes:
                new.notes = tx.notes
            pc.save()
            return new.guid
        except Exception:
            pc.cancel()
            raise
        finally:
            pc.close()


def update_transaction(book: Book, index: AccountIndex, guid: str, tx: TxInput, expected_fingerprint: str):
    with book.connect() as conn:
        existing = load_transaction(conn, book, index, guid)
    if existing is None:
        raise ValidationError([_("Die Buchung existiert nicht mehr.")])
    currency_guid = validate(index, tx, existing)
    q = Decimal(1).scaleb(-_places(index, currency_guid))
    with book.exclusive():
        with book.connect() as conn:
            if current_fingerprint(conn, book, index, guid) != expected_fingerprint:
                raise ConflictError()
        from piecash import Split, Transaction

        pc = book.piecash_book()
        try:
            s = pc.session
            ptx = s.query(Transaction).filter_by(guid=guid).one()
            _pc, accs = _pc_objects(pc, currency_guid, [sp.account_guid for sp in tx.splits])
            if (ptx.description or "") != (tx.description or ""):
                ptx.description = tx.description or ""
            if (ptx.num or "") != (tx.num or ""):
                ptx.num = tx.num or ""
            if existing.day != tx.day:
                ptx.post_date = tx.day
            if (existing.notes or "") != (tx.notes or ""):
                ptx.notes = tx.notes if tx.notes else None
            old = {sp.guid: sp for sp in ptx.splits}
            for inp in tx.splits:
                value = inp.value.quantize(q)
                psp = old.pop(inp.guid, None) if inp.guid else None
                if psp is None:
                    ptx.splits.append(Split(account=accs[inp.account_guid], value=value, memo=inp.memo or "",
                                            action=inp.action or "", reconcile_state=inp.reconcile or "n"))
                    continue
                if psp.account.guid != inp.account_guid:
                    psp.account = accs[inp.account_guid]
                if psp.value != value or psp.quantity != value:
                    psp.value = value
                    psp.quantity = value
                if (psp.memo or "") != (inp.memo or ""):
                    psp.memo = inp.memo or ""
                if (psp.action or "") != (inp.action or ""):
                    psp.action = inp.action or ""
                if psp.reconcile_state != inp.reconcile:
                    psp.reconcile_state = inp.reconcile
            for psp in old.values():
                ptx.splits.remove(psp)
            pc.save()
        except Exception:
            pc.cancel()
            raise
        finally:
            pc.close()


def delete_transaction(book: Book, index: AccountIndex, guid: str, expected_fingerprint: str):
    with book.connect() as conn:
        existing = load_transaction(conn, book, index, guid)
    if existing is None:
        return
    if not existing.editable:
        raise ValidationError([_("Diese Buchung kann nur in GnuCash Desktop gelöscht werden: {reasons}",
                                 reasons=", ".join(existing.readonly_reasons))])
    if any(s.reconcile == "y" for s in existing.splits):
        raise ValidationError([_("Buchungen mit abgeglichenen Splits bitte in GnuCash Desktop löschen.")])
    with book.exclusive():
        with book.connect() as conn:
            if current_fingerprint(conn, book, index, guid) != expected_fingerprint:
                raise ConflictError()
        from piecash import Transaction

        pc = book.piecash_book()
        try:
            ptx = pc.session.query(Transaction).filter_by(guid=guid).one()
            pc.delete(ptx)
            pc.save()
        except Exception:
            pc.cancel()
            raise
        finally:
            pc.close()
