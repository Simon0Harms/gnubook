"""The finanzen.net zero fetcher in a real headless browser against a local stand-in of the web app
(tests/fzero_mock.py). Skipped when Playwright or a browser is not available.

GNUBOOK_TEST_BROWSER=firefox|chromium chooses the browser (default: chromium if /opt/pw-browsers/chromium
exists, else firefox); GNUBOOK_TEST_BROWSER_PATH points to a browser binary instead of Playwright's own build.
"""
import os
from datetime import date
from pathlib import Path

import pytest

from gnubook.config import PostboxConfig
from gnubook.postbox import plan
from gnubook.postbox.fzero import ApprovalRequired, FetchError, FinanzenZeroFetcher, LoginFailed

from . import fzero_mock as mock

pytest.importorskip("playwright.sync_api")

_DEFAULT_CHROMIUM = "/opt/pw-browsers/chromium"


def _browser_cfg(base_url: str) -> PostboxConfig:
    cfg = PostboxConfig(enabled=True, finanzen_zero_url=base_url, timeout=8, download_pause_ms=0)
    name = os.environ.get("GNUBOOK_TEST_BROWSER") or ("chromium" if Path(_DEFAULT_CHROMIUM).exists() else "firefox")
    cfg.browser = name
    path = os.environ.get("GNUBOOK_TEST_BROWSER_PATH") or (_DEFAULT_CHROMIUM if name == "chromium" and
                                                           Path(_DEFAULT_CHROMIUM).exists() else "")
    cfg.executable_path = path
    return cfg


@pytest.fixture(scope="module")
def browser_ok():
    """Skips the module when no browser can be started."""
    from playwright.sync_api import Error, sync_playwright

    cfg = _browser_cfg("http://127.0.0.1")
    try:
        with sync_playwright() as pw:
            kwargs = {"headless": True}
            if cfg.executable_path:
                kwargs["executable_path"] = cfg.executable_path
            getattr(pw, cfg.browser).launch(**kwargs).close()
    except Error as exc:
        pytest.skip(f"kein Browser für Playwright: {str(exc).splitlines()[0]}")
    return True


@pytest.fixture
def site(browser_ok):
    docs = mock.make_docs(date(2026, 10, 10), days=26)  # 10.10. back to 15.09., two per day
    server, handler, url = mock.serve(docs)
    yield handler, url
    server.shutdown()


def _run(url, tmp_path, stop, known=frozenset(), since=None, password=mock.PASSWORD, **cfg_changes):
    cfg = _browser_cfg(url)
    for k, v in cfg_changes.items():
        setattr(cfg, k, v)
    got = {}
    fetcher = FinanzenZeroFetcher(cfg, mock.USER, password, tmp_path / "debug", tmp_path / "home")
    result = fetcher.fetch(stop, lambda listed: plan.select(listed, set(known), since),
                           lambda doc, data: got.__setitem__(doc.external_id, data))
    return result, got, fetcher


def test_first_run_lists_down_to_the_start_day_and_downloads(site, tmp_path):
    handler, url = site
    since = date(2026, 10, 1)
    result, got, fetcher = _run(url, tmp_path, since, since=since)
    # page 1: 10.–06.10., page 2: 05.–01.10., page 3: 30.09.–26.09. is wholly older -> stop
    assert result.stopped == "date" and result.complete and result.clicks == 2
    assert len(result.listed) == 30
    expected = {d["id"] for d in handler.docs if d["day"] >= since}
    assert set(got) == expected and len(expected) == 20
    assert all(data.startswith(b"%PDF") for data in got.values())
    assert result.downloaded == 20 and not result.failed
    first = result.listed[0]
    assert first.day == date(2026, 10, 10) and first.title == "Wertpapierabrechnung: Kauf DE0008430026"
    assert first.href.startswith(f"/api/posteingang/{mock.CUSTOMER}/doc/")
    assert handler.logins == [mock.USER]
    assert not handler.sessions  # logged out at the end
    assert any("/api/posteingang/" in p for p in fetcher.api_paths)
    assert (tmp_path / "debug" / "api-paths.txt").exists()
    assert (tmp_path / "home").is_dir()  # the browser's own HOME


