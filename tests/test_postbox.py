"""Belegeingang: planning, runs with a fake fetcher, pp-core (fake), Nextcloud (fake WebDAV), web pages and CLI.

The browser part (gnubook.postbox.fzero) is tested against a stand-in of the web app in test_postbox_browser.py.
"""
import stat
from datetime import date, timedelta

import pytest

from gnubook.pp.client import PPCoreError
from gnubook.postbox import jobs, plan
from gnubook.postbox.fzero import FetchResult, LoginFailed
from gnubook.postbox.plan import Listed
from gnubook.postbox.service import PostboxService
from gnubook.postbox.settings import SourceSettings

from .conftest import PASSWORD, csrf_from
from .pp_fixtures import fake  # noqa: F401 – fixture
from .test_nextcloud import APP_PW, DAV_USER, LOGIN, nc  # noqa: F401 – fixture

KIND = "finanzen-zero"
D = date
TODAY = date(2026, 10, 20)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    """The test documents are dated October 2026; a run never moves its mark past "today"."""
    monkeypatch.setattr("gnubook.postbox.service._today", lambda: TODAY)


# ------------------------------------------------------------------------------------------ plan

def test_parse_day():
    today = D(2026, 10, 11)
    assert plan.parse_day("10.10.26", today) == D(2026, 10, 10)
    assert plan.parse_day(" 1.2.2026 ", today) == D(2026, 2, 1)
    assert plan.parse_day("Heute", today) == today
    assert plan.parse_day("gestern, 18:02", today) == D(2026, 10, 10)
    for bad in ("", None, "31.02.26", "Oktober 2026", "123.10.26"):
        assert plan.parse_day(bad, today) is None


def test_stop_day():
    since = D(2026, 10, 1)
    assert plan.stop_day(since, None, 3) == since                           # first run
    assert plan.stop_day(since, D(2026, 10, 10), 3) == D(2026, 10, 7)       # newest complete day − overlap
    assert plan.stop_day(since, D(2026, 10, 2), 3) == since                 # never before "since"
    assert plan.stop_day(since, D(2026, 10, 10), 3, D(2026, 10, 4)) == D(2026, 10, 4)  # one-time look back
    assert plan.stop_day(since, D(2026, 10, 10), 3, D(2026, 9, 1)) == since
    assert plan.stop_day(None, None, 3) is None


def _l(ident, day, title="Beleg"):
    return Listed(ident, title, day, f"/api/posteingang/1/doc/{ident}/x.pdf")


def test_batch_done_and_select():
    stop = D(2026, 10, 7)
    assert plan.batch_done([_l("1", D(2026, 10, 6)), _l("2", D(2026, 10, 1))], stop)
    assert not plan.batch_done([_l("1", D(2026, 10, 7)), _l("2", D(2026, 10, 1))], stop)
    assert not plan.batch_done([_l("1", None)], stop)        # no readable day: keep listing
    assert not plan.batch_done([], stop) and not plan.batch_done([_l("1", D(2026, 1, 1))], None)
    listed = [_l("1", D(2026, 10, 9)), _l("2", D(2026, 10, 9)), _l("1", D(2026, 10, 9)), _l("3", D(2026, 9, 30)),
              _l("4", None), Listed("5", "ohne Link", D(2026, 10, 9), "")]
    assert [d.external_id for d in plan.select(listed, {"2"}, D(2026, 10, 1))] == ["1", "4"]
    assert plan.complete_day_after(listed, [], None) == D(2026, 10, 9)
    assert plan.complete_day_after(listed, [D(2026, 10, 2)], None) == D(2026, 10, 2)
    assert plan.complete_day_after([_l("1", None)], [], D(2026, 1, 1)) == D(2026, 1, 1)


def test_file_name():
    assert (plan.file_name(D(2026, 10, 9), "246891282", "Wertpapierabrechnung: Kauf DE0008430026")
            == "2026-10-09_246891282_Wertpapierabrechnung-Kauf-DE0008430026.pdf")
    assert plan.file_name(None, "../x", "Ärger über Gebühren für Überträge") == \
        "ohne-Datum_x_Aerger-ueber-Gebuehren-fuer-Uebertraege.pdf"
    assert plan.file_name(D(2026, 1, 1), "", "") == "2026-01-01_x_Dokument.pdf"
    assert len(plan.file_name(D(2026, 1, 1), "1", "a" * 300)) < 120


