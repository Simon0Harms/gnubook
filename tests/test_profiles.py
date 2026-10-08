import json
import re
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from gnubook import checkpoints as cps
from gnubook.banks import BalancePattern, get_profile
from gnubook.writer import SplitInput, TxInput, create_transaction

EN_PATTERN = {"stand": r"Balance on (?P<day>\d{2})/(?P<month>\d{2})/(?P<year>\d{4}):? (?P<sign>-?)(?P<amount>[\d,]+\.\d{2})",
              "keyword": "balance", "decimal": ".", "thousands": ","}


def test_german_profile_is_default():
    p = cps.parse("ENTGELTABSCHLUSS **ENDSALDO** 1.234,56H STAND29.05.2026 1.239,51H")
    assert (p.stand_date, p.stand, p.endsaldo) == (date(2026, 5, 29), D("1239.51"), D("1234.56"))


def test_generic_profile_only_uses_configured_patterns():
    generic = get_profile("generic")
    assert generic.parse_checkpoint("STAND29.05.2026 1.239,51H") is None
    custom = get_profile("generic", [EN_PATTERN])
    p = custom.parse_checkpoint("Statement fee; Balance on 30/04/2026: -1,234.50")
    assert (p.stand_date, p.stand, p.endsaldo) == (date(2026, 4, 30), D("-1234.50"), None)
    assert custom.checkpoint_keywords() == ["balance"]
    # the German profile keeps its own formats and can have extra ones
    de = get_profile("de", [BalancePattern(**EN_PATTERN)])
    assert de.parse_checkpoint("Balance on 30/04/2026: 5.00").stand == D("5.00")
    assert "stand" in de.checkpoint_keywords()


def test_booking_text_and_memo_per_profile():
    de, gen = get_profile("de"), get_profile("generic")
    assert de.booking_text("MIETE", "Hausverwaltung") == "MIETE; Hausverwaltung"
    assert gen.booking_text("Rent", "Landlord") == "Landlord – Rent"
    assert de.bank_memo("DE00") == "Konto DE00" and gen.bank_memo("GB00") == "IBAN GB00"


def test_book_profile_and_import_settings(app, state, api):
    reg = app.extensions["gnubook"]
    bid = state.id
    reg.cfg.checkpoints.patterns = [EN_PATTERN]
    reg.system.set_book_import(bid, "generic", {"fallback_account": "Aufwendungen:Freizeit", "match_days": 1})
    ctx = reg.context(bid)
    assert ctx.book.profile.name == "generic" and ctx.importer.cfg.match_days == 1
    idx = ctx.book.load_accounts()
    giro = idx.find("Aktiva:Barvermögen:Girokonto Musterbank").guid
    create_transaction(ctx.book, idx, TxInput(date(2026, 10, 31), "Balance on 31/10/2026: 1.00",
                                              [SplitInput(giro, D("0.00"))]))
    with ctx.book.connect() as conn:
        checks = cps.evaluate(conn, ctx.book, idx, {giro})
    # German closing lines are no longer recognised, the configured English one is
    assert len(checks[giro].checkpoints) == 1 and checks[giro].checkpoints[0].stand_date == date(2026, 10, 31)
    ids = {a["attributes"]["name"]: int(a["id"]) for a in api("GET", "/accounts?type=asset").get_json()["data"]}
    r = api("POST", "/transactions", {"transactions": [{"type": "withdrawal", "date": "2026-10-02", "amount": 9.99,
            "description": "Abo", "source_id": ids["Aktiva:Barvermögen:Girokonto Musterbank"],
            "destination_name": "Neu Ltd", "destination_iban": "GB33BUKB20201555555555"}]})
    t = r.get_json()["data"]["attributes"]["transactions"][0]
    assert t["description"] == "Neu Ltd – Abo" and t["destination_name"] == "Aufwendungen:Freizeit"


def test_admin_form_saves_profile(client, state):
    reg = client.application.extensions["gnubook"]
    page = client.get("/admin/books").text
    assert "Bankprofil" in page
    r = client.post(f"/admin/books/{state.id}/import", data={
        "csrf_token": client.csrf, "profile": "generic", "fallback_account": "", "transit_account": "",
        "transit_between": "", "accounts": "", "match_days": "2", "transfer_match_days": "",
        "iban_map": "DE89 3704 0044 0532 0130 00 = Aktiva:Barvermögen:Tagesgeld"})
    assert r.status_code == 302
    row = reg.system.book(state.id)
    assert row["profile"] == "generic"
    assert json.loads(row["import_settings"])["iban_map"] == {"DE89370400440532013000": "Aktiva:Barvermögen:Tagesgeld"}
    r = client.post(f"/admin/books/{state.id}/import", data={"csrf_token": client.csrf, "profile": "de",
                                                             "iban_map": "kaputt"}, follow_redirects=True)
    assert "IBAN = Konto" in r.text


def test_all_ui_strings_have_english_translation():
    from gnubook.translations_en import EN

    root = Path(__file__).resolve().parent.parent / "gnubook"
    rx = re.compile(r"""_\(\s*(["'])((?:(?!\1).)+)\1""", re.S)
    missing = set()
    for path in list(root.rglob("*.html")) + list(root.rglob("*.py")):
        for m in rx.finditer(path.read_text(encoding="utf-8")):
            if path.name != "i18n.py" and any(ch.isalpha() for ch in m.group(2)) and m.group(2) not in EN:
                missing.add(f"{path.name}: {m.group(2)}")
    assert not missing, sorted(missing)
