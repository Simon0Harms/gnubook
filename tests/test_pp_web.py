"""Portfolio Performance pages (pp-core replaced by a fake)."""
import io
import re
from pathlib import Path

import pytest

from gnubook.pp.client import PPCoreError

from .conftest import PASSWORD, csrf_from
from .pp_fixtures import ETF, P1, fake, find, make_export, pp_app  # noqa: F401 – fixtures


@pytest.fixture
def web(pp_app):
    c = pp_app.test_client()
    page = c.get("/login")
    r = c.post("/login", data={"username": "tester", "password": PASSWORD, "csrf_token": csrf_from(page.text)})
    assert r.status_code == 302
    c.csrf = csrf_from(c.get("/").text)
    return c


def _state(pp_app):
    reg = pp_app.extensions["gnubook"]
    return reg.context(reg.system.books()[0]["id"])


def test_no_menu_without_configuration(client):
    assert "Portfolio Performance" not in client.get("/").text
    assert client.get("/pp/").status_code == 404


@pytest.mark.parametrize("lang", ["de", "en"])
def test_all_pages_render(web, lang):
    if lang == "en":
        web.post("/language/en", data={"csrf_token": web.csrf, "next": "/"})
    assert "/pp/" in web.get("/").text
    for url in ["/pp/", "/pp/?period=all", "/pp/?period=custom&from=2024-01-01&to=2024-06-30", "/pp/holdings",
                "/pp/holdings?date=2024-02-01", "/pp/transactions", "/pp/transactions?status=attention&q=muster",
                "/pp/import", "/pp/delivery", "/pp/securities", "/pp/securities?all=1", f"/pp/securities/{ETF}",
                f"/pp/securities/{ETF}?q=MWE", "/pp/settings"]:
        r = web.get(url)
        assert r.status_code == 200, url
        assert f'<html lang="{lang}"' in r.text
    page = web.get("/pp/").text
    assert ("TTWROR" in page) and ("+8,52 %" in page or "+8.52 %" in page)


def test_settings_and_manual_sync(web, pp_app, fake):
    r = web.post("/pp/settings", data={"csrf_token": web.csrf, "enabled": "1", "realized_gains": "1", "prices": "1",
                                       "price_days": "30", "namespace": "Depot",
                                       "bank_accounts": ["Aktiva:Barvermögen:Girokonto Beispielbank"]})
    assert r.status_code == 302
    st = _state(pp_app)
    s = st.pp.settings()
    assert s.enabled and s.price_days == 30 and s.namespace == "Depot"
    assert s.bank_accounts == ["Aktiva:Barvermögen:Girokonto Beispielbank"]
    assert fake.requests  # background run requested
    r = web.post("/pp/sync", data={"csrf_token": web.csrf, "dry": "1", "next": "/pp/settings"}, follow_redirects=True)
    assert "14" in r.text and not st.appdb.pp_records()
    r = web.post("/pp/sync", data={"csrf_token": web.csrf, "next": "/pp/settings"}, follow_redirects=True)
    assert "14 neu" in r.text
    assert len(st.appdb.pp_records()) == 14
    page = web.get("/pp/transactions").text
    assert page.count("übernommen</span>") >= 14 and "Buchung #" in page
    assert "Wertpapier-Verrechnung" in web.get("/pp/").text or web.get("/pp/").status_code == 200


def test_settings_reject_placeholder_accounts(web, pp_app):
    r = web.post("/pp/settings", data={"csrf_token": web.csrf, "enabled": "1",
                                       "role_fees": "Aufwendungen:Wohnen"}, follow_redirects=True)
    assert "Platzhalterkonto" in r.text
    assert not _state(pp_app).pp.settings().enabled


