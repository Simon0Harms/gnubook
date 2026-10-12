"""finanzen.net zero: log in with a headless browser, list the postbox and download new documents.

The web app (mein.finanzen-zero.net) is an Angular single-page app. The postbox lists documents on
/posteingang/inbox, ten at a time:

    <zero-navigation-action-list-item data-zid="post-box-item" data-post-id="246953731">
      <a data-zid="zero-navigation-action-list-item-anchor" href="/api/posteingang/<customer>/doc/<id>/<name>.pdf">
        <span data-zid="zero-navigation-action-list-item-label"> Steuerausgleichsrechnung </span>
        <span data-zid="zero-navigation-action-list-item-subtitle">10.10.26</span>
    …
    <a data-zid="post-box-more-link"> Mehr anzeigen </a>

A PDF link is a plain same-origin GET that only needs the session cookies. It is fetched inside the page with
`fetch`, i.e. by the browser's own network stack – no second HTTP client with a different fingerprint.

Only user name and password are needed for the postbox (no Secure TAN). The login page could not be inspected
while this was written, so its fields are recognised generically: the visible password input, the visible text or
e-mail input before it, and the button labelled "Anmelden"/"Login"/"Weiter" (also when the name is asked first and
the password on a second page). [postbox] login_*_selector override the recognition. A cookie banner is answered
before the login, and only inside a cookie/consent container ("nur notwendige"/"ablehnen" first) – after the login
the fetcher never clicks a dialog of the app. When a run fails, a screenshot and the HTML of the page (password
removed) are kept in data_dir/postbox/debug – they show what the browser saw.
"""
from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, quote_plus, urljoin, urlsplit
from zoneinfo import ZoneInfo

from ..i18n import gettext as _
from .plan import Listed, batch_done, parse_day

log = logging.getLogger("gnubook.postbox")

KIND = "finanzen-zero"
INBOX_PATH = "/posteingang/inbox"
ITEM = '[data-zid="post-box-item"][data-post-id]'
MORE = '[data-zid="post-box-more-link"]'
LOGIN_PATHS = ("/", "/login", "/anmelden", "/auth", "/signin")
MAX_PDF = 50 * 1024 * 1024
MAX_FAILURES_IN_A_ROW = 3        # downloads; then the session is probably gone
TZ = ZoneInfo("Europe/Berlin")

# consent buttons, most privacy-friendly first; only clicked inside a cookie/consent container, before the login
CONSENT = (re.compile(r"^\s*(nur (technisch )?(notwendige|erforderliche)|alle ablehnen|ablehnen)", re.I),
           re.compile(r"^\s*(alle akzeptieren|alle cookies akzeptieren|akzeptieren|zustimmen|einverstanden)", re.I))
LOGOUT = re.compile(r"^\s*(abmelden|ausloggen|logout|log out)\s*$", re.I)
APPROVAL = re.compile(r"secure ?tan|freigabe|freigeben|tan[- ]?(eingabe|nummer)|bestätigungscode|sicherheitscode|"
                      r"zwei-faktor|2-faktor|push-nachricht", re.I)


class FetchError(RuntimeError):
    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.debug: str | None = None  # stem of the screenshot/HTML kept for this failure


class LoginFailed(FetchError):
    code = "login_failed"


class ApprovalRequired(FetchError):
    code = "approval_required"


class NotInstalled(FetchError):
    code = "not_installed"


class SessionLost(FetchError):
    """A download failed in a way that affects every further one (no session, no connection, page gone)."""


