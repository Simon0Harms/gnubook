from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import text

from gnubook.book import LOCK_TAG, WriteLockError
from gnubook.ledger import balances, load_transaction
from gnubook.writer import (ConflictError, SplitInput, TxInput, ValidationError, create_transaction,
                            delete_transaction, update_transaction)


@pytest.fixture
def ctx(state):
    book = state.book
    idx = book.load_accounts()
    a = {n.split(":")[-1]: idx.find(n).guid for n in (
        "Aktiva:Barvermögen:Girokonto Musterbank", "Aufwendungen:Lebensmittel", "Aufwendungen:Haushalt",
        "Aufwendungen:Wohnen", "Aktiva:Barvermögen:Bargeld")}
    return book, idx, a


def _bal(book, guid):
    with book.connect() as conn:
        return balances(conn, book).get(guid, D(0))


def test_create_update_delete(ctx):
    book, idx, a = ctx
    giro = a["Girokonto Musterbank"]
    before = _bal(book, giro)
    guid = create_transaction(book, idx, TxInput(date(2026, 10, 3), "Wochenmarkt", [
        SplitInput(giro, D("-16.80"), memo="Karte"), SplitInput(a["Lebensmittel"], D("12.50")),
        SplitInput(a["Haushalt"], D("4.30"), memo="Seife")], notes="Notiz"))
    assert _bal(book, giro) == before - D("16.80")
    with book.connect() as conn:
        tx = load_transaction(conn, book, idx, guid)
        slots = dict(conn.execute(text("SELECT name, slot_type FROM slots WHERE obj_guid = :g"), {"g": guid}).fetchall())
        post = conn.execute(text("SELECT post_date FROM transactions WHERE guid = :g"), {"g": guid}).scalar()
    assert tx.day == date(2026, 10, 3) and tx.notes == "Notiz" and len(tx.splits) == 3 and tx.editable
    assert slots == {"date-posted": 10, "notes": 4}
    assert str(post).startswith("2026-10-03 10:59:00")

    bank = next(s for s in tx.splits if s.account_guid == giro)
    food = next(s for s in tx.splits if s.account_guid == a["Lebensmittel"])
    update_transaction(book, idx, guid, TxInput(date(2026, 10, 4), "Wochenmarkt Samstag", [
        SplitInput(giro, D("-20.00"), memo="Karte", guid=bank.guid, reconcile="c"),
        SplitInput(a["Bargeld"], D("20.00"), guid=food.guid)], notes=""), tx.fingerprint)
    with book.connect() as conn:
        tx2 = load_transaction(conn, book, idx, guid)
        notes = conn.execute(text("SELECT COUNT(*) FROM slots WHERE obj_guid = :g AND name = 'notes'"), {"g": guid}).scalar()
    assert tx2.day == date(2026, 10, 4) and tx2.description == "Wochenmarkt Samstag" and notes == 0
    assert sorted((s.account_guid, s.value, s.reconcile) for s in tx2.splits) == sorted(
        [(giro, D("-20.00"), "c"), (a["Bargeld"], D("20.00"), "n")])
    assert {s.guid for s in tx2.splits} == {bank.guid, food.guid}  # splits kept their identity

    with pytest.raises(ConflictError):  # stale fingerprint
        delete_transaction(book, idx, guid, tx.fingerprint)
    delete_transaction(book, idx, guid, tx2.fingerprint)
    assert _bal(book, giro) == before
    with book.connect() as conn:
        assert load_transaction(conn, book, idx, guid) is None
        assert conn.execute(text("SELECT COUNT(*) FROM slots WHERE obj_guid = :g"), {"g": guid}).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM gnclock")).scalar() == 0


