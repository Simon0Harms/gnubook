"""Synthetic pp-core exports and a fake pp-core client for the Portfolio Performance tests (no Java needed)."""
from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest

P1 = "11111111-1111-4111-8111-111111111111"   # Depot Musterbank
P2 = "22222222-2222-4222-8222-222222222222"   # Depot Beispielbank
A1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"   # Verrechnungskonto (EUR)
A2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"   # USD account
ETF = "e7f00000-0000-4000-8000-000000000001"
SHARE = "e7f00000-0000-4000-8000-000000000002"
US = "e7f00000-0000-4000-8000-000000000003"


def _t(uuid, kind, owner, typ, day, amount, *, shares=None, security=None, units=(), cross=None, currency="EUR",
       note=None, source=None):
    return {"uuid": uuid, "kind": kind, "owner": owner, "type": typ, "date": f"{day}T00:00", "currency": currency,
            "amount": str(amount), "shares": None if shares is None else str(shares), "security": security,
            "note": note, "source": source, "updatedAt": None, "exDate": None,
            "units": [{"type": t, "amount": str(a), "currency": currency} for t, a in units], "cross": cross}


def _buysell(uuid, typ, day, port, acc, sec, shares, amount, units=(), note=None, source=None, currency="EUR"):
    pu, au = f"{uuid}-p", f"{uuid}-a"
    return [
        _t(pu, "portfolio", port, typ, day, amount, shares=shares, security=sec, units=units, currency=currency,
           cross={"type": "buysell", "uuid": au, "owner": acc}, note=note, source=source),
        _t(au, "account", acc, typ, day, amount, security=sec, currency=currency,
           cross={"type": "buysell", "uuid": pu, "owner": port}),
    ]


def prices(start: date, days: int, value: float, step: float = 0.1):
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        if d.weekday() < 5:
            v = f"{value + i * step:.2f}"
            out.append([d.isoformat(), v, v])
    return out


def make_export(revision: str = "rev-1", with_prices: bool = True) -> dict:
    """A small but complete PP file: two securities accounts, EUR and USD cash accounts, every transaction type."""
    today = date.today()
    sec = [
        {"uuid": ETF, "name": "Musterwelt Aktien ETF", "currency": "EUR", "targetCurrency": None,
         "isin": "DE000MUSTER1", "wkn": "MUST01", "ticker": "MWE.DE", "feed": "YAHOO", "feedUrl": None,
         "latestFeed": None, "latestFeedUrl": None, "retired": False, "exchangeRate": False, "note": None,
         "calendar": None, "latest": None, "prices": prices(date(2024, 1, 1), 40, 100.0) if with_prices else []},
        {"uuid": SHARE, "name": "Muster: Industrie AG", "currency": "EUR", "targetCurrency": None,
         "isin": "DE000MUSTER2", "wkn": None, "ticker": None, "feed": None, "feedUrl": None, "latestFeed": None,
         "latestFeedUrl": None, "retired": False, "exchangeRate": False, "note": None, "calendar": None,
         "latest": {"date": (today - timedelta(days=1)).isoformat(), "value": "12.5"},
         "prices": prices(today - timedelta(days=30), 30, 10.0) if with_prices else []},
        {"uuid": US, "name": "Example Corp.", "currency": "USD", "targetCurrency": None, "isin": "US0000000001",
         "wkn": None, "ticker": "EXMP", "feed": "YAHOO", "feedUrl": None, "latestFeed": None, "latestFeedUrl": None,
         "retired": False, "exchangeRate": False, "note": None, "calendar": None, "latest": None, "prices": []},
    ]
    tx = []
    tx.append(_t("dep-1", "account", A1, "DEPOSIT", "2024-01-02", "10000"))
    tx += _buysell("buy-1", "BUY", "2024-01-03", P1, A1, ETF, "10", "1001.50", [("FEE", "1.50")],
                   note="Sparplan", source="Kauf_2024-01-03.pdf")
    tx += _buysell("buy-2", "BUY", "2024-01-10", P1, A1, ETF, "5", "600")
    tx += _buysell("sell-1", "SELL", "2024-01-20", P1, A1, ETF, "12", "1500", [("FEE", "2"), ("TAX", "48")])
    tx.append(_t("div-1", "account", A1, "DIVIDENDS", "2024-01-25", "20", shares="3", security=ETF,
                 units=[("TAX", "5")]))
    tx.append(_t("int-1", "account", A1, "INTEREST", "2024-01-31", "3"))
    tx.append(_t("fee-1", "account", A1, "FEES", "2024-02-01", "10", security=ETF))
    tx.append(_t("tax-1", "account", A1, "TAXES", "2024-02-02", "4"))
    tx.append(_t("taxr-1", "account", A1, "TAX_REFUND", "2024-02-03", "2"))
    tx.append(_t("feer-1", "account", A1, "FEES_REFUND", "2024-02-04", "1"))
    tx.append(_t("intc-1", "account", A1, "INTEREST_CHARGE", "2024-02-05", "0.50"))
    tx.append(_t("tr-out", "portfolio", P1, "TRANSFER_OUT", "2024-02-06", "390", shares="3", security=ETF,
                 cross={"type": "portfolio-transfer", "uuid": "tr-in", "owner": P2}))
    tx.append(_t("tr-in", "portfolio", P2, "TRANSFER_IN", "2024-02-06", "390", shares="3", security=ETF,
                 cross={"type": "portfolio-transfer", "uuid": "tr-out", "owner": P1}))
    tx.append(_t("del-in", "portfolio", P2, "DELIVERY_INBOUND", "2024-02-07", "500", shares="50", security=SHARE))
    tx.append(_t("del-out", "portfolio", P2, "DELIVERY_OUTBOUND", "2024-02-08", "120", shares="10",
                 security=SHARE))
    tx += _buysell("buy-usd", "BUY", "2024-02-09", P2, A2, US, "2", "300", currency="USD")
    tx += _buysell("buy-3", "BUY", "2024-02-12", P2, A1, SHARE, "2.5", "31.25")  # fractional shares
    return {
        "client": {"id": "book-1", "baseCurrency": "EUR", "revision": revision, "file": "portfolio.xml",
                   "version": 70},
        "securities": sec,
        "accounts": [{"uuid": A1, "name": "Verrechnungskonto", "currency": "EUR", "retired": False, "note": None},
                     {"uuid": A2, "name": "Dollarkonto", "currency": "USD", "retired": False, "note": None}],
        "portfolios": [{"uuid": P1, "name": "Depot Musterbank", "referenceAccount": A1, "retired": False,
                        "note": None},
                       {"uuid": P2, "name": "Depot Beispielbank", "referenceAccount": A1, "retired": False,
                        "note": None}],
        "transactions": tx,
    }