@dataclass
class FetchResult:
    listed: list[Listed] = field(default_factory=list)
    clicks: int = 0
    stopped: str = ""  # date | end | limit | stalled
    selected: list[Listed] = field(default_factory=list)
    downloaded: int = 0
    failed: list[tuple[Listed, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """The list was read down to the stop day or to its end."""
        return self.stopped in ("date", "end")


# ------------------------------------------------------------------------------------------ JavaScript

_HELPERS = """
const vis = (el) => {
  if (!el) return false;
  const s = getComputedStyle(el);
  if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
};
const isSearch = (i) => /such|search|wkn|isin/i.test([i.placeholder, i.name, i.getAttribute('aria-label')].join(' '));
const userTypes = ['text', 'email', 'tel', ''];
const isUser = (i) => userTypes.includes((i.getAttribute('type') || '').toLowerCase()) && !isSearch(i)
                      && !i.readOnly && !i.disabled;
const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
const label = (b) => clean(b.innerText || b.value || b.getAttribute('aria-label') || '');
const loginLabel = /^(anmelden|einloggen|login|log in|jetzt anmelden|weiter|fortfahren)$/i;
"""

JS_STATE = "() => {" + _HELPERS + """
  const inputs = Array.from(document.querySelectorAll('input')).filter(vis);
  const alerts = Array.from(document.querySelectorAll(
      '[role="alert"], .alert, .invalid-feedback, mat-error, [class*="error"], [class*="danger"]'))
    .filter(vis).map(e => clean(e.innerText)).filter(t => t && t.length < 300);
  const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"]')).filter(vis);
  return {
    path: location.pathname,
    inbox: !!document.querySelector('[data-zid="post-box-page"]'),
    items: document.querySelectorAll('[data-zid="post-box-item"][data-post-id]').length,
    password: inputs.some(i => (i.getAttribute('type') || '').toLowerCase() === 'password'),
    user: inputs.some(isUser),
    loginButton: buttons.some(b => loginLabel.test(label(b))),
    alerts: Array.from(new Set(alerts)).slice(0, 5),
    text: clean(document.body ? document.body.innerText : '').slice(0, 4000),
  };
}"""

# marks the login fields with data-gnubook-login="user|password|submit"
JS_MARK = "() => {" + _HELPERS + """
  document.querySelectorAll('[data-gnubook-login]').forEach(e => e.removeAttribute('data-gnubook-login'));
  const inputs = Array.from(document.querySelectorAll('input')).filter(vis);
  const pw = inputs.find(i => (i.getAttribute('type') || '').toLowerCase() === 'password');
  const users = inputs.filter(isUser);
  let user = null;
  if (pw) {
    const before = users.filter(i => i.compareDocumentPosition(pw) & Node.DOCUMENT_POSITION_FOLLOWING);
    user = before.length ? before[before.length - 1] : null;
  } else {
    const hint = (i) => [i.name, i.id, i.getAttribute('autocomplete'), i.placeholder, i.getAttribute('formcontrolname'),
                         i.getAttribute('aria-label')].join(' ');
    user = users.find(i => /user|login|mail|kunde|benutzer|anmelde/i.test(hint(i))) || users[0] || null;
  }
  if (user) user.setAttribute('data-gnubook-login', 'user');
  if (pw) pw.setAttribute('data-gnubook-login', 'password');
  const anchor = pw || user;
  const form = anchor ? anchor.closest('form') : null;
  const buttons = Array.from((form || document).querySelectorAll('button, input[type="submit"], [role="button"]'))
    .filter(vis);
  let submit = buttons.find(b => loginLabel.test(label(b)))
            || buttons.find(b => /anmelden|einloggen|login/i.test(label(b)))
            || (form ? form.querySelector('[type="submit"]') : null);
  if (submit) submit.setAttribute('data-gnubook-login', 'submit');
  return {user: !!user, password: !!pw, submit: submit ? label(submit) : null};
}"""

# is the element inside a cookie/consent container (also across shadow roots, e.g. Usercentrics)?
JS_IN_CONSENT = """(el) => {
  const named = (n) => /cookie|consent|usercentrics|onetrust|didomi|sourcepoint|cmp|privacy/i.test(
      (n.id || '') + ' ' + (typeof n.className === 'string' ? n.className : ''));
  let n = el;
  while (n) {
    if (n.nodeType === 1) {
      if (named(n)) return true;
      const role = n.getAttribute('role');
      if ((role === 'dialog' || role === 'alertdialog' || n.getAttribute('aria-modal') === 'true')
          && /cookie/i.test(n.innerText || n.textContent || '')) return true;
    }
    if (n.parentElement) { n = n.parentElement; continue; }
    const root = n.getRootNode ? n.getRootNode() : null;
    n = root && root.host ? root.host : null;
  }
  return false;
}"""

JS_ITEMS = """(sel) => Array.from(document.querySelectorAll(sel)).map(el => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const a = el.querySelector('a[href]');
  const label = el.querySelector('[data-zid="zero-navigation-action-list-item-label"]');
  const sub = el.querySelector('[data-zid="zero-navigation-action-list-item-subtitle"]');
  return {id: el.getAttribute('data-post-id') || '', href: a ? a.getAttribute('href') : '',
          title: clean(label ? label.textContent : (a ? a.textContent : '')), day: clean(sub ? sub.textContent : '')};
})"""

JS_FETCH = """async ([url, max, ms]) => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  try {
    const r = await fetch(url, {credentials: 'include', cache: 'no-store', signal: ctrl.signal});
    const type = r.headers.get('content-type') || '';
    if (!r.ok) return {status: r.status, type};
    const buf = new Uint8Array(await r.arrayBuffer());
    if (buf.length > max) return {status: r.status, type, error: 'zu groß (' + buf.length + ' Bytes)'};
    let bin = '';
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return {status: r.status, type, data: btoa(bin)};
  } catch (e) {
    return {status: 0, error: ctrl.signal.aborted ? 'Zeitüberschreitung' : String(e)};
  } finally {
    clearTimeout(timer);
  }
}"""


def _first_line(exc) -> str:
    text = str(exc).strip()
    return text.splitlines()[0][:300] if text else exc.__class__.__name__


class FinanzenZeroFetcher:
    kind = KIND

    def __init__(self, cfg, login: str, password: str, debug_dir: Path | None = None, home_dir: Path | None = None):
        """cfg: gnubook.config.PostboxConfig. home_dir: HOME of the browser (the service user's own home is
        usually not writable; the browser keeps caches there)."""
        self.cfg = cfg
        self.login = login
        self.password = password
        self.base = (cfg.finanzen_zero_url or "https://mein.finanzen-zero.net").rstrip("/")
        self.timeout = max(5, int(cfg.timeout or 45))
        self.debug_dir = Path(debug_dir) if debug_dir else None
        self.home_dir = Path(home_dir) if home_dir else None
        self.api_paths: list[str] = []
        self._submitted = False

    # ------------------------------------------------------------------ run
    def fetch(self, stop: date | None, choose, sink, dry_run: bool = False) -> FetchResult:
        """Lists the postbox down to `stop`; choose(listed) -> entries to download; sink(entry, pdf bytes) stores
        one document (called right after its download, so a run that breaks off keeps what it got)."""
        try:
            from playwright.sync_api import Error as PWError, sync_playwright
        except ImportError:
            raise NotInstalled(_("Playwright fehlt – bitte deploy/install-postbox.sh ausführen.")) from None
        if self.cfg.browsers_path:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = self.cfg.browsers_path
        result = FetchResult()
        with sync_playwright() as pw:
            browser = self._launch(pw, PWError)
            try:
                context = browser.new_context(locale="de-DE", timezone_id="Europe/Berlin",
                                              viewport={"width": 1366, "height": 1000})
                page = context.new_page()
                page.set_default_timeout(self.timeout * 1000)
                page.set_default_navigation_timeout(self.timeout * 1000)
                page.on("request", self._note_request)
                try:
                    self._open_inbox(page)
                    result.listed, result.clicks, result.stopped = self._collect(page, stop)
                    result.selected = list(choose(result.listed))
                    if not dry_run:
                        self._download_all(page, result, sink)
                except FetchError as exc:
                    exc.debug = self._capture(page, exc.code)
                    raise
                except PWError as exc:
                    err = FetchError(_("Fehler im Browser: {a0}", a0=_first_line(exc)))
                    err.debug = self._capture(page, "browser")
                    raise err from None
                finally:
                    if self._submitted:
                        self._logout(page)
            finally:
                self._write_api_paths()
                try:
                    browser.close()
                except PWError:
                    pass
        return result

    def _launch(self, pw, PWError):
        name = (self.cfg.browser or "firefox").strip().lower()
        if name not in ("firefox", "chromium"):
            raise FetchError(_("Unbekannter Browser in [postbox] browser: {a0}", a0=name))
        kwargs = {"headless": True}
        if self.cfg.executable_path:
            kwargs["executable_path"] = self.cfg.executable_path
        if self.home_dir is not None:
            self.home_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            kwargs["env"] = {**os.environ, "HOME": str(self.home_dir)}
        try:
            return getattr(pw, name).launch(**kwargs)
        except PWError as exc:
            if "executable doesn't exist" in str(exc).lower():
                raise NotInstalled(_("Der Browser fehlt – bitte deploy/install-postbox.sh ausführen ({a0}).",
                                     a0=_first_line(exc))) from None
            raise FetchError(_("Der Browser startet nicht: {a0}", a0=_first_line(exc))) from None

    # ------------------------------------------------------------------ login
    @staticmethod
    def _state(page) -> dict | None:
        """What the page shows right now; None while it is navigating (the script context is replaced)."""
        try:
            return page.evaluate(JS_STATE)
        except Exception:  # noqa: BLE001 – "Execution context was destroyed" during a navigation
            return None

    def _login_form(self, st: dict) -> bool:
        """A login form is shown: a password field – or, before the login was sent, a user name field on a
        login path or next to a login button (the first page of a two-step login)."""
        if st["password"]:
            return True
        if self._submitted:
            return False
        on_login_path = st["path"].rstrip("/") in [p.rstrip("/") for p in LOGIN_PATHS]
        return st["user"] and (on_login_path or st["loginButton"])

    def _open_inbox(self, page):
        page.goto(self.base + INBOX_PATH, wait_until="domcontentloaded")
        deadline = time.monotonic() + 3 * self.timeout
        submitted_at = left_login = None
        consent = gone_to_inbox = 0
        st: dict = {}
        while time.monotonic() < deadline:
            now = self._state(page)
            if now is None:
                page.wait_for_timeout(400)
                continue
            st = now
            if st["items"] and not st["password"]:
                return  # list entries only come with a valid session
            if not self._submitted and consent < 3 and self._answer_consent(page):
                consent += 1
                page.wait_for_timeout(600)
                continue
            if self._login_form(st):
                if not self._submitted:
                    self._login(page)
                    self._submitted = True
                    submitted_at = time.monotonic()
                    page.wait_for_timeout(800)
                    continue
                waited = time.monotonic() - submitted_at
                if waited > 6 and st["alerts"]:
                    raise LoginFailed(_("Anmeldung abgelehnt: {a0}", a0=st["alerts"][0]))
                if waited > self.timeout:
                    raise LoginFailed(_("Die Anmeldung führt nicht weiter – Benutzername und Passwort prüfen."))
            elif not st["inbox"] and self._submitted:
                # logged in: the app shows its start page – open the postbox (before the login the app is
                # still on its way to the login form, so nothing is done then)
                if left_login is None:
                    left_login = time.monotonic()
                settled = time.monotonic() - left_login > 1.5
                if settled and not st["path"].startswith("/posteingang") and gone_to_inbox < 2:
                    gone_to_inbox += 1
                    page.goto(self.base + INBOX_PATH, wait_until="domcontentloaded")
                    continue
            page.wait_for_timeout(400)
        if st and APPROVAL.search(st.get("text") or "") and not st.get("password") and not st.get("items"):
            # also an approval prompt over the postbox page (an approval asked for pauses the timer)
            raise ApprovalRequired(_("finanzen.net zero verlangt eine Freigabe (Secure TAN) – der Abruf funktioniert nur mit einer Anmeldung, die keine verlangt."))
        if st.get("inbox") and not st.get("items"):
            raise FetchError(_("Im Posteingang wurden keine Einträge gefunden."))
        if st and (st.get("password") or st.get("user")) and self._submitted:
            raise LoginFailed(_("Die Anmeldung führt nicht weiter – Benutzername und Passwort prüfen."))
        raise FetchError(_("Der Posteingang wurde nicht erreicht (zuletzt auf {a0}).", a0=st.get("path") or "?"))

    def _login(self, page):
        if not self.login or not self.password:
            raise LoginFailed(_("Benutzername oder Passwort fehlt."))
        marked = page.evaluate(JS_MARK)
        user_sel = self.cfg.login_user_selector or '[data-gnubook-login="user"]'
        pw_sel = self.cfg.login_password_selector or '[data-gnubook-login="password"]'
        if self.cfg.login_user_selector or marked["user"]:
            page.locator(user_sel).first.fill(self.login)
        elif not marked["password"] and not self.cfg.login_password_selector:
            raise LoginFailed(_("Das Anmeldeformular wurde nicht erkannt ([postbox] login_*_selector setzen)."))
        if not marked["password"] and not self.cfg.login_password_selector:
            # two steps: the name first, then the password on the next page
            self._submit(page, marked)
            page.locator('input[type="password"]').first.wait_for(state="visible", timeout=self.timeout * 1000)
            marked = page.evaluate(JS_MARK)
        page.locator(pw_sel).first.fill(self.password)
        self._submit(page, marked)

    def _submit(self, page, marked):
        """Clicks the login button; if something covers it (e.g. a banner in a frame), Enter in the last field."""
        field_sel = (self.cfg.login_password_selector or '[data-gnubook-login="password"]') if marked.get("password") \
            else (self.cfg.login_user_selector or '[data-gnubook-login="user"]')
        if self.cfg.login_submit_selector or marked.get("submit"):
            try:
                page.locator(self.cfg.login_submit_selector or '[data-gnubook-login="submit"]').first.click(
                    timeout=min(10, self.timeout) * 1000)
                return
            except Exception as exc:  # noqa: BLE001 – fall back to the keyboard, but never send the login twice
                if "navigat" in str(exc).lower():
                    return  # clicked; only the page it started loads slowly
                try:
                    if not page.locator(field_sel).first.is_visible():
                        return  # the form is gone: the click went through
                except Exception:  # noqa: BLE001 – the page changed: the click went through
                    return
        page.locator(field_sel).first.press("Enter")

    def _answer_consent(self, page) -> bool:
        for pattern in CONSENT:
            buttons = page.get_by_role("button", name=pattern)
            try:
                for i in range(min(buttons.count(), 5)):
                    b = buttons.nth(i)
                    if b.is_visible() and b.evaluate(JS_IN_CONSENT):
                        b.click(timeout=5000)
                        return True
            except Exception:  # noqa: BLE001 – a banner that vanishes while clicking is fine
                continue
        return False

    def _logout(self, page):
        try:
            for role in ("button", "link", "menuitem"):
                loc = page.get_by_role(role, name=LOGOUT)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=3000)
                    page.wait_for_timeout(1000)
                    return
        except Exception:  # noqa: BLE001 – logging out is a courtesy
            pass

    # ------------------------------------------------------------------ list
    def _read(self, page) -> list[Listed]:
        today = datetime.now(TZ).date()
        out = []
        for raw in page.evaluate(JS_ITEMS, ITEM):
            out.append(Listed(external_id=str(raw.get("id") or "").strip(), title=raw.get("title") or "",
                              day=parse_day(raw.get("day"), today), href=raw.get("href") or "",
                              raw_day=raw.get("day") or ""))
        return out

    def _collect(self, page, stop: date | None) -> tuple[list[Listed], int, str]:
        self._settle(page)
        listed = self._read(page)
        if not listed:
            raise FetchError(_("Im Posteingang wurden keine Einträge gefunden."))
        if all(d.day is None for d in listed):
            # without readable days the list would be clicked through to its end and everything downloaded
            raise FetchError(_("Das Datum der Einträge ist nicht lesbar (z. B. „{a0}“) – Abruf abgebrochen.",
                               a0=listed[0].raw_day))
        seen = {d.external_id for d in listed}
        batch, clicks = listed, 0
        while True:
            if batch_done(batch, stop):
                return listed, clicks, "date"
            more = page.locator(MORE)
            if more.count() == 0 or not more.first.is_visible():
                return listed, clicks, "end"
            if clicks >= self.cfg.max_clicks:
                return listed, clicks, "limit"
            before = page.locator(ITEM).count()
            self._click(more.first)
            clicks += 1
            if not self._wait_growth(page, before):
                return listed, clicks, "stalled"
            batch = [d for d in self._read(page) if d.external_id not in seen]
            seen.update(d.external_id for d in batch)
            listed.extend(batch)

    def _click(self, locator):
        try:
            locator.scroll_into_view_if_needed(timeout=5000)
            locator.click(timeout=min(10, self.timeout) * 1000)
        except Exception:  # noqa: BLE001 – covered by an overlay: click it the way the app's own code would
            locator.evaluate("el => el.click()")

    def _wait_growth(self, page, before: int) -> bool:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if page.locator(ITEM).count() > before:
                self._settle(page)
                return True
            page.wait_for_timeout(250)
        return False

    def _settle(self, page, quiet: float = 0.6, limit: float = 5.0):
        """Waits until the number of entries has not changed for `quiet` seconds (the app renders in steps)."""
        end = time.monotonic() + limit
        last, since = page.locator(ITEM).count(), time.monotonic()
        while time.monotonic() < end:
            page.wait_for_timeout(200)
            n = page.locator(ITEM).count()
            if n != last:
                last, since = n, time.monotonic()
            elif time.monotonic() - since >= quiet:
                return

    # ------------------------------------------------------------------ download
    def _download_all(self, page, result: FetchResult, sink):
        in_a_row = 0
        for i, doc in enumerate(result.selected):
            try:
                data = self._download(page, doc)
            except FetchError as exc:
                result.failed.append((doc, str(exc)))
                # a document the server refuses on its own (404, 500, no PDF) must not keep the others from being
                # fetched; only failures that hit every download end the downloads of this run
                in_a_row = in_a_row + 1 if isinstance(exc, SessionLost) else 0
            except Exception as exc:  # noqa: BLE001 – the page may be gone (e.g. logged out by the app)
                result.failed.append((doc, _first_line(exc)))
                in_a_row += 1
            else:
                in_a_row = 0
                sink(doc, data)
                result.downloaded += 1
                page.wait_for_timeout(max(0, int(self.cfg.download_pause_ms)))
                continue
            if in_a_row >= MAX_FAILURES_IN_A_ROW:
                for rest in result.selected[i + 1:]:
                    result.failed.append((rest, _("nicht versucht – zu viele Fehler hintereinander")))
                return

    def _download(self, page, doc: Listed) -> bytes:
        url = urljoin(self.base + "/", doc.href)
        if urlsplit(url).netloc != urlsplit(self.base).netloc:
            raise FetchError(_("Der Link führt aus der Web-App heraus: {a0}", a0=doc.href))
        res = page.evaluate(JS_FETCH, [url, MAX_PDF, self.timeout * 1000])
        status = res.get("status")
        if res.get("error"):
            error = SessionLost if not status else FetchError  # no answer at all: connection or browser gone
            raise error(_("Download fehlgeschlagen: {a0}", a0=res["error"]))
        if status != 200:
            error = SessionLost if status in (401, 403, 419, 440) else FetchError
            raise error(_("Download fehlgeschlagen: HTTP {a0}", a0=status))
        data = base64.b64decode(res.get("data") or "")
        if not data.startswith(b"%PDF"):
            raise FetchError(_("Keine PDF-Datei erhalten ({a0}).", a0=res.get("type") or "?"))
        return data

    # ------------------------------------------------------------------ diagnostics
    def _note_request(self, request):
        """Remembers the app's own API calls (method and path) – they show how the postbox could be read without
        a browser later. Downloads are left out; query values and long numbers (customer, document) are
        replaced."""
        try:
            if request.resource_type not in ("xhr", "fetch"):
                return
            parts = urlsplit(request.url)
            if not parts.path.startswith("/api/") or "/doc/" in parts.path:
                return
            keys = sorted({p.split("=", 1)[0] for p in parts.query.split("&") if p})
            path = re.sub(r"\d{5,}", "{n}", parts.path)
            entry = f"{request.method} {path}" + (f"?{'&'.join(keys)}" if keys else "")
            if entry not in self.api_paths and len(self.api_paths) < 100:
                self.api_paths.append(entry)
        except Exception:  # noqa: BLE001
            pass

    def _write_api_paths(self):
        if self.debug_dir is None or not self.api_paths:
            return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / "api-paths.txt"
            path.write_text("\n".join(self.api_paths) + "\n", encoding="utf-8")
            path.chmod(0o600)
        except OSError:
            pass

    def _redact(self, text: str) -> str:
        pw = self.password or ""
        if not pw:
            return text
        attr = pw.replace("&", "&amp;").replace('"', "&quot;").replace("\u00a0", "&nbsp;")
        forms = {pw, html.escape(pw), html.escape(pw, quote=False), json.dumps(pw)[1:-1], quote(pw, safe=""),
                 quote_plus(pw), attr, attr.replace("<", "&lt;").replace(">", "&gt;"),
                 pw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\u00a0", "&nbsp;")}
        for form in sorted((f for f in forms if f), key=len, reverse=True):
            text = text.replace(form, "********")
        return text

    def _capture(self, page, label: str) -> str | None:
        if self.debug_dir is None:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            page.evaluate("() => document.querySelectorAll('input').forEach(e => {"
                          " if ((e.getAttribute('type') || '').toLowerCase() === 'password') e.value = '';"
                          " e.removeAttribute('value'); })")
            stem = self.debug_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-z_]', '', label) or 'fehler'}"
            page.screenshot(path=f"{stem}.png", full_page=True, timeout=15000)
            Path(f"{stem}.html").write_text(self._redact(page.content()), encoding="utf-8")
            for suffix in (".png", ".html"):
                Path(f"{stem}{suffix}").chmod(0o600)
            self._prune()
            return str(stem)
        except Exception as exc:  # noqa: BLE001 – diagnostics must not hide the real error
            log.warning("Postfach: Screenshot nicht möglich: %s", exc)
            return None

    def _prune(self):
        keep = max(1, int(self.cfg.debug_keep or 10))
        stems = sorted({p.with_suffix("") for p in self.debug_dir.glob("*.png")} |
                       {p.with_suffix("") for p in self.debug_dir.glob("*.html")})
        for stem in stems[:-keep]:
            for suffix in (".png", ".html"):
                Path(f"{stem}{suffix}").unlink(missing_ok=True)