def test_conflict_can_be_resolved_in_the_web(web, pp_app, fake):
    from sqlalchemy import text

    web.post("/pp/settings", data={"csrf_token": web.csrf, "enabled": "1", "realized_gains": "1"})
    web.post("/pp/sync", data={"csrf_token": web.csrf})
    st = _state(pp_app)
    guid = st.appdb.pp_records()["int-1"]["tx_guid"]
    with st.book.engine.begin() as conn:
        conn.execute(text("UPDATE transactions SET description = 'anders' WHERE guid = :g"), {"g": guid})
    e = make_export(revision="r2")
    find(e, "int-1")["amount"] = "4"
    fake.export_data = e
    web.post("/pp/sync", data={"csrf_token": web.csrf})
    assert st.appdb.pp_records()["int-1"]["status"] == "conflict"
    page = web.get("/pp/transactions?status=attention").text
    assert "Konflikt" in page and "PP übernehmen" in page
    r = web.post("/pp/sync/resolve", data={"csrf_token": web.csrf, "key": "int-1", "action": "force"})
    assert r.status_code == 302
    assert st.appdb.pp_records()["int-1"]["status"] == "booked"
    web.post("/pp/sync/resolve", data={"csrf_token": web.csrf, "key": "int-1", "action": "detach"})
    assert st.appdb.pp_records()["int-1"]["status"] == "detached"
    web.post("/pp/sync/resolve", data={"csrf_token": web.csrf, "key": "int-1", "action": "attach"})
    assert st.appdb.pp_records()["int-1"]["status"] == "booked"


def test_only_one_sync_per_book_at_a_time(web, pp_app, cfg):
    import fcntl

    from gnubook.pp.service import SyncBusy

    web.post("/pp/settings", data={"csrf_token": web.csrf, "enabled": "1"})
    st = _state(pp_app)
    # e.g. the timer's `gnubook pp-update` in another process
    with open(Path(cfg.app.data_dir) / f"pp-sync-{st.id}.lock", "a") as other:
        fcntl.flock(other, fcntl.LOCK_EX)
        with pytest.raises(SyncBusy):
            st.pp.sync(wait=0.3)
        assert not st.appdb.pp_records()
    assert st.pp.sync().created == 14


def test_pdf_import(web, fake):
    r = web.post("/pp/import", data={"csrf_token": web.csrf, "files": (io.BytesIO(b"%PDF-1.4 x"), "Kauf.pdf"),
                                     "auto_feed": "1"}, content_type="multipart/form-data")
    assert r.status_code == 302 and "session=s-1" in r.headers["Location"]
    assert fake.calls[-1][:2] == ("import", ["Kauf.pdf"])
    page = web.get(r.headers["Location"]).text
    assert "Musterwelt Aktien ETF" in page and "kaputt.pdf" in page and 'name="force" value="1"' in page
    r = web.post("/pp/import/s-1/apply", data={"csrf_token": web.csrf, "force": ["1"]})
    assert r.status_code == 302
    assert fake.calls[-1] == ("apply", "s-1", ["1"], None, None, None)


def test_import_needs_files(web):
    r = web.post("/pp/import", data={"csrf_token": web.csrf}, content_type="multipart/form-data",
                 follow_redirects=True)
    assert "mindestens eine PDF-Datei oder ein Archiv" in r.text


def test_securities_quotes_and_delete(web, fake):
    # without an active link quotes are loaded, but nothing is booked
    web.post("/pp/quotes", data={"csrf_token": web.csrf})
    assert ("quotes", (), 0) in fake.calls and fake.requests == []
    web.post("/pp/settings", data={"csrf_token": web.csrf, "enabled": "1"})
    fake.requests.clear()
    fake.calls.clear()
    r = web.post(f"/pp/securities/{ETF}", data={"csrf_token": web.csrf, "feed": "YAHOO", "ticker": "MWE.DE",
                                                "latestFeed": "", "update": "1"})
    assert r.status_code == 302
    assert ("update_security", ETF, {"feed": "YAHOO", "ticker": "MWE.DE", "latestFeed": ""}) in fake.calls
    assert ("quotes", (ETF,), 0) in fake.calls and fake.requests == [True]
    r = web.post("/pp/quotes", data={"csrf_token": web.csrf})
    assert r.status_code == 302 and ("quotes", (), 0) in fake.calls
    r = web.post("/pp/transactions/fee-1/delete", data={"csrf_token": web.csrf})
    assert r.status_code == 302 and fake.deleted == ["fee-1"]


def test_file_upload_download_and_create(web, fake):
    r = web.post("/pp/file", data={"csrf_token": web.csrf, "file": (io.BytesIO(b"<client/>"), "Depot.xml")},
                 content_type="multipart/form-data", follow_redirects=True)
    assert fake.uploaded == (b"<client/>", "Depot.xml") and "PP-Datei übernommen" in r.text
    r = web.get("/pp/file")
    assert r.status_code == 200 and r.data == b"<client/>" and "Depot.xml" in r.headers["Content-Disposition"]
    web.post("/pp/file/create", data={"csrf_token": web.csrf, "portfolio": "Depot", "account": "Konto"})
    assert ("create", "EUR", "Depot", "Konto") in fake.calls


