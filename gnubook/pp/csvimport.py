"""Securities account transactions from CSV or Excel files for the PDF import page.

Read are
- CSV files like Portfolio Performance's own export or import (German or English column names, ';' ',' or tab,
  dates like 31.12.2024 or 2024-12-31, amounts like 1.234,56 or 1,234.56), and
- the Excel list of transactions finanzen.net zero sends on request (one file per securities account, columns
  "BELEG-D", "WPGARTB-D", "WPNRID-D", ...).

Both are turned into one canonical CSV that pp-core reads with PP's own CSV importer (see PdfImporter.extract) and
then checks like PDF documents – possible duplicates of transactions already in the PP file are not imported
without a decision. Only securities account transactions (buy, sell, deliveries, transfers) – no dividends or
deposits: those are cash account transactions with other columns.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

from ..i18n import gettext as _

SUFFIXES = (".csv", ".xlsx")
CANONICAL = ("date", "time", "type", "isin", "wkn", "name", "shares", "value", "fees", "taxes", "currency", "note")
MAX_ROWS = 50000


class CsvImportError(ValueError):
    pass


@dataclass
class Row:
    day: date
    time: str  # HH:MM:SS or ""
    type: str  # PortfolioTransaction.Type
    isin: str
    wkn: str
    name: str
    shares: Decimal
    value: Decimal
    fees: Decimal
    taxes: Decimal
    currency: str
    note: str


def is_table(name: str) -> bool:
    return (name or "").lower().endswith(SUFFIXES)


# ------------------------------------------------------------------------------------------ common

_COLUMNS = {
    "date": ("datum", "date", "handelstag", "buchungstag", "schlusstag", "budatd"),
    "time": ("uhrzeit", "zeit", "time", "buzeitd"),
    "type": ("typ", "type", "art", "buchungsart", "transaktion", "wpgartbd"),
    "isin": ("isin", "wpnridd"),
    "wkn": ("wkn", "wpnred"),
    "name": ("wertpapiername", "wertpapier", "name", "securityname", "bezeichnung", "wpbezkd"),
    "shares": ("stuck", "stueck", "anzahl", "shares", "quantity", "nwd"),
    "value": ("wert", "betrag", "value", "amount", "gesamtbetrag"),
    "fees": ("gebuhren", "gebuehren", "fees", "geabd"),
    "taxes": ("steuern", "taxes"),
    "currency": ("buchungswahrung", "wahrung", "transactioncurrency", "currency", "whgabd"),
    "note": ("notiz", "note", "bemerkung", "kommentar"),
}

_TYPES = {
    "BUY": ("kauf", "buy", "kauf umtausch ca"),
    "SELL": ("verkauf", "sell", "verkauf umtausch ca"),
    "DELIVERY_INBOUND": ("einlieferung", "delivery inbound", "delivery (inbound)", "einbuchung"),
    "DELIVERY_OUTBOUND": ("auslieferung", "delivery outbound", "delivery (outbound)", "ausbuchung",
                          "depotumbuchung ausl"),
    "TRANSFER_IN": ("umbuchung eingang", "umbuchung (eingang)", "transfer inbound", "transfer (inbound)"),
    "TRANSFER_OUT": ("umbuchung ausgang", "umbuchung (ausgang)", "transfer outbound", "transfer (outbound)"),
}
_TYPE_OF = {alias: code for code, aliases in _TYPES.items() for alias in aliases + (code.lower(),)}


def _key(text) -> str:
    """'Stück' -> 'stuck', 'WPGARTB-D' -> 'wpgartbd'."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", text)


def _type(text: str) -> str | None:
    t = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or "")).strip().lower()).rstrip(".")
    return _TYPE_OF.get(t) or _TYPE_OF.get(t.replace("(", "").replace(")", ""))


def _number(text, comma_decimal: bool | None) -> Decimal | None:
    if text is None:
        return None
    if isinstance(text, (int, float, Decimal)):
        return Decimal(str(text))
    s = str(text).strip().replace(" ", "").replace(" ", "").replace("€", "").replace("EUR", "")
    if not s or s in ("-", "–"):
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif "." in s and comma_decimal and re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")  # 1.234 in a file with decimal commas
    try:
        return Decimal(s)
    except InvalidOperation:
        raise CsvImportError(_("„{a0}“ ist keine Zahl", a0=text)) from None


def _day(text) -> date:
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    s = str(text or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y", "%Y/%m/%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise CsvImportError(_("„{a0}“ ist kein Datum", a0=text))


def _time(text) -> str:
    s = str(text or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    return f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3) or '00'}" if m else ""