def test_zero_single_split(ctx):
    book, idx, a = ctx
    guid = create_transaction(book, idx, TxInput(date(2026, 10, 31), "ENTGELTABSCHLUSS STAND30.10.2026 1,00H",
                                                 [SplitInput(a["Girokonto Musterbank"], D("0.00"))]))
    with book.connect() as conn:
        row = conn.execute(text("SELECT value_num, value_denom, quantity_num, quantity_denom FROM splits "
                                "WHERE tx_guid = :g"), {"g": guid}).fetchall()
    assert [tuple(r) for r in row] == [(0, 100, 0, 100)]


@pytest.mark.parametrize("splits,message", [
    ([("Girokonto Musterbank", "-1.00"), ("Lebensmittel", "0.99")], "nicht ausgeglichen"),
    ([("Girokonto Musterbank", "-1.00"), ("Wohnen", "1.00")], "Platzhalterkonto"),
    ([("Girokonto Musterbank", "-1.001"), ("Lebensmittel", "1.001")], "Nachkommastellen"),
    ([], "mindestens einen Split"),
])
def test_validation(ctx, splits, message):
    book, idx, a = ctx
    with pytest.raises(ValidationError) as exc:
        create_transaction(book, idx, TxInput(date(2026, 10, 1), "x", [SplitInput(a[n], D(v)) for n, v in splits]))
    assert any(message in m for m in exc.value.messages)


def test_refuses_while_gnucash_desktop_has_the_book(ctx):
    book, idx, a = ctx
    with book.engine.begin() as conn:
        conn.execute(text("INSERT INTO gnclock (hostname, pid) VALUES ('desktop-pc', 4711)"))
    assert book.is_locked()
    with pytest.raises(WriteLockError) as exc:
        create_transaction(book, idx, TxInput(date(2026, 10, 1), "x", [
            SplitInput(a["Girokonto Musterbank"], D("-1")), SplitInput(a["Lebensmittel"], D("1"))]))
    assert "desktop-pc" in str(exc.value)
    assert book.remove_foreign_locks() == 1 and not book.is_locked()


def test_stale_own_lock_is_cleaned_up(ctx):
    book, idx, a = ctx
    with book.engine.begin() as conn:
        conn.execute(text("INSERT INTO gnclock (hostname, pid) VALUES (:h, 999999)"), {"h": LOCK_TAG})
    create_transaction(book, idx, TxInput(date(2026, 10, 1), "x", [
        SplitInput(a["Girokonto Musterbank"], D("-1")), SplitInput(a["Lebensmittel"], D("1"))]))
    assert book.lock_holders() == []


def test_reconciled_split_is_protected(ctx):
    book, idx, a = ctx
    giro, food = a["Girokonto Musterbank"], a["Lebensmittel"]
    guid = create_transaction(book, idx, TxInput(date(2026, 10, 1), "abgeglichen", [
        SplitInput(giro, D("-5")), SplitInput(food, D("5"))]))
    with book.engine.begin() as conn:
        conn.execute(text("UPDATE splits SET reconcile_state = 'y' WHERE tx_guid = :g AND account_guid = :a"),
                     {"g": guid, "a": giro})
    with book.connect() as conn:
        tx = load_transaction(conn, book, idx, guid)
    bank = next(s for s in tx.splits if s.account_guid == giro)
    other = next(s for s in tx.splits if s.account_guid == food)
    with pytest.raises(ValidationError):
        update_transaction(book, idx, guid, TxInput(tx.day, "x", [
            SplitInput(giro, D("-6"), guid=bank.guid, reconcile="y"), SplitInput(food, D("6"), guid=other.guid)]),
            tx.fingerprint)
    # changing only the unreconciled split's memo is fine
    update_transaction(book, idx, guid, TxInput(tx.day, "abgeglichen", [
        SplitInput(giro, D("-5"), guid=bank.guid, reconcile="y"),
        SplitInput(food, D("5"), memo="neu", guid=other.guid)]), tx.fingerprint)
    with book.connect() as conn:
        fp = load_transaction(conn, book, idx, guid).fingerprint
    with pytest.raises(ValidationError):
        delete_transaction(book, idx, guid, fp)