def test_later_run_finds_late_and_new_documents_and_stops_early(site, tmp_path):
    handler, url = site
    known = {d["id"] for d in handler.docs if d["day"] >= date(2026, 10, 1)}
    new = [{"id": "246999001", "title": "Steuerausgleichsrechnung", "day": date(2026, 10, 11)},
           {"id": "246999002", "title": "Verlusttopfoptimierung", "day": date(2026, 10, 11)},
           # posted late for an earlier day: within a day it sorts by id, i.e. behind the known ones
           {"id": "246999003", "title": "Wertpapierabrechnung: Verkauf LU0908500753", "day": date(2026, 10, 9)}]
    handler.docs += new
    stop = plan.stop_day(date(2026, 10, 1), date(2026, 10, 10), 3)
    assert stop == date(2026, 10, 7)
    result, got, _f = _run(url, tmp_path, stop, known=known, since=date(2026, 10, 1))
    assert set(got) == {d["id"] for d in new}
    assert result.complete and result.clicks == 2


def test_dry_run_downloads_nothing(site, tmp_path):
    handler, url = site
    cfg = _browser_cfg(url)
    got = []
    result = FinanzenZeroFetcher(cfg, mock.USER, mock.PASSWORD, None).fetch(
        date(2026, 10, 8), lambda listed: plan.select(listed, set(), None), lambda d, b: got.append(d), dry_run=True)
    assert not got and result.selected and result.downloaded == 0


def test_click_limit_and_failed_download(site, tmp_path):
    handler, url = site
    handler.fail_ids.add(handler.docs[1]["id"])
    result, got, _f = _run(url, tmp_path, date(2026, 9, 1), since=date(2026, 9, 1), max_clicks=1)
    assert result.stopped == "limit" and not result.complete and len(result.listed) == 20
    assert [d.external_id for d, _msg in result.failed] == [handler.docs[1]["id"]]
    assert "500" in result.failed[0][1]
    assert len(got) == 19


def test_end_of_list(site, tmp_path):
    handler, url = site
    result, got, _f = _run(url, tmp_path, date(2026, 1, 1), since=date(2026, 1, 1))
    assert result.stopped == "end" and result.complete and len(result.listed) == 52 and len(got) == 52


def test_wrong_password(site, tmp_path):
    handler, url = site
    with pytest.raises(LoginFailed, match="falsch") as err:
        _run(url, tmp_path, date(2026, 10, 1), password="vertippt-123")
    assert err.value.debug and Path(err.value.debug + ".png").exists()
    html = Path(err.value.debug + ".html").read_text(encoding="utf-8")
    assert "Passwort falsch" in html and "vertippt-123" not in html


def test_two_step_login(browser_ok, tmp_path):
    server, handler, url = mock.serve(mock.make_docs(date(2026, 10, 10), days=3), mode="twostep")
    try:
        result, got, _f = _run(url, tmp_path, date(2026, 10, 9), since=date(2026, 10, 9))
        assert len(got) == 4 and result.complete
    finally:
        server.shutdown()


def test_approval_is_reported(browser_ok, tmp_path):
    server, handler, url = mock.serve(mock.make_docs(date(2026, 10, 10), days=3), mode="approval")
    try:
        with pytest.raises(ApprovalRequired):
            _run(url, tmp_path, date(2026, 10, 9), timeout=5)
    finally:
        server.shutdown()


def test_unreadable_days_stop_the_run(site, tmp_path):
    handler, url = site
    handler.day_format = "%d. %b"  # e.g. "10. Oct" – the stop day could never be reached
    with pytest.raises(FetchError, match="nicht lesbar"):
        _run(url, tmp_path, date(2026, 10, 1), since=date(2026, 10, 1))


