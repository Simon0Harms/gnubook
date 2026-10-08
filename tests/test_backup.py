import json
import subprocess
from datetime import date
from decimal import Decimal as D
from pathlib import Path


from gnubook.backup import BackupWriter
from gnubook.ledger import balances
from gnubook.writer import SplitInput, TxInput, create_transaction

from .test_gnucash_compat import GNUCASH_PYTHON, _have_bindings


def test_gnucash_file_after_write(state, tmp_path):
    book = state.book
    target = tmp_path / "backup" / "buch.gnucash"
    bw = BackupWriter(book, str(target), keep=3, delay=0)
    idx = book.load_accounts()
    giro = idx.find("Aktiva:Barvermögen:Girokonto Musterbank").guid
    food = idx.find("Aufwendungen:Lebensmittel").guid
    for i in range(4):
        create_transaction(book, idx, TxInput(date(2026, 10, 3), f"Test {i}", [
            SplitInput(giro, D("-1")), SplitInput(food, D("1"))]))
        bw.run_now()
    assert bw.last_error is None and target.exists()
    assert len(list(target.parent.glob("buch.*.gnucash"))) == 2  # keep=3: current + 2 older
    if _have_bindings():
        out = subprocess.run([GNUCASH_PYTHON, str(Path(__file__).with_name("gnucash_verify.py")), f"sqlite3://{target}"],
                             capture_output=True, text=True, timeout=300)
        assert out.returncode == 0, out.stderr
        gnc = json.loads(out.stdout.strip().splitlines()[-1])
        with book.connect() as conn:
            ours = balances(conn, book)
        for guid in idx.by_guid:
            assert D(gnc[guid]["balance"]) == ours.get(guid, D(0))
