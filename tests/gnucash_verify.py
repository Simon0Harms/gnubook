"""Read a book with the official GnuCash engine (python3-gnucash) and print balances as JSON.

Usage: python3 gnucash_verify.py sqlite3:///path/to/book.gnucash
Runs with the system Python that has the GnuCash bindings, not inside gnubook's virtualenv.
"""
import json
import sys
from decimal import Decimal

from gnucash import Session, SessionOpenMode

with Session(sys.argv[1], SessionOpenMode.SESSION_READ_ONLY) as session:
    root = session.book.get_root_account()
    out = {}
    for acc in root.get_descendants():
        bal = acc.GetBalance()
        out[acc.GetGUID().to_string()] = {
            "balance": str(Decimal(bal.num()) / Decimal(bal.denom())),
            "splits": len(acc.GetSplitList()),
        }
    print(json.dumps(out))
