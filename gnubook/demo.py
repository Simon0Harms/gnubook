"""Synthetic demo book (fictional person and banks) for tests, screenshots and trying gnubook out."""
from __future__ import annotations

import random
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal as D

from sqlalchemy import create_engine, text

DEMO_BLZ = {"Musterbank": "10010010", "Beispielbank": "20020020"}
DEMO_KTO = {"Musterbank": "0123456789", "Beispielbank": "0987654321"}


def german_iban(blz: str, kto: str) -> str:
    bban = blz + kto.rjust(10, "0")
    num = int(bban + "131400")  # "DE" = 13 14, check digits 00
    return f"DE{98 - num % 97:02d}{bban}"


def _month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _fmt_bank(v: D) -> str:
    s = f"{abs(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s}{'H' if v >= 0 else 'S'}"


def create_demo_book(target: str, start: date = date(2025, 8, 1), months: int = 14, seed: int = 7,
                     with_deviation: bool = True) -> str:
    """Create a GnuCash book with ~14 months of plausible data. Returns the SQLAlchemy URL.

    `target` is a file path (SQLite) or a postgresql:// URL (the database is recreated!).
    """
    import piecash
    from piecash import Account, Split, Transaction

    rnd = random.Random(seed)
    url = target if "://" in target else f"sqlite:///{target}"
    if url.startswith("sqlite"):
        book = piecash.create_book(sqlite_file=url, currency="EUR", overwrite=True)
    else:
        book = piecash.create_book(uri_conn=url, currency="EUR", overwrite=True)
    eur = book.default_currency

    def acc(name, typ, parent, placeholder=False, code="", description=""):
        a = Account(name=name, type=typ, parent=parent, commodity=eur, placeholder=int(placeholder), code=code,
                    description=description)
        return a

    root = book.root_account
    aktiva = acc("Aktiva", "ASSET", root, True)
    bar = acc("Barvermögen", "ASSET", aktiva, True)
    giro = acc("Girokonto Musterbank", "BANK", bar, code=german_iban(DEMO_BLZ["Musterbank"], DEMO_KTO["Musterbank"]))
    giro2 = acc("Girokonto Beispielbank", "BANK", bar)
    tagesgeld = acc("Tagesgeld", "BANK", bar)
    cash = acc("Bargeld", "CASH", bar)
    anlagen = acc("Geldanlagen", "ASSET", aktiva, True)
    acc("Depot Verrechnung", "ASSET", anlagen)
    fremd = acc("Fremdkapital", "LIABILITY", root, True)
    kk = acc("Kreditkarte", "CREDIT", fremd)
    ertrag = acc("Erträge", "INCOME", root, True)
    gehalt = acc("Gehalt", "INCOME", ertrag)
    zinsen = acc("Zinsen", "INCOME", ertrag)
    aufw = acc("Aufwendungen", "EXPENSE", root, True)
    wohnen = acc("Wohnen", "EXPENSE", aufw, True)
    miete = acc("Miete", "EXPENSE", wohnen)
    strom = acc("Strom", "EXPENSE", wohnen)
    internet = acc("Internet und Telefon", "EXPENSE", wohnen)
    lebensm = acc("Lebensmittel", "EXPENSE", aufw)
    haushalt = acc("Haushalt", "EXPENSE", aufw)
    vers = acc("Versicherungen", "EXPENSE", aufw)
    mobil = acc("Mobilität", "EXPENSE", aufw, True)
    tanken = acc("Tanken", "EXPENSE", mobil)
    freizeit = acc("Freizeit", "EXPENSE", aufw)
    gebuehr = acc("Bankgebühren", "EXPENSE", aufw)
    equity = acc("Anfangsbestand", "EQUITY", root)
    acc("Ausgleichskonto-EUR", "BANK", root)
    book.save()

    iban2 = german_iban(DEMO_BLZ["Beispielbank"], DEMO_KTO["Beispielbank"])
    partners = {
        "Beispiel GmbH": german_iban("30030030", "0000111222"),
        "Hausverwaltung Sonnenhof": german_iban("30030030", "0000333444"),
        "Stadtwerke Musterstadt": german_iban("40040040", "0000555666"),
        "NetzFix Telekommunikation": german_iban("40040040", "0000777888"),
        "Sorgenfrei Versicherung AG": german_iban("50050050", "0000999000"),
    }
    specs = []  # (date, description, notes, [(account, value, memo, reconcile)])

    def add(d, desc, splits, notes=""):
        specs.append((d, desc, notes, splits))

    add(start, "Eröffnungssaldo", [(giro, D("2500.00"), "", "c"), (giro2, D("800.00"), "", "c"),
                                     (tagesgeld, D("5000.00"), "", "c"), (cash, D("120.00"), "", "n"),
                                     (equity, D("-8420.00"), "", "n")])
    shops = [("REWE Markt Musterstadt", lebensm, (18, 95)), ("EDEKA Frischecenter", lebensm, (12, 70)),
             ("Bäckerei Krume", lebensm, (3, 14)), ("Drogerie Glanz", haushalt, (6, 40))]
    for m in range(months):
        first = date(start.year + (start.month - 1 + m) // 12, (start.month - 1 + m) % 12 + 1, 1)
        end = _month_end(first)
        tag = f"{first:%m/%Y}"
        add(first, "DAUERAUFTRAG MIETE; Hausverwaltung Sonnenhof",
            [(giro, D("-950.00"), "Konto " + partners["Hausverwaltung Sonnenhof"], "c"), (miete, D("950.00"), "", "n")])
        add(first + timedelta(days=2), f"LASTSCHRIFT ABSCHLAG STROM {tag}; Stadtwerke Musterstadt",
            [(giro, D("-62.00"), "Konto " + partners["Stadtwerke Musterstadt"], "c"), (strom, D("62.00"), "", "n")])
        add(first + timedelta(days=4), f"LASTSCHRIFT RECHNUNG {tag}; NetzFix Telekommunikation",
            [(giro, D("-39.99"), "Konto " + partners["NetzFix Telekommunikation"], "c"), (internet, D("39.99"), "", "n")])
        add(first + timedelta(days=5), "LASTSCHRIFT HAFTPFLICHT/HAUSRAT; Sorgenfrei Versicherung AG",
            [(giro, D("-41.20"), "Konto " + partners["Sorgenfrei Versicherung AG"], "c"), (vers, D("41.20"), "", "n")])
        add(first + timedelta(days=6), "ÜBERWEISUNG Umbuchung Haushaltskonto; Max Mustermann",
            [(giro, D("-200.00"), "Konto " + iban2, "c"), (giro2, D("200.00"), "", "n")])
        add(first + timedelta(days=1), "DAUERAUFTRAG Sparrate; Max Mustermann",
            [(giro, D("-1200.00"), "", "c"), (tagesgeld, D("1200.00"), "", "n")])
        salary_day = end - timedelta(days=max(0, end.weekday() - 4))
        add(salary_day, f"GUTSCHRIFT GEHALT {tag}; Beispiel GmbH",
            [(giro, D("3150.00"), "Konto " + partners["Beispiel GmbH"], "c"), (gehalt, D("-3150.00"), "", "n")])
        for _ in range(rnd.randint(6, 10)):
            name, target, (lo, hi) = rnd.choice(shops)
            d = first + timedelta(days=rnd.randint(0, (end - first).days))
            v = D(rnd.randint(lo * 100, hi * 100)) / 100
            src = rnd.choice([giro, giro, giro2, cash])
            memo = "" if src is cash else "Kartenzahlung"
            if name.startswith("REWE") and rnd.random() < 0.35:  # split purchase
                part = (v * D("0.3")).quantize(D("0.01"))
                add(d, f"KARTENZAHLUNG {name}", [(src, -v, memo, "n"), (lebensm, v - part, "", "n"),
                                                  (haushalt, part, "Spülmittel, Küchenrolle", "n")])
            else:
                add(d, f"KARTENZAHLUNG {name}" if src is not cash else name, [(src, -v, memo, "n"), (target, v, "", "n")])
        for _ in range(rnd.randint(1, 3)):
            d = first + timedelta(days=rnd.randint(0, (end - first).days))
            v = D(rnd.randint(4500, 8200)) / 100
            add(d, "Tankstelle Am Ring", [(kk, -v, "", "n"), (tanken, v, "", "n")])
        if rnd.random() < 0.6:
            d = first + timedelta(days=rnd.randint(0, (end - first).days))
            v = D(rnd.randint(1500, 6000)) / 100
            add(d, "Kino und Essen", [(kk, -v, "", "n"), (freizeit, v, "", "n")],
                notes="Mit Freunden, Rechnung geteilt")
        add(first + timedelta(days=14), "Bargeld abgehoben", [(giro, D("-100.00"), "Geldautomat", "c"), (cash, D("100.00"), "", "n")])
        if m % 3 == 2:
            v = D(rnd.randint(800, 1600)) / 100
            add(end, "Zinsgutschrift Tagesgeld", [(tagesgeld, v, "", "n"), (zinsen, -v, "", "n")])
        specs.append(("KK", first + timedelta(days=19), m))  # credit card settlement, computed below

    # credit card settlement: pay the previous month's card spending
    final = []
    kk_balance_by_month = {}
    for spec in specs:
        if spec[0] != "KK":
            final.append(spec)
    for spec in specs:
        if spec[0] == "KK":
            _, d, m = spec
            spent = sum((v for (dd, _, _, sp) in final for (a, v, _, _) in sp if a is kk and dd < d.replace(day=1)
                         and dd >= (d.replace(day=1) - timedelta(days=1)).replace(day=1)), D(0))
            if spent:
                final.append((d, "LASTSCHRIFT KREDITKARTENABRECHNUNG; Musterbank Card Services",
                              "", [(giro, spent, "", "c"), (kk, -spent, "", "n")]))
            kk_balance_by_month[m] = spent

    # monthly closing lines with the bank balance (fee 4,95), one deviation on purpose
    def balance_through(account, day, extra=D(0)):
        return sum((v for (dd, _, _, sp) in final for (a, v, _, _) in sp if a is account and dd <= day), D(0)) + extra

    for m in range(months):
        first = date(start.year + (start.month - 1 + m) // 12, (start.month - 1 + m) % 12 + 1, 1)
        stand_day = _month_end(first)
        missing = D("-23.40") if with_deviation and m == months - 6 else D(0)  # bank knows a booking the book lacks
        stand = balance_through(giro, stand_day, missing)
        end_saldo = stand - D("4.95")
        final.append((stand_day + timedelta(days=1),
                      f"ENTGELTABSCHLUSS **ENDSALDO** {_fmt_bank(end_saldo)} STAND{stand_day:%d.%m.%Y} {_fmt_bank(stand)}",
                      "", [(giro, D("-4.95"), "Konto 0000000000 Bank " + DEMO_BLZ["Musterbank"], "c"),
                           (gebuehr, D("4.95"), "", "n")]))
        if m % 3 == 2:  # quarterly statement of the second bank (0,00 line)
            stand2 = balance_through(giro2, stand_day)
            final.append((stand_day + timedelta(days=1),
                          f"ABRECHNUNG {stand_day:%d.%m.%Y} Information zur Abrechnung Kontostand am "
                          f"{stand_day:%d.%m.%Y} {_fmt_bank(stand2)[:-1]} {'+' if stand2 >= 0 else '-'}",
                          "", [(giro2, D("0.00"), "", "n")]))

    final.sort(key=lambda s: s[0])
    for n, (d, desc, notes, splits) in enumerate(final):
        entered = datetime.combine(d, time(17, 0), tzinfo=timezone.utc) + timedelta(minutes=n % 300)
        tx = Transaction(currency=eur, description=desc, post_date=d, enter_date=entered,
                         splits=[Split(account=a, value=v, memo=memo, reconcile_state=rs) for a, v, memo, rs in splits])
        if notes:
            tx.notes = notes
    book.save()
    guids = {"giro": giro.guid, "giro2": giro2.guid, "lebensm": lebensm.guid, "tanken": tanken.guid,
             "gebuehr": gebuehr.guid}
    pc_engine = book.session.bind
    book.close()
    pc_engine.dispose()  # piecash keeps its own engine; release the connection

    # GnuCash online-banking data and a small Bayesian import map (stored the way GnuCash stores them)
    engine = create_engine(url)
    with engine.begin() as conn:
        frame = uuid.uuid4().hex
        conn.execute(text("INSERT INTO slots (obj_guid, name, slot_type, guid_val) VALUES (:o, 'hbci', 9, :f)"),
                     {"o": guids["giro2"], "f": frame})
        for name, value in (("hbci/bank-code", DEMO_BLZ["Beispielbank"]), ("hbci/account-id", DEMO_KTO["Beispielbank"].lstrip("0"))):
            conn.execute(text("INSERT INTO slots (obj_guid, name, slot_type, string_val) VALUES (:o, :n, 4, :v)"),
                         {"o": frame, "n": name, "v": value})
        for token, target, count in (("REWE", "lebensm", 6), ("EDEKA", "lebensm", 4), ("Markt", "lebensm", 3),
                                     ("Tankstelle", "tanken", 3), ("ENTGELTABSCHLUSS", "gebuehr", 9)):
            conn.execute(text("INSERT INTO slots (obj_guid, name, slot_type, int64_val) VALUES (:o, :n, 1, :v)"),
                         {"o": guids["giro"], "n": f"import-map-bayes/{token}/{guids[target]}", "v": count})
    engine.dispose()
    return url


def main():  # python -m gnubook.demo PATH
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "demo.gnucash"
    print(create_demo_book(path))


if __name__ == "__main__":
    main()
