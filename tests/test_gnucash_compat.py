"""Books written by gnubook must read back identically in GnuCash itself.

Needs a Python with the GnuCash bindings (Debian/Ubuntu: apt install python3-gnucash); set
GNUCASH_PYTHON (default /usr/bin/python3). Skipped when the bindings are not available.
"""
import json
import os
import subprocess
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import text

from gnubook.ledger import balances, load_transaction
from gnubook.writer import SplitInput, TxInput, create_transaction, delete_transaction, update_transaction

GNUCASH_PYTHON = os.environ.get("GNUCASH_PYTHON", "/usr/bin/python3")


def _have_bindings():
    try:
        return subprocess.run([GNUCASH_PYTHON, "-c", "import gnucash"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not _have_bindings(), reason="GnuCash Python bindings not available")


def test_gnucash_reads_what_gnubook_wrote(state, api):
    book = state.book
    if not book.is_sqlite:
        pytest.skip("checked with SQLite books")
    idx = book.load_accounts()
    giro = idx.find("Aktiva:Barvermögen:Girokonto Musterbank").guid
    food = idx.find("Aufwendungen:Lebensmittel").guid
    home = idx.find("Aufwendungen:Haushalt").guid
    g = create_transaction(book, idx, TxInput(date(2026, 10, 3), "Wochenmarkt", [
        SplitInput(giro, D("-16.80")), SplitInput(food, D("12.50")), SplitInput(home, D("4.30"))], notes="Notiz"))
    with book.connect() as conn:
        tx = load_transaction(conn, book, idx, g)
    bank = next(s for s in tx.splits if s.account_guid == giro)
    update_transaction(book, idx, g, TxInput(date(2026, 10, 4), "Wochenmarkt", [
        SplitInput(giro, D("-20.00"), guid=bank.guid, reconcile="c"), SplitInput(food, D("20.00"))]), tx.fingerprint)
    g2 = create_transaction(book, idx, TxInput(date(2026, 10, 5), "weg damit", [
        SplitInput(giro, D("-1")), SplitInput(food, D("1"))]))
    with book.connect() as conn:
        fp = load_transaction(conn, book, idx, g2).fingerprint
    delete_transaction(book, idx, g2, fp)
    create_transaction(book, idx, TxInput(date(2026, 10, 31), "ENTGELTABSCHLUSS STAND30.10.2026 1,00H",
                                          [SplitInput(giro, D("0.00"))]))
    ids = {a["attributes"]["name"]: int(a["id"]) for a in api("GET", "/accounts?type=asset").get_json()["data"]}
    r = api("POST", "/transactions", {"transactions": [{
        "type": "withdrawal", "date": "2026-10-06", "amount": 7.5, "description": "REWE Markt", "source_id":
        ids["Aktiva:Barvermögen:Girokonto Musterbank"], "destination_name": "REWE"}]})
    assert r.status_code == 200

    path = book.engine.url.database
    out = subprocess.run([GNUCASH_PYTHON, str(Path(__file__).with_name("gnucash_verify.py")), f"sqlite3://{path}"],
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    gnc = json.loads(out.stdout.strip().splitlines()[-1])
    with book.connect() as conn:
        ours = balances(conn, book)
        n_splits = dict(conn.execute(text("SELECT account_guid, COUNT(*) FROM splits GROUP BY account_guid")).fetchall())
    for guid, acc in idx.by_guid.items():
        assert D(gnc[guid]["balance"]) == ours.get(guid, D(0)), acc.full_name
        assert gnc[guid]["splits"] == n_splits.get(guid, 0), acc.full_name
