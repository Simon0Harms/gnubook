from datetime import date
from decimal import Decimal as D

import pytest

from gnubook import checkpoints as cps


@pytest.mark.parametrize("text,expected", [
    ("ENTGELTABSCHLUSS **ENDSALDO**     1.234,56H STAND29.05.2026     1.239,51H",
     (date(2026, 5, 29), D("1239.51"), D("1234.56"))),
    ("ENTGELTABSCHLUSS **ENDSALDO** 512,30H STAND28.03.2024 516,20H", (date(2024, 3, 28), D("516.20"), D("512.30"))),
    ("Stand 31.05.2026 1.234,56 H", (date(2026, 5, 31), D("1234.56"), None)),
    ("STAND31.05.2026 12,00S", (date(2026, 5, 31), D("-12.00"), None)),
    ("ABSCHLUSS Abrechnung 28.03.2024 Information zur Abrechnung Kontostand am 28.03.2024 47,11 + ----",
     (date(2024, 3, 28), D("47.11"), None)),
    ("Kontostand am 28.03.2024 47,11 -", (date(2024, 3, 28), D("-47.11"), None)),
    ("Kontostand am 28.03.2024 -1.047,11", (date(2024, 3, 28), D("-1047.11"), None)),
])
def test_parse(text, expected):
    p = cps.parse(text)
    assert (p.stand_date, p.stand, p.endsaldo) == expected


@pytest.mark.parametrize("text", [
    "SILIKON WIDERSTANDSFÄHI G GEGEN WASSER",
    "STANDARDLASTSCHRIFT 12.03.2026 15,00 S",
    "STAND31.02.2026 12,00H",
    "Stand 31.05.2026 12,00 Schuhe",
    "RECHNUNG Abrechnung 31.03.2026 Information zur Abrechnu ng Kontostand am 31.03.2025; BEISPIELBANK AG",
    "",
    None,
])
def test_parse_none(text):
    assert cps.parse(text) is None


def _checks(state):
    book = state.book
    idx = book.load_accounts()
    with book.connect() as conn:
        return idx, cps.evaluate(conn, book, idx, None, state.appdb.acceptances())


def test_demo_book_checkpoints(state):
    idx, checks = _checks(state)
    giro = idx.find("Aktiva:Barvermögen:Girokonto Musterbank")
    giro2 = idx.find("Aktiva:Barvermögen:Girokonto Beispielbank")
    assert checks[giro.guid].counts == {"ok": 13, "accepted": 0, "open": 1}
    assert checks[giro2.guid].counts == {"ok": 4, "accepted": 0, "open": 0}
    bad = [cp for cp in checks[giro.guid].checkpoints if not cp.ok][0]
    assert bad.diff_stand == D("23.40") and bad.diff_end == D("23.40") and bad.new_diff == D("23.40")
    # the demo's bank only "knows" the missing booking in that one statement, so the next one matches
    nxt = checks[giro.guid].checkpoints[checks[giro.guid].checkpoints.index(bad) + 1]
    assert nxt.ok and nxt.new_diff == D("-23.40")


def test_acceptance_survives_only_same_difference(state):
    idx, checks = _checks(state)
    giro = idx.find("Aktiva:Barvermögen:Girokonto Musterbank")
    bad = [cp for cp in checks[giro.guid].checkpoints if not cp.ok][0]
    state.appdb.accept(giro.guid, bad.tx_guid, bad.diff_stand, bad.diff_end, "bekannt")
    _, checks = _checks(state)
    assert checks[giro.guid].counts == {"ok": 13, "accepted": 1, "open": 0}
    state.appdb.accept(giro.guid, bad.tx_guid, D("1.00"), D("1.00"), "andere Differenz")
    _, checks = _checks(state)
    assert checks[giro.guid].counts["open"] == 1