def to_canonical(rows: list[Row]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\n")
    w.writerow(CANONICAL)
    for r in rows:
        w.writerow([r.day.isoformat(), r.time, r.type, r.isin, r.wkn, r.name, _plain(r.shares), _plain(r.value, 2),
                    _plain(r.fees, 2), _plain(r.taxes, 2), r.currency, r.note])
    return out.getvalue().encode("utf-8")


def _plain(d: Decimal, places: int | None = None) -> str:
    if places is not None:
        return f"{d:.{places}f}"
    s = format(d.normalize(), "f")
    return s


# ------------------------------------------------------------------------------------------ CSV

def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def read_csv(data: bytes) -> list[Row]:
    text = _decode(data)
    first = text.split("\n", 1)[0]
    delimiter = max((";", ",", "\t"), key=first.count)
    records = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    records = [r for r in records if any(c.strip() for c in r)]
    if len(records) < 2:
        raise CsvImportError(_("keine Zeilen"))
    header = [_key(h) for h in records[0]]
    col = {}
    for field, names in _COLUMNS.items():
        for i, h in enumerate(header):
            if h in names and i not in col.values():
                col[field] = i
                break
    missing = [f for f in ("date", "type", "shares", "value") if f not in col]
    if missing or not ({"isin", "wkn", "name"} & col.keys()):
        raise CsvImportError(_("Spalten nicht erkannt (nötig: Datum, Typ, Stück, Wert und ISIN, WKN oder Wertpapiername; gefunden: {a0})", a0=", ".join(records[0])))
    body = records[1:]
    if len(body) > MAX_ROWS:
        raise CsvImportError(_("mehr als {a0} Zeilen", a0=MAX_ROWS))
    numeric = [r[col[f]] for r in body for f in ("shares", "value", "fees", "taxes") if f in col and col[f] < len(r)]
    comma_decimal = any(re.search(r",\d{1,2}$", v.strip()) for v in numeric) or delimiter == ";"

    def get(r, f):
        i = col.get(f)
        return r[i].strip() if i is not None and i < len(r) else ""

    rows, problems = [], []
    for n, r in enumerate(body, start=2):
        try:
            kind = _type(get(r, "type"))
            if kind is None:
                raise CsvImportError(_("Typ „{a0}“ ist kein Depotumsatz (Kauf, Verkauf, Ein-/Auslieferung, Umbuchung)", a0=get(r, "type")))
            shares = abs(_number(get(r, "shares"), comma_decimal) or Decimal(0))
            if not shares:
                raise CsvImportError(_("keine Stückzahl"))
            rows.append(Row(_day(get(r, "date")), _time(get(r, "time")), kind, get(r, "isin").upper(), get(r, "wkn"),
                            get(r, "name"), shares, abs(_number(get(r, "value"), comma_decimal) or Decimal(0)),
                            abs(_number(get(r, "fees"), comma_decimal) or Decimal(0)),
                            abs(_number(get(r, "taxes"), comma_decimal) or Decimal(0)),
                            (get(r, "currency") or "EUR").upper()[:3], get(r, "note")))
        except CsvImportError as exc:
            problems.append(_("Zeile {a0}: {a1}", a0=n, a1=exc))
    if problems:
        raise CsvImportError("; ".join(problems[:5]) + (_(" (und {a0} weitere)", a0=len(problems) - 5)
                                                         if len(problems) > 5 else ""))
    return rows


# ------------------------------------------------------------------------------------------ Excel (finanzen.net zero)

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_EPOCH = datetime(1899, 12, 30)


def _xlsx_rows(data: bytes, limit: int = MAX_ROWS + 100) -> list[list]:
    """Values of the first worksheet (numbers as float, text as str) – enough for a plain list export."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise CsvImportError(_("keine Excel-Datei (.xlsx)")) from None
    with zf:
        names = set(zf.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        if not sheets:
            raise CsvImportError(_("keine Tabelle in der Excel-Datei"))
        root = ElementTree.fromstring(zf.read(sheets[0]))
    rows = []
    for row in root.iter(f"{{{_NS['m']}}}row"):
        values = {}
        for c in row.findall("m:c", _NS):
            ref, kind = c.get("r", ""), c.get("t", "n")
            letters = re.match(r"[A-Z]+", ref)
            if not letters:
                continue
            idx = 0
            for ch in letters.group(0):
                idx = idx * 26 + ord(ch) - 64
            v = c.find("m:v", _NS)
            if kind == "s" and v is not None:
                val = shared[int(v.text)]
            elif kind == "inlineStr":
                val = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
            elif kind in ("str", "e") and v is not None:
                val = v.text or ""
            elif v is not None and v.text not in (None, ""):
                try:
                    val = float(v.text)
                except ValueError:
                    val = v.text
            else:
                continue
            values[idx - 1] = val
        if values:
            rows.append([values.get(i) for i in range(max(values) + 1)])
        if len(rows) > limit:
            raise CsvImportError(_("mehr als {a0} Zeilen", a0=MAX_ROWS))
    return rows


def _text(v) -> str:
    """Cell text; whole numbers without '.0' (e.g. a WKN or document number stored as a number)."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v if v is not None else "").strip()


def _excel_day(v) -> date:
    if isinstance(v, float):
        return (_EPOCH + timedelta(days=v)).date()
    return _day(v)


def _excel_time(v) -> str:
    if isinstance(v, float):
        seconds = round((v % 1) * 86400)
        return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    return _time(v)


_ZERO_TYPES = {"kauf": "BUY", "verkauf": "SELL", "einlieferung": "DELIVERY_INBOUND",
               "depotumbuchung ausl.": "DELIVERY_OUTBOUND", "kauf umtausch ca": "DELIVERY_INBOUND",
               "verkauf umtausch ca": "DELIVERY_OUTBOUND", "auslieferung": "DELIVERY_OUTBOUND"}


def read_zero_xlsx(data: bytes) -> list[Row]:
    """finanzen.net zero's list of securities account transactions (Excel, on request from the bank).

    Columns used: BELEG-D (document number), WPGARTB-D (kind), NW-D (shares, negative = out), WPNRID-D (ISIN/currency),
    WPBEZK-D (name), WPNRE-D (WKN), BUDAT-D + BUZEIT-D (trade day and time), BETRAB-D (amount booked), KWAB-D
    (market value), GEAB-D (fees), WHGAB-D (currency), BOKZBEZ-D (trading venue), SNR-D (securities account).
    Taxes of a sale are the market value minus the amount credited (minus fees). Exchanges of a corporate action
    ("Umtausch CA", booked with amount 0) become deliveries at market value."""
    table = _xlsx_rows(data)
    head_at = next((i for i, r in enumerate(table) if r and "BELEG-D" in [str(c or "").strip() for c in r]), None)
    if head_at is None:
        raise CsvImportError(_("keine Umsatzliste von finanzen.net zero (Spalte „BELEG-D“ fehlt)"))
    header = [str(c or "").strip() for c in table[head_at]]
    ix = {h: i for i, h in enumerate(header) if h}
    need = ("BELEG-D", "WPGARTB-D", "NW-D", "WPNRID-D", "BUDAT-D", "BETRAB-D", "KWAB-D")
    missing = [h for h in need if h not in ix]
    if missing:
        raise CsvImportError(_("Spalten fehlen: {a0}", a0=", ".join(missing)))

    def get(r, h):
        i = ix.get(h)
        return r[i] if i is not None and i < len(r) else None

    rows, problems = [], []
    for n, r in enumerate(table[head_at + 1:], start=head_at + 2):
        if get(r, "BELEG-D") in (None, ""):
            continue  # totals and empty lines
        kind_text = str(get(r, "WPGARTB-D") or "").strip()
        kind = _ZERO_TYPES.get(kind_text.lower())
        try:
            if kind is None:
                raise CsvImportError(_("unbekannte Art „{a0}“", a0=kind_text))
            shares = abs(_number(get(r, "NW-D"), False) or Decimal(0))
            if not shares:
                raise CsvImportError(_("keine Stückzahl"))
            booked = abs(_number(get(r, "BETRAB-D"), False) or Decimal(0))
            market = abs(_number(get(r, "KWAB-D"), False) or Decimal(0))
            fees = abs(_number(get(r, "GEAB-D"), False) or Decimal(0))
            taxes = Decimal(0)
            if kind == "BUY":
                value = booked
            elif kind == "SELL":
                value = booked
                taxes = max(Decimal(0), (market - booked - fees).quantize(Decimal("0.01")))
            else:
                value = market
            isin = str(get(r, "WPNRID-D") or "").split("/")[0].strip().upper()
            beleg, account = _text(get(r, "BELEG-D")), _text(get(r, "SNR-D"))
            venue = str(get(r, "BOKZBEZ-D") or "").strip()
            note = ", ".join(p for p in (f"finanzen.net zero Depot {account}" if account else "", f"Beleg {beleg}",
                                         kind_text, venue) if p)
            rows.append(Row(_excel_day(get(r, "BUDAT-D")), _excel_time(get(r, "BUZEIT-D")), kind, isin,
                            _text(get(r, "WPNRE-D")), _text(get(r, "WPBEZK-D")), shares,
                            value, fees, taxes, str(get(r, "WHGAB-D") or "EUR").strip().upper()[:3] or "EUR", note))
        except CsvImportError as exc:
            problems.append(_("Zeile {a0}: {a1}", a0=n, a1=exc))
    if problems:
        raise CsvImportError("; ".join(problems[:5]) + (_(" (und {a0} weitere)", a0=len(problems) - 5)
                                                         if len(problems) > 5 else ""))
    if not rows:
        raise CsvImportError(_("keine Umsätze"))
    return rows


def convert(name: str, data: bytes) -> tuple[bytes, int]:
    """(canonical CSV for pp-core, number of transactions) of an uploaded .csv or .xlsx file."""
    rows = read_zero_xlsx(data) if name.lower().endswith(".xlsx") else read_csv(data)
    rows.sort(key=lambda r: (r.day, r.time))
    return to_canonical(rows), len(rows)
