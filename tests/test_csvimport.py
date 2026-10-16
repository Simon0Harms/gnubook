"""CSV and Excel lists of securities account transactions for the PDF import page (gnubook.pp.csvimport)."""
import io
import zipfile
from datetime import date
from decimal import Decimal

import pytest

from gnubook.pp import csvimport
from gnubook.pp.csvimport import CsvImportError


def test_pp_style_csv_in_english_with_decimal_points():
    data = ("Date,Type,Security Name,ISIN,Shares,Value,Fees,Taxes,Transaction Currency\n"
            "2024-12-31,Buy,Some ETF,ie00b4l5y983,1.5,\"1,234.56\",1.00,0,EUR\n"
            "2025-01-02,Delivery (Outbound),Some ETF,IE00B4L5Y983,1.5,1300,,,EUR\n").encode()
    rows = csvimport.read_csv(data)
    assert [r.type for r in rows] == ["BUY", "DELIVERY_OUTBOUND"]
    assert rows[0].isin == "IE00B4L5Y983" and rows[0].value == Decimal("1234.56") and rows[0].shares == Decimal("1.5")
    assert rows[0].day == date(2024, 12, 31) and rows[1].fees == 0


def test_errors_name_the_line():
    data = "Datum;Typ;Wertpapiername;Stück;Wert\n01.02.2024;Kauf;X;1;10\n31.02.2024;Kauf;X;1;10\n" \
           "01.03.2024;Dividende;X;1;10\n".encode()
    with pytest.raises(CsvImportError) as e:
        csvimport.read_csv(data)
    assert "Zeile 3" in str(e.value) and "Zeile 4" in str(e.value) and "Dividende" in str(e.value)


def _xlsx(rows):
    """A minimal workbook like the bank's (inline strings, numbers, dates as serials)."""
    def cell(ref, v):
        if isinstance(v, str):
            return f'<c r="{ref}" t="inlineStr"><is><t>{v}</t></is></c>'
        return f'<c r="{ref}"><v>{v}</v></c>'
    xml_rows = []
    for i, r in enumerate(rows, 1):
        cells = "".join(cell(f"{chr(65 + j)}{i}", v) for j, v in enumerate(r) if v is not None)
        xml_rows.append(f'<row r="{i}">{cells}</row>')
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             + "".join(xml_rows) + "</sheetData></worksheet>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


HEAD = ["AUSW", "BELEG-D", "WPGARTB-D", "DENR-D", "WHGNW-D", "NW-D", "WPNRID-D", "WPBEZK-D", "VALUTA-D", "BUDAT-D",
        "BUZEIT-D", "WPKURS-D", "SNR-D", "WPNRE-D", "WHGAB-D", "BETRAB-D", "KURKWAB-D", "KWAB-D", "GEAB-D", "STATB-D",
        "BOKZ-D", "BOKZBEZ-D"]


def test_finanzen_zero_excel_list():
    day = 44470.0  # 2021-10-01
    data = _xlsx([["BELEGU36", "MEIE24"], ["Grid1"], HEAD,
                  [None, 56555526, "Kauf", 1, "STK", 8, "IE00BJ0KDQ92/EUR", "X(IE)-MSCI WORLD 1C", day + 3, day,
                   0.6721875, 78.898, 1948974, "A1XB5U", "EUR", -631.18, None, 631.18, 0, "TOT", "GDM", "GETTEX"],
                  [None, 338894412, "Verkauf", 1, "STK", -6, "US0079031078/EUR", "ADVANCED MIC.DEV.", day, day,
                   0.5, 167.5, 1948974, 863186, "EUR", 1003.09, None, -1005, 0, "TOT", "GDM", "GETTEX"],
                  [None, 181202843, "Verkauf Umtausch CA", 1, "STK", -2, "LU0635178014/EUR", "LYX MSCI EM", day, day,
                   0.0, 45.385, 1948974, "ETF127", "EUR", 0, None, -90.77, 0, "TOT", "GDM", "GETTEX"],
                  [None, None, "Total"]])
    rows = csvimport.read_zero_xlsx(data)
    buy, sell, out = rows
    assert (buy.type, buy.day, buy.time, buy.shares, buy.value, buy.taxes) == \
           ("BUY", date(2021, 10, 1), "16:07:57", 8, Decimal("631.18"), 0)
    assert (sell.type, sell.wkn, sell.value, sell.taxes) == ("SELL", "863186", Decimal("1003.09"), Decimal("1.91"))
    assert (out.type, out.value) == ("DELIVERY_OUTBOUND", Decimal("90.77"))
    assert "Depot 1948974, Beleg 56555526, Kauf, GETTEX" == buy.note.replace("finanzen.net zero ", "")
    canonical, n = csvimport.convert("1948974.xlsx", data)
    assert n == 3 and canonical.decode().splitlines()[0] == ";".join(csvimport.CANONICAL)


def test_other_excel_is_refused():
    with pytest.raises(CsvImportError, match="BELEG-D"):
        csvimport.read_zero_xlsx(_xlsx([["Datum", "Betrag"], ["x", 1]]))
    with pytest.raises(CsvImportError, match="keine Excel-Datei"):
        csvimport.read_zero_xlsx(b"not a zip")


def test_finanzen_zero_excel_other_kinds_and_marked_days():
    data = _xlsx([HEAD,
                  [None, 1, "Depotumbuchung Einl.", 1, "STK", 221, "IE00BJ0KDQ92/EUR", "MSCI WORLD", 45146.0, 45146.0,
                   0.3, 84.718, 1483210, "A1XB5U", "EUR", -18722.68, None, 18722.68, 0, "TOT", "EDE", "XETRA"],
                  [None, 2, "Kapitalkauf CA", 1, "STK", 1.2, "FR0000120073", "AIR LIQUIDE", 45453.0, 45453.0,
                   0.0, 0, 1483210, None, "EUR", 0, None, 0, 0, "TOT", "GDM", "GETTEX"],
                  [None, 3, "Kauf", 1, "STK", 4, "DE0008402215", "HANNOVER RUECK", "!13.10.2025", "!09.10.2025",
                   0.5, 266, 1483210, 840221, "EUR", -1064, None, 1064, 0, "TOT", "GDM", "GETTEX"],
                  [None, 4, "Sonstiges CA", 1, "STK", -2, "DE000TUAG000", "TUI", 45000.0, 45000.0,
                   0.0, 0, 1483210, None, "EUR", 0, None, 0, 0, "TOT", "GDM", "GETTEX"]])
    inbound, bonus, buy, other = csvimport.read_zero_xlsx(data)
    assert (inbound.type, inbound.value, inbound.day) == ("DELIVERY_INBOUND", Decimal("18722.68"), date(2023, 8, 8))
    assert (bonus.type, bonus.shares, bonus.value) == ("DELIVERY_INBOUND", Decimal("1.2"), 0)
    assert (buy.type, buy.day, buy.wkn) == ("BUY", date(2025, 10, 9), "840221")
    assert other.type == "DELIVERY_OUTBOUND"  # an unknown corporate action without cash: by the sign of the shares