def find(export: dict, uuid: str) -> dict:
    return next(t for t in export["transactions"] if t["uuid"] == uuid)


def without(export: dict, *uuids) -> dict:
    out = copy.deepcopy(export)
    out["transactions"] = [t for t in out["transactions"] if t["uuid"] not in uuids]
    return out


class FakePPCore:
    """Stands in for gnubook.pp.client.PPCoreClient."""

    def __init__(self, export: dict | None = None):
        self.export_data = export if export is not None else make_export()
        self.calls = []
        self.deleted = []
        self.uploaded = None
        self.quotes_state = "done"
        self.demo_marker = None

    # pp-core API ---------------------------------------------------------------------------------
    def health(self):
        return {"status": "ok", "ppVersion": "0.88.0", "service": "0.1.0", "clients": ["book-1"]}

    def feeds(self):
        return [{"id": "MANUAL", "name": "Manuell"}, {"id": "YAHOO", "name": "Yahoo Finance"}]

    def summary(self, cid):
        e = self.export_data
        return {"id": cid, "exists": bool(e), "file": "portfolio.xml", "size": 1234,
                "modified": "2026-10-01T10:00:00Z", "revision": e["client"]["revision"], "baseCurrency": "EUR",
                "securities": len(e["securities"]), "accounts": len(e["accounts"]),
                "portfolios": len(e["portfolios"]), "transactions": len(e["transactions"]),
                "firstDate": "2024-01-02",
                "portfolioList": [{"uuid": p["uuid"], "name": p["name"], "retired": False} for p in e["portfolios"]],
                "accountList": [{"uuid": a["uuid"], "name": a["name"], "currency": a["currency"], "retired": False}
                                for a in e["accounts"]],
                "originalName": "Depot.xml", "lastPriceUpdate": "2026-10-01T09:00:00Z", "importTargets": {},
                "demo": self.demo_marker}

    def demo(self, cid, start, months, seed=7):
        self.calls.append(("demo", cid, start, months, seed))
        self.demo_marker = f"{start.isoformat()}/{months}/{seed}"
        return self.summary(cid)

    def export(self, cid, prices="all"):
        self.calls.append(("export", prices))
        e = copy.deepcopy(self.export_data)
        if prices == "none":
            for s in e["securities"]:
                s["prices"] = []
        return e

    def performance(self, cid, start, end, portfolio=None):
        self.calls.append(("performance", start, end, portfolio))
        cat = lambda label, v: {"label": label, "value": {"amount": v, "currency": "EUR"}, "positions": []}  # noqa
        return {"currency": "EUR", "from": start.isoformat(), "to": end.isoformat(), "portfolio": portfolio,
                "ttwror": 0.0852, "ttwrorAnnualized": 0.041, "irr": 0.044,
                "initialValue": {"amount": "1000", "currency": "EUR"}, "finalValue": {"amount": "1100", "currency": "EUR"},
                "absoluteChange": {"amount": "100", "currency": "EUR"}, "delta": {"amount": "85.5", "currency": "EUR"},
                "maxDrawdown": 0.0327, "volatility": 0.057, "semiVolatility": 0.04,
                "categories": {"INITIAL_VALUE": cat("Anfangswert", "1000"), "CAPITAL_GAINS": cat("Kurserfolge", "80"),
                               "EARNINGS": {"label": "Erträge", "value": {"amount": "20", "currency": "EUR"},
                                            "positions": [{"label": "Musterwelt Aktien ETF", "security": ETF,
                                                           "value": {"amount": "20", "currency": "EUR"}}]},
                               "FEES": cat("Gebühren", "14.5"), "FINAL_VALUE": cat("Endwert", "1100")},
                "series": [["2024-01-02", 0.0, "1000", "1000"], ["2024-06-30", 0.04, "1050", "1000"],
                           ["2024-12-31", 0.0852, "1100", "1000"]],
                "warnings": []}

    def holdings(self, cid, day=None):
        return {"date": (day or date.today()).isoformat(), "currency": "EUR",
                "positions": [{"security": SHARE, "name": "Muster: Industrie AG", "isin": "DE000MUSTER2", "wkn": None,
                               "ticker": None, "currency": "EUR", "shares": "42.5", "price": "12.5",
                               "priceDate": "2026-10-08", "value": {"amount": "531.25", "currency": "EUR"},
                               "cost": {"amount": "431.25", "currency": "EUR"},
                               "unrealizedGain": {"amount": "100", "currency": "EUR"}, "unrealizedGainPercent": 0.23,
                               "realizedGain": {"amount": "0", "currency": "EUR"},
                               "dividends": {"amount": "0", "currency": "EUR"}, "irr": 0.1, "ttwror": 0.2}],
                "securitiesValue": {"amount": "531.25", "currency": "EUR"},
                "accounts": [{"uuid": A1, "name": "Verrechnungskonto", "balance": {"amount": "8000", "currency": "EUR"},
                              "value": {"amount": "8000", "currency": "EUR"}}],
                "cash": {"amount": "8000", "currency": "EUR"}, "total": {"amount": "8531.25", "currency": "EUR"},
                "portfolios": [{"uuid": P2, "name": "Depot Beispielbank", "value": {"amount": "531.25", "currency": "EUR"},
                                "positions": []}]}

    def quotes_status(self, cid):
        return {"job": "j1", "state": self.quotes_state, "started": "2026-10-01T09:00:00Z",
                "finished": "2026-10-01T09:01:00Z", "error": None, "modified": 1,
                "securities": [{"uuid": ETF, "name": "Musterwelt Aktien ETF", "feed": "YAHOO", "ticker": "MWE.DE",
                                "status": "updated", "message": None, "newPrices": 3, "lastPrice": "2024-02-08"}]}

    def quotes_start(self, cid, securities=(), wait=0):
        self.calls.append(("quotes", tuple(securities), wait))
        return {"job": "j2", "state": "running", "securities": [], "modified": 0}

    def search(self, cid, query):
        self.calls.append(("search", query))
        return [{"provider": "Yahoo", "name": "Musterwelt Aktien ETF", "symbol": "MWE.DE", "isin": "DE000MUSTER1",
                 "wkn": None, "type": "ETF", "exchange": "GER", "currency": "EUR", "feed": "YAHOO",
                 "source": "Yahoo", "needsLogin": False}]

    def update_security(self, cid, uuid, fields):
        self.calls.append(("update_security", uuid, dict(fields)))
        return {"uuid": uuid, **fields}

    def add_delivery(self, cid, fields):
        """Like pp-core: refuses to deliver more than 1000 shares out without force (stands for "more than held")."""
        from gnubook.pp.client import PPCoreError

        self.calls.append(("add_delivery", dict(fields)))
        if (fields["type"] == "DELIVERY_OUTBOUND" and float(fields["shares"]) > 1000
                and not fields.get("force")):
            raise PPCoreError("only 10 shares of Musterwelt Aktien ETF in Depot Musterbank", 409,
                              "not_enough_shares")
        uuid = f"manual-{len(self.calls)}"
        sec = fields.get("security") or ETF
        self.export_data = copy.deepcopy(self.export_data)
        self.export_data["transactions"].append(
            _t(uuid, "portfolio", fields.get("portfolio") or P1, fields["type"], fields["date"],
               fields.get("amount") or "100", shares=fields["shares"], security=sec, note=fields.get("note")))
        return {"uuid": uuid, "type": fields["type"], "security": sec, "shares": fields["shares"]}

    def delete_transaction(self, cid, uuid):
        self.deleted.append(uuid)
        self.export_data = without(self.export_data, uuid)
        return {"deleted": uuid}

    def upload(self, cid, content, filename):
        self.uploaded = (content, filename)
        return self.summary(cid)

    def download(self, cid):
        return b"<client/>", "portfolio.xml"

    def create(self, cid, currency="EUR", portfolio="Depot", account="Verrechnungskonto"):
        self.calls.append(("create", currency, portfolio, account))
        return self.summary(cid)

    def _import_result(self, imported=2, needs_target=False):
        return {"session": "s-1", "revision": "rev-2", "imported": imported, "needsTarget": needs_target,
                "fileErrors": [{"file": "kaputt.pdf", "message": "keine Buchungen erkannt"}],
                "targets": [{"extractor": "Musterbank", "portfolio": None, "accounts": {"EUR": None},
                             "missing": needs_target}],
                "items": [{"index": 0, "extractor": "Musterbank", "file": "Kauf.pdf", "kind": "BuySellEntryItem",
                           "type": "Kauf", "date": "2024-03-01T00:00",
                           "security": {"name": "Musterwelt Aktien ETF", "isin": "DE000MUSTER1", "wkn": None,
                                        "uuid": ETF},
                           "amount": {"amount": "100", "currency": "EUR"}, "shares": "1",
                           "fees": {"amount": "1", "currency": "EUR"}, "taxes": {"amount": "0", "currency": "EUR"},
                           "uuid": "x", "status": "OK", "messages": [], "imported": not needs_target, "error": None},
                          {"index": 1, "extractor": "Musterbank", "file": "Kauf2.pdf", "kind": "BuySellEntryItem",
                           "type": "Kauf", "date": "2024-03-02T00:00", "security": None,
                           "amount": {"amount": "50", "currency": "EUR"}, "shares": "0.5", "fees": None,
                           "taxes": None, "uuid": None, "status": "WARNING",
                           "messages": [{"code": "WARNING", "message": "Buchung existiert möglicherweise schon"}],
                           "imported": False, "error": None}],
                "newSecurities": [ETF], "portfolios": [{"uuid": P1, "name": "Depot Musterbank"}],
                "accounts": [{"uuid": A1, "name": "Verrechnungskonto", "currency": "EUR"}]}

    def import_pdfs(self, cid, files, portfolio=None, account=None, apply=True, auto_feed=True):
        self.calls.append(("import", [n for n, _d in files], portfolio, account, auto_feed))
        return self._import_result()

    def import_session(self, cid, session):
        return self._import_result()

    def import_apply(self, cid, session, force=(), portfolio=None, account=None, extractor=None, auto_feed=True):
        self.calls.append(("apply", session, list(force), portfolio, account, extractor))
        return self._import_result(imported=3)


# ------------------------------------------------------------------------------------------ fixtures

@pytest.fixture
def fake(monkeypatch):
    """pp-core replaced by FakePPCore; background sync requests are only recorded (fake.requests)."""
    f = FakePPCore()
    monkeypatch.setattr("gnubook.pp.service.PPCoreClient", lambda *a, **k: f)
    monkeypatch.setattr("gnubook.pp.client.PPCoreClient", lambda *a, **k: f)
    f.requests = []
    monkeypatch.setattr("gnubook.pp.service.SyncWorker.request",
                        lambda self, after_quotes=False: f.requests.append(after_quotes))
    return f


@pytest.fixture
def pp_app(cfg, fake):
    from gnubook import create_app

    cfg.pp.url = "http://pp-core.test"
    cfg.pp.token = "t" * 30
    application = create_app(cfg)
    application.testing = True
    yield application
    application.extensions["gnubook"].dispose()