def test_doc_status():
    from gnubook.postbox.service import _doc_status

    ok = {"kind": "BuySellEntryItem", "status": "OK", "messages": [], "imported": True}
    sec = {"kind": "SecurityItem", "status": "OK", "messages": [], "imported": True, "file": None}
    warn = {"kind": "TransactionItem", "status": "WARNING", "imported": False,
            "messages": [{"code": "WARNING", "message": "Buchung existiert möglicherweise schon"}]}
    target = {"kind": "BuySellEntryItem", "status": "ERROR", "imported": False,
              "messages": [{"code": "ERROR", "message": "target account missing"}]}
    assert _doc_status([ok, sec], [], False) == ("imported", "1 Buchungen übernommen.")
    assert _doc_status([ok, warn], [], False)[0] == "review"
    assert _doc_status([target], [], True)[0] == "needs_target"
    assert _doc_status([], ["no transactions recognised (unsupported bank or document type)"], False)[0] == "unsupported"
    assert _doc_status([], ["PDF kaputt"], False) == ("error", "PDF kaputt")
    # OK, but not taken (its new security failed): never "imported"
    bad_sec = {"kind": "SecurityItem", "status": "ERROR", "imported": False, "error": "ISIN ungültig", "messages": []}
    not_taken = {"kind": "BuySellEntryItem", "status": "OK", "messages": [], "imported": False}
    assert _doc_status([not_taken, bad_sec], [], False) == ("error", "ISIN ungültig")
    assert _doc_status([ok, dict(not_taken)], [], False)[0] == "partial"
    skip = {"kind": "SkippedItem", "status": "SKIP", "imported": False, "messages": [{"code": "SKIP", "message": "Kosteninformation"}]}
    assert _doc_status([skip], [], False) == ("skipped", "Kosteninformation")
    # session waiting for a securities account: OK items are not taken yet, skipped ones do not change that
    assert _doc_status([not_taken, skip], [], True)[0] == "needs_target"


def test_settings_json():
    s = SourceSettings.from_json('{"since": "2026-10-01", "overlap_days": 999, "unknown": 1, "enabled": true}')
    assert s.since_day == D(2026, 10, 1) and s.overlap_days == 60 and s.enabled
    assert SourceSettings.from_json("kaputt").since == "" and SourceSettings.from_json("[1]").import_pp
    assert SourceSettings.from_json(s.to_json()) == s