@pytest.mark.parametrize("mode", ["dialog", "rootstart"])
def test_app_dialogs_are_left_alone_and_start_page_at_root(browser_ok, tmp_path, mode):
    server, handler, url = mock.serve(mock.make_docs(date(2026, 10, 10), days=3), mode=mode)
    try:
        result, got, _f = _run(url, tmp_path, date(2026, 10, 9), since=date(2026, 10, 9))
        assert len(got) == 4 and result.complete
        assert handler.decisions == []  # "Ablehnen"/"Zustimmen" of the app were not clicked
    finally:
        server.shutdown()


def test_redaction_and_api_paths(tmp_path):
    cfg = _browser_cfg("http://127.0.0.1")
    f = FinanzenZeroFetcher(cfg, mock.USER, 'ge<h>eim"1', tmp_path / "debug")
    assert f._redact('value="ge&lt;h&gt;eim&quot;1" x ge<h>eim"1') == 'value="********" x ********'

    class Req:
        resource_type = "fetch"
        method = "GET"

        def __init__(self, url):
            self.url = url

    f._note_request(Req("https://x/api/posteingang/1483210?page=2&size=10"))
    f._note_request(Req("https://x/api/posteingang/1483210/doc/246953731/Steuer.pdf"))
    f._note_request(Req("https://x/assets/app.js"))
    assert f.api_paths == ["GET /api/posteingang/{n}?page&size"]


def test_approval_over_the_postbox_is_reported(browser_ok, tmp_path):
    server, handler, url = mock.serve(mock.make_docs(date(2026, 10, 10), days=3), mode="approval_inbox")
    try:
        with pytest.raises(ApprovalRequired):
            _run(url, tmp_path, date(2026, 10, 9), timeout=5)
    finally:
        server.shutdown()


def test_documents_refused_one_by_one_do_not_stop_the_others(site, tmp_path):
    handler, url = site
    newest = [d["id"] for d in mock.sort_docs(handler.docs)[:4]]
    handler.fail_ids.update(newest)                        # HTTP 500 for the four newest
    result, got, _f = _run(url, tmp_path, date(2026, 10, 1), since=date(2026, 10, 1))
    assert len(result.failed) == 4 and len(got) == 16


def test_lost_session_stops_the_downloads(site, tmp_path):
    handler, url = site
    handler.fail_status = 401
    handler.fail_ids.update(d["id"] for d in handler.docs)  # every download: no session any more
    result, got, _f = _run(url, tmp_path, date(2026, 10, 1), since=date(2026, 10, 1))
    assert not got and len(result.failed) == 20
    assert sum("nicht versucht" in msg for _d, msg in result.failed) == 17


def test_redaction_of_escaped_forms(tmp_path):
    f = FinanzenZeroFetcher(_browser_cfg("http://127.0.0.1"), mock.USER, "o'k\"7&", None)
    text = 'a value="o\'k&quot;7&amp;" b {"pw": "o\'k\\"7&"} c o%27k%227%26 d'
    assert f._redact(text) == 'a value="********" b {"pw": "********"} c ******** d'


def test_cancel_stops_between_downloads_and_logs_out(site, tmp_path):
    from gnubook.postbox.fzero import Cancelled

    handler, url = site
    cfg = _browser_cfg(url)
    got = {}
    fetcher = FinanzenZeroFetcher(cfg, mock.USER, mock.PASSWORD, tmp_path / "debug", tmp_path / "home")
    fetcher.cancelled = lambda: len(got) >= 3  # cancelled in the web interface after three downloads
    since = date(2026, 10, 1)
    with pytest.raises(Cancelled):
        fetcher.fetch(since, lambda listed: plan.select(listed, set(), since),
                      lambda doc, data: got.__setitem__(doc.external_id, data))
    assert len(got) == 3
    assert not list((tmp_path / "debug").glob("*.png"))  # no screenshot for a cancel