def test_pp_core_down_shows_a_hint(web, fake, monkeypatch):
    def boom(*a, **k):
        raise PPCoreError("pp-core nicht erreichbar (http://pp-core.test): Connection refused")
    monkeypatch.setattr(fake, "summary", boom)
    r = web.get("/pp/")
    assert r.status_code == 503 and "systemctl status gnubook-ppcore" in r.text


def test_demo_users_see_a_fictional_file_read_only(pp_app, fake):
    from datetime import date

    from gnubook.demo import demo_period

    c = pp_app.test_client()
    page = c.get("/login")
    r = c.post("/demo", data={"csrf_token": csrf_from(page.text)})
    assert r.status_code == 302
    home = c.get("/").text
    assert 'href="/pp/"' in home
    csrf = csrf_from(home)
    start, months = demo_period(date.today())
    for url in ["/pp/", "/pp/holdings", "/pp/transactions", "/pp/securities", f"/pp/securities/{ETF}?q=MWE"]:
        r = c.get(url)
        assert r.status_code == 200, url
        assert "erfundene Portfolio-Performance-Datei" in r.text
        assert 'action="/pp/quotes"' not in r.text and 'href="/pp/settings"' not in r.text, url
        assert not re.search(r'action="/pp/(transactions/[^"]+/delete|sync|securities/[^"?]+")', r.text), url
    demo_calls = [x for x in fake.calls if x[0] == "demo"]
    assert len(demo_calls) == 1 and demo_calls[0][2:] == (start, months, 7)  # created once, then reused
    assert not [x for x in fake.calls if x[0] == "search"]
    for url in ["/pp/settings", "/pp/import", "/pp/file"]:
        assert c.get(url).status_code == 404, url
    for url in ["/pp/quotes", "/pp/sync", "/pp/settings", "/pp/transactions/fee-1/delete", f"/pp/securities/{ETF}"]:
        assert c.post(url, data={"csrf_token": csrf}).status_code == 404, url
    assert fake.deleted == []


def test_csrf_is_required(web, fake):
    for url in ["/pp/sync", "/pp/quotes", "/pp/settings", f"/pp/securities/{ETF}", "/pp/transactions/fee-1/delete",
                "/pp/file/create", "/pp/import/s-1/apply"]:
        assert web.post(url, data={}).status_code == 400, url
    assert fake.calls == [] and fake.deleted == []


def test_pp_changes_request_the_nextcloud_copy(web, pp_app, fake, monkeypatch):
    st = _state(pp_app)
    calls = []
    monkeypatch.setattr(st.backup, "request_extras", lambda: calls.append(1))
    web.post("/pp/file/create", data={"csrf_token": web.csrf})
    web.post("/pp/transactions/fee-1/delete", data={"csrf_token": web.csrf})
    assert len(calls) == 2
    assert st.backup.extras == st.pp.backup_files
    assert st.pp.backup_files() == [(".xml", b"<client/>")]


def _zip(members: dict) -> bytes:
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_pdf_import_unpacks_archives(web, fake):
    import tarfile
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w:gz") as tf:
        info = tarfile.TarInfo("2026/Dividende.pdf")
        info.size = 10
        tf.addfile(info, io.BytesIO(b"%PDF-1.4 y"))
    z = _zip({"a/Kauf.pdf": b"%PDF-1.4 x", "liesmich.md": b"x", "__MACOSX/a/._Kauf.pdf": b"x", "b/": b""})
    r = web.post("/pp/import", data={"csrf_token": web.csrf, "auto_feed": "1",
                                     "files": [(io.BytesIO(z), "belege.zip"), (io.BytesIO(tbuf.getvalue()), "alt.tgz"),
                                               (io.BytesIO(b"%PDF-1.4 z"), "Verkauf.pdf")]},
                 content_type="multipart/form-data")
    assert r.status_code == 302
    assert fake.calls[-1][:2] == ("import", ["belege.zip/a/Kauf.pdf", "alt.tgz/2026/Dividende.pdf", "Verkauf.pdf"])