def test_jobs(tmp_path):
    jobs.add_request(tmp_path, 1, KIND, "dry-run", "simon")
    jobs.add_request(tmp_path, 1, KIND, "fetch", "simon")
    jobs.add_request(tmp_path, 2, KIND, "process", "anna")
    (jobs.request_dir(tmp_path) / "kaputt.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        jobs.add_request(tmp_path, 1, KIND, "rm -rf", "x")
    assert [r["mode"] for r in jobs.pending(tmp_path, 1)] == ["dry-run", "fetch"]
    taken = jobs.take(tmp_path)
    assert len(taken) == 3 and not list(jobs.request_dir(tmp_path).iterdir())
    assert jobs.merge(taken) == {(1, KIND): ["fetch"], (2, KIND): ["process"]}
    assert jobs.merge([{"book": "3", "source": KIND, "mode": "dry-run"},
                       {"book": "3", "source": KIND, "mode": "process"}]) == {(3, KIND): ["process", "dry-run"]}


# ------------------------------------------------------------------------------------------ runs

class FakeFetcher:
    """Lists `listing` down to a few days past the stop day, like the real postbox."""

    def __init__(self, listing, fail=(), error=None, stopped="date"):
        self.listing, self.fail, self.error, self.stopped = listing, set(fail), error, stopped
        self.stops = []

    def fetch(self, stop, choose, sink, dry_run=False):
        self.stops.append(stop)
        if self.error is not None:
            raise self.error
        listed = [d for d in self.listing if stop is None or d.day is None or d.day >= stop - timedelta(days=5)]
        res = FetchResult(listed=listed, clicks=2, stopped=self.stopped)
        res.selected = list(choose(listed))
        if not dry_run:
            for d in res.selected:
                if d.external_id in self.fail:
                    res.failed.append((d, "Download fehlgeschlagen: HTTP 500"))
                    continue
                sink(d, b"%PDF-1.4 test " + d.external_id.encode())
                res.downloaded += 1
        return res


def _listing():
    """Two documents per day from 10.10. back to 20.09.: a contract note and a tax statement, plus one
    Verlusttopfoptimierung (which PP does not read) on 05.10."""
    out = []
    for i in range(21):
        day = D(2026, 10, 10) - timedelta(days=i)
        base = 246000000 + i * 1000
        out.append(Listed(str(base + 1), "Wertpapierabrechnung: Kauf DE0008430026", day, f"/doc/{base + 1}.pdf"))
        out.append(Listed(str(base + 2), "Steuerausgleichsrechnung", day, f"/doc/{base + 2}.pdf"))
    out.append(Listed("246999999", "Verlusttopfoptimierung", D(2026, 10, 5), "/doc/246999999.pdf"))
    return out


class FakeImport:
    """pp-core's PDF import, by file name: contract notes are imported, tax statements are possible duplicates,
    Verlusttopfoptimierung is not recognised."""

    def __init__(self, fake):
        self.fake = fake
        self.calls = []
        self.sessions = {}
        self.needs_target = False
        self.unreachable = False
        fake.import_pdfs = self.import_pdfs
        fake.import_session = self.import_session

    def import_pdfs(self, cid, files, portfolio=None, account=None, apply=True, auto_feed=True):
        if self.unreachable:
            raise PPCoreError("pp-core nicht erreichbar (test)")
        self.calls.append([n for n, _d in files])
        items, errors = [], []
        for name, data in files:
            assert data.startswith(b"%PDF")
            if "Verlusttopf" in name:
                errors.append({"file": name, "message": "no transactions recognised (unsupported bank or document type)"})
            elif "Steuerausgleich" in name:
                items.append({"index": len(items), "file": name, "kind": "TransactionItem", "status": "WARNING",
                              "messages": [{"code": "WARNING", "message": "Buchung existiert möglicherweise schon"}],
                              "imported": False, "error": None})
            else:
                items.append({"index": len(items), "file": name, "kind": "SecurityItem", "status": "OK",
                              "messages": [], "imported": not self.needs_target, "error": None})
                items.append({"index": len(items), "file": name, "kind": "BuySellEntryItem",
                              "status": "ERROR" if self.needs_target else "OK",
                              "messages": [{"code": "ERROR", "message": "target account missing"}] if self.needs_target
                              else [], "imported": not self.needs_target, "error": None})
        sid = f"s-{len(self.calls)}"
        result = {"session": sid, "items": items, "fileErrors": errors, "needsTarget": self.needs_target,
                  "imported": sum(1 for i in items if i["imported"])}
        self.sessions[sid] = result
        return result

    def import_session(self, cid, session):
        if session not in self.sessions:
            raise PPCoreError("import session expired or unknown", 404, "not_found")
        return self.sessions[session]


@pytest.fixture
def reg(cfg, fake, nc, monkeypatch):
    from gnubook.system import Registry

    cfg.pp.url = "http://pp-core.test"
    cfg.pp.token = "t" * 30
    cfg.postbox.enabled = True
    r = Registry(cfg)
    r.system.set_nextcloud_account(1, nc.url, LOGIN, APP_PW, DAV_USER)
    yield r
    r.dispose()


@pytest.fixture
def book(reg):
    return reg.context(reg.system.books()[0]["id"])


@pytest.fixture
def pp_import(fake):
    return FakeImport(fake)


@pytest.fixture
def fetcher(monkeypatch):
    f = FakeFetcher(_listing())
    monkeypatch.setattr("gnubook.postbox.service.make_fetcher", lambda *a, **k: f)
    return f


def _source(reg, book, **changes):
    st = SourceSettings(since="2026-10-01", nc_folder="/Finanzen/zero")
    for k, v in changes.items():
        setattr(st, k, v)
    reg.system.set_postbox_source(book.id, KIND, 1, "simon@example.org", "geheim-123", st.to_json())


def test_first_and_second_run(reg, book, fetcher, pp_import, nc, cfg):
    _source(reg, book)
    svc = PostboxService(reg, book)
    out = svc.run(KIND, "test")
    assert out.status == "ok", out.message
    assert fetcher.stops == [D(2026, 10, 1)]
    docs = svc.store.docs(KIND, limit=500)
    assert len(docs) == 21 and out.downloaded == 21  # 10 days × 2 + Verlusttopf
    by_title = {}
    for d in docs:
        by_title.setdefault(d["title"], []).append(d)
        path = svc.doc_path(d)
        assert path.read_bytes().startswith(b"%PDF") and stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.parent.name == "2026" and f"book-{book.id}" in str(path)
    assert {d["pp_status"] for d in by_title["Wertpapierabrechnung: Kauf DE0008430026"]} == {"imported"}
    assert {d["pp_status"] for d in by_title["Steuerausgleichsrechnung"]} == {"review"}
    assert by_title["Steuerausgleichsrechnung"][0]["pp_session"] == "s-1"
    assert by_title["Verlusttopfoptimierung"][0]["pp_status"] == "unsupported"
    assert len(pp_import.calls) == 1 and len(pp_import.calls[0]) == 21  # one batch
    # copies in Nextcloud, one folder per year
    assert {d["nc_status"] for d in docs} == {"done"}
    first = by_title["Verlusttopfoptimierung"][0]
    assert nc.files[f"/Finanzen/zero/2026/{first['filename']}"].startswith(b"%PDF")
    assert first["nc_path"] == f"/Finanzen/zero/2026/{first['filename']}"
    assert svc.store.complete_day(KIND) == D(2026, 10, 10)
    run = svc.store.runs(KIND, 1)[0]
    assert run["status"] == "ok" and run["new"] == 21 and "10 in Portfolio Performance" in run["message"]

    # later: two new documents and one posted late for an already listed day
    fetcher.listing = [Listed("247000001", "Wertpapierabrechnung: Verkauf LU0908500753", D(2026, 10, 12), "/n1"),
                       Listed("247000002", "Steuerausgleichsrechnung", D(2026, 10, 12), "/n2"),
                       Listed("247000003", "Wertpapierabrechnung: Kauf DE0008430026", D(2026, 10, 9), "/n3"),
                       ] + _listing()
    out = svc.run(KIND, "test")
    assert fetcher.stops[-1] == D(2026, 10, 7)
    assert out.new == 3 and out.downloaded == 3
    assert len(pp_import.calls) == 2 and len(pp_import.calls[1]) == 3  # only the new ones
    assert svc.store.complete_day(KIND) == D(2026, 10, 12)
    assert svc.store.counts(KIND)["total"] == 24


def test_incomplete_run_and_failed_download_keep_the_mark(reg, book, fetcher, pp_import):
    _source(reg, book)
    svc = PostboxService(reg, book)
    failing = next(d for d in fetcher.listing if d.day == D(2026, 10, 4))
    fetcher.fail = {failing.external_id}
    out = svc.run(KIND, "test")
    assert out.status == "warning" and "1 Downloads fehlgeschlagen" in out.message
    assert svc.store.complete_day(KIND) == D(2026, 10, 4)   # not past the failed document
    assert failing.external_id not in svc.store.known_ids(KIND)
    fetcher.fail = set()
    svc.run(KIND, "test")
    assert fetcher.stops[-1] == D(2026, 10, 1)            # 04.10. − 3 days, not before "since"
    assert failing.external_id in svc.store.known_ids(KIND)
    assert svc.store.complete_day(KIND) == D(2026, 10, 10)

    fetcher.stopped = "limit"
    fetcher.listing = [Listed("248000001", "Steuerausgleichsrechnung", D(2026, 10, 20), "/x")] + fetcher.listing
    out = svc.run(KIND, "test")
    assert out.status == "warning" and "Klickgrenze" in out.message
    assert svc.store.complete_day(KIND) == D(2026, 10, 10)  # unchanged: the list was not read completely


def test_dry_run_rescan_and_errors(reg, book, fetcher, pp_import):
    svc = PostboxService(reg, book)
    _source(reg, book, rescan_from="2026-09-25", since="2026-09-20")
    out = svc.run(KIND, "test", mode="dry-run")
    assert out.status == "dry_run" and out.downloaded == 0 and svc.store.counts(KIND)["total"] == 0
    assert "Probelauf" in out.message
    out = svc.run(KIND, "test")
    assert fetcher.stops[-1] == D(2026, 9, 20)
    assert svc.settings_of(svc.source(KIND)).rescan_from == ""  # used up by the complete run
    assert svc.settings_of(svc.source(KIND)).since == "2026-09-20"

    fetcher.error = LoginFailed("Anmeldung abgelehnt: falsch")
    out = svc.run(KIND, "test")
    assert out.status == "login_failed" and out.failed
    assert svc.store.runs(KIND, 1)[0]["status"] == "login_failed"


def test_lock(reg, book, fetcher, pp_import):
    _source(reg, book)
    svc = PostboxService(reg, book)
    with svc._lock(KIND):
        assert svc.run(KIND, "test").status == "busy"
    assert svc.run(KIND, "test").status == "ok"


def test_pp_unreachable_then_needs_target_then_ok(reg, book, fetcher, pp_import):
    _source(reg, book, nc_folder="")
    svc = PostboxService(reg, book)
    pp_import.unreachable = True
    out = svc.run(KIND, "test")
    assert "nicht erreichbar" in out.message
    assert {d["pp_status"] for d in svc.store.docs(KIND)} == {"pending"}
    assert {d["nc_status"] for d in svc.store.docs(KIND)} == {"off"}
    pp_import.unreachable = False
    pp_import.needs_target = True
    svc.run(KIND, "test", mode="process")
    notes = [d for d in svc.store.docs(KIND) if d["title"].startswith("Wertpapier")]
    assert {d["pp_status"] for d in notes} == {"needs_target"}
    # the user chose the securities account on the import page in the meantime
    pp_import.needs_target = False
    for res in pp_import.sessions.values():
        for item in res["items"]:
            if item["kind"] == "BuySellEntryItem":
                item.update(imported=True, status="OK", messages=[])
    svc.refresh_sessions(KIND)
    notes = [d for d in svc.store.docs(KIND) if d["title"].startswith("Wertpapier")]
    assert {d["pp_status"] for d in notes} == {"imported"}


def test_import_switched_off(reg, book, fetcher, pp_import):
    _source(reg, book, import_pp=False, nc_folder="")
    svc = PostboxService(reg, book)
    svc.run(KIND, "test")
    assert {d["pp_status"] for d in svc.store.docs(KIND)} == {"off"} and not pp_import.calls


def test_review_uses_live_session_or_imports_again(reg, book, fetcher, pp_import):
    _source(reg, book, nc_folder="")
    svc = PostboxService(reg, book)
    svc.run(KIND, "test")
    doc = next(d for d in svc.store.docs(KIND) if d["pp_status"] == "review")
    assert svc.review(doc["id"]) == "s-1"
    pp_import.sessions.clear()  # expired
    sid = svc.review(doc["id"])
    assert sid == "s-2" and pp_import.calls[-1] == [doc["filename"]]


def test_nextcloud_error_and_retry(reg, book, fetcher, pp_import, nc):
    _source(reg, book)
    nc.readonly.add("/Finanzen/zero/2026")
    svc = PostboxService(reg, book)
    out = svc.run(KIND, "test")
    assert "Nextcloud" in out.message
    states = [d["nc_status"] for d in svc.store.docs(KIND)]
    assert states.count("error") == 3 and set(states) == {"error", "pending"}  # stops after 3 failures in a row
    nc.readonly.clear()
    doc = svc.store.docs(KIND)[0]
    assert svc.upload_one(KIND, doc["id"]) == (True, "")
    assert svc.store.doc(doc["id"])["nc_status"] == "done"
    svc.run(KIND, "test", mode="process")
    assert {d["nc_status"] for d in svc.store.docs(KIND)} == {"done"}


# ------------------------------------------------------------------------------------------ web

@pytest.fixture
def web(cfg, fake, nc, fetcher, pp_import, tmp_path):
    from gnubook import create_app

    cfg.pp.url = "http://pp-core.test"
    cfg.pp.token = "t" * 30
    cfg.postbox.enabled = True
    app = create_app(cfg)
    app.testing = True
    c = app.test_client()
    page = c.get("/login")
    c.post("/login", data={"username": "tester", "password": PASSWORD, "csrf_token": csrf_from(page.text)})
    c.csrf = csrf_from(c.get("/").text)
    c.app_ = app
    yield c
    app.extensions["gnubook"].dispose()


def test_menu_only_when_enabled(client):
    assert "/postbox/" not in client.get("/").text
    assert client.get("/postbox/").status_code == 404


def test_web_settings_run_request_and_documents(web, cfg, nc):
    reg = web.app_.extensions["gnubook"]
    assert "/postbox/" in web.get("/").text
    page = web.get("/postbox/").text
    assert "Noch nicht eingerichtet" in page
    r = web.post("/postbox/source", data={"csrf_token": web.csrf, "login": "simon@example.org", "password": "",
                                          "since": ""}, follow_redirects=True)
    assert "Bitte das Passwort angeben" in r.text and "Belege ab" in r.text
    assert reg.system.postbox_source(1, KIND) is None
    r = web.post("/postbox/source", data={"csrf_token": web.csrf, "login": "simon@example.org",
                                          "password": "geheim-123", "since": "01.10.2026", "overlap_days": "2",
                                          "enabled": "1", "import_pp": "1", "nc_folder": "Finanzen//zero/",
                                          "nc_subfolders": "year"}, follow_redirects=True)
    assert "Gespeichert" in r.text
    row = reg.system.postbox_source(1, KIND)
    assert row["secret"].startswith("enc:v1:") and "geheim-123" not in row["secret"]
    assert reg.system.postbox_password(row) == "geheim-123"
    st = SourceSettings.from_json(row["settings"])
    assert (st.since, st.overlap_days, st.enabled, st.nc_folder) == ("2026-10-01", 2, True, "/Finanzen/zero")
    assert "geheim-123" not in r.text
    # keep the password when the field stays empty
    web.post("/postbox/source", data={"csrf_token": web.csrf, "login": "simon@example.org", "password": "",
                                      "since": "2026-10-01", "import_pp": "1", "nc_folder": "/Finanzen/zero"})
    assert reg.system.postbox_password(reg.system.postbox_source(1, KIND)) == "geheim-123"
    reg.system.set_nextcloud_account(1, nc.url, LOGIN, APP_PW, DAV_USER)
    # moving "Belege ab" back makes the next run look back once
    web.post("/postbox/source", data={"csrf_token": web.csrf, "login": "simon@example.org", "password": "",
                                      "since": "2026-09-28", "import_pp": "1", "nc_folder": "/Finanzen/zero"})
    st = SourceSettings.from_json(reg.system.postbox_source(1, KIND)["settings"])
    assert (st.since, st.rescan_from) == ("2026-09-28", "2026-09-28")

    r = web.post("/postbox/run", data={"csrf_token": web.csrf, "mode": "fetch"}, follow_redirects=True)
    assert "Angefordert" in r.text and "wartet auf den Hintergrunddienst" in r.text
    assert [q["mode"] for q in jobs.pending(cfg.app.data_dir, 1)] == ["fetch"]
    assert web.post("/postbox/run", data={"csrf_token": web.csrf, "mode": "bogus"}).status_code == 400

    # what the background service does: run the request
    ctx = reg.context(1)
    svc = PostboxService(reg, ctx)
    svc.run(KIND, "simon", "fetch", sync_now=False)
    page = web.get("/postbox/").text
    assert "Wertpapierabrechnung: Kauf DE0008430026" in page and "übernommen" in page and "prüfen" in page
    doc = next(d for d in svc.store.docs(KIND) if d["pp_status"] == "review")
    pdf = web.get(f"/postbox/docs/{doc['id']}/pdf")
    assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF") and pdf.mimetype == "application/pdf"
    assert web.get(f"/postbox/docs/{doc['id']}/pdf?source=andere").status_code == 404
    r = web.post(f"/postbox/docs/{doc['id']}/review", data={"csrf_token": web.csrf})
    assert r.status_code == 302 and f"/pp/import?session={doc['pp_session']}" in r.headers["Location"]
    assert "nur zu prüfende" in page
    assert doc["title"] in web.get("/postbox/?filter=attention").text
    web.post(f"/postbox/docs/{doc['id']}/done", data={"csrf_token": web.csrf})
    assert svc.store.doc(doc["id"])["pp_status"] == "done"
    # the last form had "Unterordner je Jahr" unchecked: no year folder
    assert nc.files[f"/Finanzen/zero/{doc['filename']}"].startswith(b"%PDF")
    svc.store.set_nc(doc["id"], "error", message="weg")
    r = web.post(f"/postbox/docs/{doc['id']}/nextcloud", data={"csrf_token": web.csrf}, follow_redirects=True)
    assert "In die Nextcloud kopiert" in r.text and svc.store.doc(doc["id"])["nc_status"] == "done"

    web.post("/language/en", data={"csrf_token": web.csrf, "next": "/"})
    page = web.get("/postbox/").text
    assert '<html lang="en"' in page and "Document inbox" in page and "Fetch now" in page

    r = web.post("/postbox/source/delete", data={"csrf_token": web.csrf}, follow_redirects=True)
    assert reg.system.postbox_source(1, KIND) is None and svc.store.counts(KIND)["total"] > 0


def test_demo_user_sees_nothing(cfg, fake):
    from gnubook import create_app

    cfg.postbox.enabled = True
    app = create_app(cfg)
    app.testing = True
    c = app.test_client()
    r = c.post("/demo", data={"csrf_token": csrf_from(c.get("/login").text)})
    assert r.status_code == 302
    try:
        assert c.get("/postbox/").status_code == 404
        assert "/postbox/" not in c.get("/").text
    finally:
        app.extensions["gnubook"].dispose()


# ------------------------------------------------------------------------------------------ command line

def test_cli_requests_and_status(cfg, fake, nc, fetcher, pp_import, monkeypatch):
    from click.testing import CliRunner

    from gnubook import cli
    from gnubook.system import Registry

    cfg.pp.url = "http://pp-core.test"
    cfg.pp.token = "t" * 30
    cfg.postbox.enabled = True
    monkeypatch.setattr(cli, "_cfg", lambda ctx: cfg)
    reg = Registry(cfg)
    try:
        book = reg.context(reg.system.books()[0]["id"])
        _source(reg, book, enabled=False)
    finally:
        reg.dispose()
    runner = CliRunner()
    r = runner.invoke(cli.main, ["postbox-fetch", "--scheduled"])
    assert r.exit_code == 0 and not fetcher.stops  # not switched on for the timer
    jobs.add_request(cfg.app.data_dir, 1, KIND, "dry-run", "simon")
    r = runner.invoke(cli.main, ["postbox-fetch", "--requests"])
    assert r.exit_code == 0, r.output
    assert "dry_run" in r.output and len(fetcher.stops) == 1 and not jobs.pending(cfg.app.data_dir)
    r = runner.invoke(cli.main, ["postbox-fetch"])
    assert r.exit_code == 0 and "ok" in r.output
    r = runner.invoke(cli.main, ["postbox-status"])
    assert "finanzen.net zero als simon@example.org" in r.output and "21 Belege" in r.output
    fetcher.error = LoginFailed("Anmeldung abgelehnt")
    r = runner.invoke(cli.main, ["postbox-fetch"])
    assert r.exit_code == 1 and "login_failed" in r.output

    cfg.postbox.enabled = False
    jobs.add_request(cfg.app.data_dir, 1, KIND, "fetch", "simon")
    r = runner.invoke(cli.main, ["postbox-fetch", "--requests"])
    assert r.exit_code != 0 and "postbox" in r.output and not jobs.pending(cfg.app.data_dir)


def test_failed_login_pauses_the_timer_until_saved(reg, book, fetcher, pp_import):
    _source(reg, book, enabled=True)
    svc = PostboxService(reg, book)
    fetcher.error = LoginFailed("Anmeldung abgelehnt: falsch")
    assert svc.run(KIND, "timer", scheduled=True).status == "login_failed"
    assert svc.paused(KIND) is not None
    out = svc.run(KIND, "timer", scheduled=True)
    assert out.status == "paused" and not out.failed and len(fetcher.stops) == 1  # no second login
    fetcher.error = None
    assert svc.run(KIND, "simon").status == "ok"            # by hand it still logs in
    fetcher.error = LoginFailed("falsch")
    svc.run(KIND, "timer", scheduled=True)
    import time
    time.sleep(1.1)  # timestamps have seconds
    _source(reg, book, enabled=True)                         # saved again: the timer tries once more
    assert svc.paused(KIND) is None
    fetcher.error = None
    assert svc.run(KIND, "timer", scheduled=True).status == "ok"


def test_settings_saved_during_a_run_are_kept(reg, book, fetcher, pp_import, monkeypatch):
    _source(reg, book, rescan_from="2026-10-02", nc_folder="")
    svc = PostboxService(reg, book)
    real_fetch = fetcher.fetch

    def fetch_while_saving(*a, **k):
        _source(reg, book, rescan_from="2026-10-02", nc_folder="", enabled=True)  # user saves meanwhile
        return real_fetch(*a, **k)

    monkeypatch.setattr(fetcher, "fetch", fetch_while_saving)
    svc.run(KIND, "test")
    st = svc.settings_of(svc.source(KIND))
    assert st.enabled and st.rescan_from == ""  # the look back is used up, the new switch survives


def test_future_day_does_not_move_the_mark_past_today(reg, book, fetcher, pp_import):
    _source(reg, book, nc_folder="")
    fetcher.listing = [Listed("299000001", "Steuerausgleichsrechnung", D(2099, 1, 1), "/f")] + _listing()
    svc = PostboxService(reg, book)
    svc.run(KIND, "test")
    assert svc.store.complete_day(KIND) == TODAY


def test_no_copy_for_an_owner_without_access(reg, book, fetcher, pp_import, nc):
    _source(reg, book)
    reg.system.set_book_users(book.id, [])  # the owner lost access to the book
    svc = PostboxService(reg, book)
    out = svc.run(KIND, "test")
    assert "keinen Zugriff" in out.message and not nc.files
    assert {d["nc_status"] for d in svc.store.docs(KIND)} == {"error"}


def test_unexpected_error_finishes_the_run(reg, book, fetcher, pp_import, monkeypatch):
    _source(reg, book)
    svc = PostboxService(reg, book)
    monkeypatch.setattr(svc, "process", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaputt")))
    with pytest.raises(RuntimeError):
        svc.run(KIND, "test")
    assert svc.store.runs(KIND, 1)[0]["status"] == "error" and svc.store.running(KIND) is None


def test_linkless_entries_are_reported(reg, book, fetcher, pp_import):
    _source(reg, book, nc_folder="")
    fetcher.listing = [Listed("299000002", "Mitteilung", D(2026, 10, 10), "")] + _listing()
    out = PostboxService(reg, book).run(KIND, "test")
    assert out.status == "warning" and "ohne Link" in out.message


def test_pause_is_not_lifted_by_a_run_that_did_not_log_in(reg, book, fetcher, pp_import):
    from gnubook.postbox.fzero import FetchError

    _source(reg, book, enabled=True)
    svc = PostboxService(reg, book)
    fetcher.error = LoginFailed("falsch")
    svc.run(KIND, "timer", scheduled=True)
    fetcher.error = FetchError("Seite nicht erreichbar")
    assert svc.run(KIND, "simon").status == "error"          # by hand, the site was down
    assert svc.paused(KIND) is not None                       # still paused: nothing was learnt about the login
    assert svc.run(KIND, "timer", scheduled=True).status == "paused"


def test_documents_failing_in_nextcloud_do_not_hold_back_new_ones(reg, book, fetcher, pp_import, nc):
    _source(reg, book, nc_folder="")
    svc = PostboxService(reg, book)
    svc.run(KIND, "test")
    old = svc.store.docs(KIND, limit=500)
    for d in old[:4]:                                          # four documents whose PDF is gone and that failed
        svc.doc_path(d).unlink()
        svc.store.set_nc(d["id"], "error", message="früher")
    for d in old[4:]:
        svc.store.set_nc(d["id"], "pending")
    _source(reg, book)                                         # Nextcloud folder set now
    svc.run(KIND, "test", mode="process")
    states = {d["id"]: d["nc_status"] for d in svc.store.docs(KIND, limit=500)}
    assert all(states[d["id"]] == "done" for d in old[4:])
    assert all(states[d["id"]] == "error" for d in old[:4])
    assert "PDF fehlt" in svc.store.doc(old[0]["id"])["nc_message"]


def test_own_share_link_and_week_folders(reg, book, web, fetcher, pp_import, nc):
    from .test_nextcloud import SHARE, SHARE_PW

    reg.cfg.nextcloud.allow_http = True
    form = {"csrf_token": web.csrf, "login": "simon@example.org", "password": "geheim-123", "since": "2026-10-08",
            "import_pp": "1", "nc_folder": "/Belege", "nc_subfolders": "week", "nc_share_url": f"{nc.url}/s/{SHARE}",
            "nc_share_password": "falsch"}
    r = web.post("/postbox/source", data=form, follow_redirects=True)
    assert "Freigabelink:" in r.text and reg.system.postbox_source(book.id, KIND) is None
    r = web.post("/postbox/source", data=dict(form, nc_share_password=SHARE_PW), follow_redirects=True)
    assert "Gespeichert" in r.text
    st = SourceSettings.from_json(reg.system.postbox_source(book.id, KIND)["settings"])
    assert st.nc_share_url.endswith(SHARE) and st.nc_share_secret.startswith("enc:v1:") and SHARE_PW not in r.text
    assert st.nc_subfolders == "week"
    # saving again without the password keeps the share
    web.post("/postbox/source", data=dict(form, password="", nc_share_password=""))
    assert SourceSettings.from_json(reg.system.postbox_source(book.id, KIND)["settings"]).nc_share_secret

    fetcher.listing = [Listed("1", "Tageskontoauszug: EUR-Konto LU0908500753", D(2026, 10, 9), "/1"),
                       Listed("2", "Wochenkontoauszug: EUR-Konto", D(2026, 10, 9), "/2"),
                       Listed("3", "Steuerausgleichsrechnung", D(2026, 10, 9), "/3")]
    svc = PostboxService(reg, book)
    out = svc.run(KIND, "test")
    assert out.status == "ok", out.message
    paths = {d["title"][:4]: d["nc_path"] for d in svc.store.docs(KIND)}
    assert all(p.startswith("/Belege/2026/KW41/") for p in paths.values())  # 09.10.2026: ISO week 41
    # in the shared folder (/Share in the fake), not in the Nextcloud of the settings
    assert all(f"/Share{p}" in nc.files for p in paths.values())
    assert not any(k.startswith("/Belege") for k in nc.files)

    # removing the link: back to the Nextcloud of the settings
    web.post("/postbox/source", data=dict(form, password="", nc_share_url="", nc_share_password=""))
    st = SourceSettings.from_json(reg.system.postbox_source(book.id, KIND)["settings"])
    assert (st.nc_share_url, st.nc_share_secret) == ("", "")


def test_subfolders():
    st = SourceSettings.from_json('{"nc_year_folders": false}')
    assert st.nc_subfolders == "" and st.nc_subfolder(D(2026, 10, 9)) == ""
    st = SourceSettings.from_json('{"nc_year_folders": true}')  # settings saved before the choice existed
    assert st.nc_subfolder(D(2026, 10, 9)) == "2026"
    st.nc_subfolders = "week"
    assert st.nc_subfolder(D(2026, 10, 9)) == "2026/KW41" and st.nc_subfolder(D(2027, 1, 1)) == "2026/KW53"
    st.nc_subfolders = "day"
    assert st.nc_subfolder(D(2026, 10, 9)) == "2026/2026-10-09" and st.nc_subfolder(None) == "ohne-Datum"
    st.nc_subfolders = "ymd"
    assert st.nc_subfolder(D(2026, 10, 9)) == "2026/10/09"


def test_without_since_fetches_down_to_the_oldest(reg, book, web, fetcher, pp_import, nc):
    _source(reg, book, since="", nc_folder="")
    svc = PostboxService(reg, book)
    out = svc.run(KIND, "test")
    assert out.status == "ok", out.message
    assert fetcher.stops == [None]  # no limit: "Mehr anzeigen" until the end of the list
    assert len(svc.store.docs(KIND, limit=500)) == len(_listing())  # 20.09. and older too
    assert svc.store.complete_day(KIND) == D(2026, 10, 10)
    svc.run(KIND, "test")
    assert fetcher.stops[-1] == D(2026, 10, 7)  # later runs: complete day minus overlap

    # emptying "Belege ab" in the form after a day was set: the next run lists everything once more
    form = {"csrf_token": web.csrf, "login": "simon@example.org", "password": "", "since": "2026-10-01"}
    web.post("/postbox/source", data=form)
    r = web.post("/postbox/source", data=dict(form, since=""), follow_redirects=True)
    assert "Gespeichert" in r.text and "alle (ältester Beleg)" in r.text
    assert svc.store.complete_day(KIND) is None
    r = web.post("/postbox/source", data=dict(form, since="31.02.2026"), follow_redirects=True)
    assert "kein gültiges Datum" in r.text


def test_cancel_from_the_web_interface(reg, book, web, fetcher, pp_import, cfg):
    from gnubook.postbox.fzero import Cancelled

    _source(reg, book, nc_folder="")
    svc = PostboxService(reg, book)
    original = fetcher.fetch

    def fetch_and_cancel(stop, choose, sink, dry_run=False):
        listed = [d for d in fetcher.listing if d.day >= stop]
        sink(listed[0], b"%PDF-1.4 eins")
        # meanwhile: "Abbrechen" on the page (the run shows as running)
        page = web.get("/postbox/").text
        assert "Abbrechen" in page
        r = web.post("/postbox/cancel", data={"csrf_token": web.csrf}, follow_redirects=True)
        assert "Abbruch angefordert" in r.text
        if fetcher.cancelled():
            raise Cancelled("Abgebrochen.")
        return original(stop, choose, sink, dry_run)

    fetcher.fetch = fetch_and_cancel
    jobs.add_request(cfg.app.data_dir, book.id, KIND, "fetch", "simon")  # a waiting request is dropped as well
    out = svc.run(KIND, "test")
    assert out.status == "cancelled" and "bleiben erhalten" in out.message
    assert svc.store.runs(KIND, 1)[0]["status"] == "cancelled"
    docs = svc.store.docs(KIND)
    assert len(docs) == 1 and docs[0]["pp_status"] == "pending"  # imported by the next run
    assert not jobs.cancel_requested(cfg.app.data_dir, book.id, KIND)  # cleared after the run
    assert jobs.pending(cfg.app.data_dir, book.id) == []
    assert "abgebrochen" in web.get("/postbox/").text


def test_dead_run_is_closed_when_the_page_is_shown(reg, book, web, cfg):
    _source(reg, book)
    svc = PostboxService(reg, book)
    run_id = svc.store.start_run(KIND, "simon")  # its process was killed: nobody holds the lock
    jobs.request_cancel(cfg.app.data_dir, book.id, KIND, "simon")
    assert svc.alive(KIND) is False
    page = web.get("/postbox/").text
    assert "Abbruch angefordert" not in page and "der Prozess lief nicht mehr" in page
    assert svc.store.running(KIND) is None and svc.store.runs(KIND, 1)[0]["id"] == run_id
    assert not jobs.cancel_requested(cfg.app.data_dir, book.id, KIND)
    with svc._lock(KIND):
        svc.store.start_run(KIND, "simon")
        assert svc.alive(KIND) is True and not svc.close_dead_run(KIND)