def test_pdf_import_reports_bad_archives(web, fake):
    n = len(fake.calls)
    r = web.post("/pp/import", data={"csrf_token": web.csrf,
                                     "files": [(io.BytesIO(b"kein zip"), "kaputt.zip"),
                                               (io.BytesIO(_zip({"x.md": b"x"})), "leer.zip")]},
                 content_type="multipart/form-data", follow_redirects=True)
    assert "kaputt.zip: Archiv nicht lesbar" in r.text and "leer.zip: enthält keine PDF" in r.text
    assert "mindestens eine PDF" not in r.text and len(fake.calls) == n


def test_archive_limits(monkeypatch):
    from gnubook.pp import archive
    monkeypatch.setattr(archive, "MAX_MEMBERS", 2)
    docs, problems = archive.expand([("viel.zip", _zip({f"{i}.pdf": b"%PDF" for i in range(3)}))])
    assert docs == [] and problems == [("viel.zip", "mehr als 2 Dokumente")]
    monkeypatch.setattr(archive, "MAX_TOTAL", 100)
    docs, problems = archive.expand([("gross.zip", _zip({"a.pdf": b"0" * 1000}))])
    assert docs == [] and problems == [("gross.zip", "entpackt größer als 0 MB")]


def test_manual_delivery(web, fake):
    page = web.get("/pp/delivery")
    assert page.status_code == 200 and "Ein-/Auslieferung" in page.text and ETF in page.text
    r = web.post("/pp/delivery", data={"csrf_token": web.csrf, "type": "DELIVERY_OUTBOUND", "security": ETF,
                                       "portfolio": P1, "date": "04.10.2023", "shares": "2", "amount": "1.234,50",
                                       "fees": "", "taxes": "", "note": "Depotauslieferung"})
    assert r.status_code == 302 and "/pp/transactions" in r.headers["Location"]
    call = [c for c in fake.calls if c[0] == "add_delivery"][-1][1]
    assert call == {"type": "DELIVERY_OUTBOUND", "portfolio": P1, "date": "2023-10-04", "shares": "2",
                    "note": "Depotauslieferung", "force": False, "amount": "1234.50", "security": ETF}
    assert any(t["type"] == "DELIVERY_OUTBOUND" and t.get("note") == "Depotauslieferung"
               for t in fake.export_data["transactions"])


def test_manual_delivery_new_security_and_value_from_price(web, fake):
    r = web.post("/pp/delivery", data={"csrf_token": web.csrf, "type": "DELIVERY_INBOUND", "security": "new",
                                       "isin": "ie00bkm4gz66", "name": "iShs Core MSCI EM IMI", "date": "2023-01-02",
                                       "shares": "20,5", "amount": ""})
    assert r.status_code == 302
    call = [c for c in fake.calls if c[0] == "add_delivery"][-1][1]
    assert call["isin"] == "IE00BKM4GZ66" and call["name"] == "iShs Core MSCI EM IMI"
    assert call["shares"] == "20.5" and "amount" not in call and "security" not in call


def test_manual_delivery_validation_and_confirmation(web, fake):
    base = {"csrf_token": web.csrf, "type": "DELIVERY_OUTBOUND", "security": ETF, "date": "2023-10-04"}
    r = web.post("/pp/delivery", data={**base, "shares": ""}, follow_redirects=True)
    assert "Stückzahl" in r.text
    r = web.post("/pp/delivery", data={**base, "shares": "2", "date": "2999-01-01"}, follow_redirects=True)
    assert "Zukunft" in r.text
    r = web.post("/pp/delivery", data={**base, "security": "new", "isin": "XX", "shares": "2"},
                 follow_redirects=True)
    assert "ISIN" in r.text
    assert not [c for c in fake.calls if c[0] == "add_delivery"]
    # more than held: pp-core refuses, the form asks for confirmation and keeps the input
    r = web.post("/pp/delivery", data={**base, "shares": "5000"})
    assert r.status_code == 200 and 'name="force"' in r.text and 'value="5000"' in r.text
    r = web.post("/pp/delivery", data={**base, "shares": "5000", "force": "1"})
    assert r.status_code == 302


def test_delivery_hidden_from_demo_user(pp_app):
    from gnubook.web import pp as pp_views

    assert "pp.delivery" not in pp_views.DEMO_ENDPOINTS
