"""A small stand-in for the finanzen.net zero web app, for the browser tests of the postbox fetcher.

It mimics what the fetcher relies on: a login page with a cookie banner, a session cookie, a start page after the
login, and the postbox (/posteingang/inbox) with the same markup as the real one (data-zid attributes, ten
entries per "Mehr anzeigen", newest day first and rising ids within a day). PDFs are served below
/api/posteingang/<customer>/doc/<id>/<name>.pdf and need the session cookie.

Modes: "normal", "twostep" (user name first, then password), "approval" (the login waits for an app approval),
"dialog" (a dialog of the app over the postbox that must never be answered), "rootstart" (after the login the
start page is "/" and has text fields), "approval_inbox" (the postbox asks for an approval instead of listing).
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CUSTOMER = "1483210"
USER, PASSWORD = "simon@example.org", "geheim-123"
PAGE_SIZE = 10
TITLES = ("Wertpapierabrechnung: Kauf DE0008430026", "Steuerausgleichsrechnung",
          "Tageskontoauszug: EUR-Konto LU0908500753, DE0008430026")


def pdf_bytes(doc_id: str) -> bytes:
    return (b"%PDF-1.4\n% gnubook test document " + doc_id.encode() + b"\n1 0 obj << /Type /Catalog >> endobj\n"
            b"trailer << /Root 1 0 R >>\n%%EOF\n")


def make_docs(newest: date, days: int, per_day: int = 2) -> list[dict]:
    """Newest day first; within a day the ids rise (like the real postbox)."""
    docs = []
    for i in range(days):
        day = newest - timedelta(days=i)
        base = 246000000 + (day - date(2026, 1, 1)).days * 1000
        for k in range(per_day):
            docs.append({"id": str(base + k), "title": TITLES[(i + k) % len(TITLES)], "day": day})
    return docs


def sort_docs(docs: list[dict]) -> list[dict]:
    return sorted(docs, key=lambda d: (-d["day"].toordinal(), int(d["id"])))


LOGIN_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><title>finanzen.net zero</title>
<style>#consent{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center}
#consent div{background:#fff;padding:20px}</style></head><body>
<header><input data-zid="zero-input-field" type="text" placeholder="Suche"></header>
<h1>Login</h1>
<form id="f" onsubmit="return false">
  <div id="step1"><label for="u">E-Mail</label><input data-zid="zero-input-field" id="u" type="text" name="username"></div>
  <div id="step2" style="display:__STEP2__"><label for="p">Passwort</label><input data-zid="zero-input-field" id="p" type="password" name="password"></div>
  <button id="go" type="submit">__BUTTON__</button>
</form>
<div id="msg"></div>
<div id="consent" role="dialog"><div><p>Wir verwenden Cookies.</p>
  <button type="button" onclick="document.getElementById('consent').remove()">Alle akzeptieren</button>
  <button type="button" onclick="document.getElementById('consent').remove()">Nur notwendige Cookies</button></div></div>
<script>
const twostep = __TWOSTEP__;
document.getElementById('go').addEventListener('click', async () => {
  const u = document.getElementById('u').value, p = document.getElementById('p').value;
  if (twostep && document.getElementById('step2').style.display === 'none') {
    if (!u) return;
    setTimeout(() => { document.getElementById('step1').style.display = 'none';
                       document.getElementById('step2').style.display = '';
                       document.getElementById('go').textContent = 'Anmelden'; }, 300);
    return;
  }
  const r = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                       body: JSON.stringify({user: u, password: p})});
  const data = await r.json();
  if (r.ok) { location.href = data.next; }
  else { document.getElementById('msg').innerHTML = '<div role="alert">Benutzername oder Passwort falsch.</div>'; }
});
</script></body></html>"""

START_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><title>Übersicht</title></head><body>
<input data-zid="zero-input-field" type="text" placeholder="Suche">
<h1>Übersicht</h1><p>Zum Handeln die Session in der Secure TAN App freigeben.</p>
<nav><a href="/posteingang/inbox">Postfach</a> <button type="button" onclick="logout()">Abmelden</button></nav>
<script>
fetch('/api/me').then(r => { if (r.status === 401) location.href = '/'; });
async function logout() { await fetch('/api/logout', {method: 'POST'}); location.href = '/'; }
</script></body></html>"""

APP_DIALOG = """<div role="dialog" aria-modal="true" id="terms" style="position:fixed;inset:0;background:#fff">
<p>Geänderte Sonderbedingungen</p>
<button type="button" onclick="fetch('/api/decision', {method: 'POST', body: 'ablehnen'})">Ablehnen</button>
<button type="button" onclick="fetch('/api/decision', {method: 'POST', body: 'zustimmen'})">Zustimmen</button></div>"""

ROOT_START_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><title>Übersicht</title></head><body>
<input type="text" placeholder="Name, WKN oder ISIN"> <label>Kurs-Alarm an <input type="email" name="alarm"></label>
<h1>Willkommen zurück</h1><a href="/posteingang/inbox">Postfach</a></body></html>"""

APPROVAL_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><title>Freigabe</title></head><body>
<h1>Freigabe erforderlich</h1><p>Bitte bestätige die Anmeldung in der Secure TAN App.</p></body></html>"""

INBOX_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><title>Posteingang</title></head><body>
<header><button type="button" onclick="fetch('/api/logout', {method: 'POST'}).then(() => location.href = '/')">Abmelden</button></header>
<div class="container"><div data-zid="post-box-page" class="post-box-page" id="page" style="display:none">
  <h1 data-zid="post-box-headline">Posteingang</h1>
  <section data-zid="post-box-card"><div data-zid="post-box-groups" id="groups"></div>
    <div data-zid="post-box-footer"><a data-zid="post-box-more-link" id="more" class="link-secondary cursor-pointer">
      Mehr anzeigen <span class="sorter-after-flat"></span></a></div></section></div></div>
<script>
let page = 0;
function item(d) {
  return '<zero-navigation-action-list-item data-zid="post-box-item" target="_blank" class="post-box-page__item--unread"' +
    ' data-post-id="' + d.id + '"><div class="item-container default with-divider"><a data-zid="zero-navigation-action-list-item-anchor"' +
    ' class="item-link" href="' + d.href + '" target="_blank" rel="noopener noreferrer"><span data-zid="zero-navigation-action-list-item-content"' +
    ' class="content"><span data-zid="zero-navigation-action-list-item-label" class="label"><!----> ' + d.title + ' <!----></span>' +
    '<span data-zid="zero-navigation-action-list-item-subtitle" class="subtitle">' + d.day + '</span></span></a></div>' +
    '</zero-navigation-action-list-item>';
}
async function load() {
  const r = await fetch('/api/posteingang/__CUSTOMER__?page=' + page);
  if (r.status === 401) { location.href = '/'; return; }
  if (r.status === 409) { document.body.insertAdjacentHTML('beforeend',
      '<div role="dialog">Bitte gib die Anmeldung in der Secure TAN App frei.</div>'); return; }
  if (r.status === 403) { location.href = '/freigabe'; return; }
  const data = await r.json();
  const g = document.getElementById('groups');
  g.insertAdjacentHTML('beforeend', '<section data-zid="post-box-group">' + data.items.map(item).join('') + '</section>');
  document.getElementById('page').style.display = '';
  if (data.last) document.getElementById('more').remove();
  page += 1;
}
document.getElementById('more').addEventListener('click', () => setTimeout(load, __DELAY__));
load();
</script></body></html>"""


class FzeroHandler(BaseHTTPRequestHandler):
    # set per server (see serve())
    docs: list
    sessions: set
    mode: str = "normal"
    fail_ids: set
    delay_ms: int = 150
    logins: list
    day_format: str = "%d.%m.%y"
    decisions: list
    fail_status: int = 500
    expire_after: int = 0  # > 0: the session ends after this many downloads (like a session time limit)
    served: int = 0

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes | str = b"", ctype: str = "text/html; charset=utf-8", headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj, headers=None):
        self._send(code, json.dumps(obj), "application/json", headers)

    def _session(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid" and v in self.sessions:
                return v
        return None

    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        cls = type(self)
        if path == "/" and cls.mode == "rootstart" and self._session():
            return self._send(200, ROOT_START_PAGE)
        if path == "/":
            twostep = cls.mode == "twostep"
            page = (LOGIN_PAGE.replace("__TWOSTEP__", "true" if twostep else "false")
                    .replace("__STEP2__", "none" if twostep else "block")
                    .replace("__BUTTON__", "Weiter" if twostep else "Anmelden"))
            return self._send(200, page)
        if path == "/uebersicht":
            return self._send(200, START_PAGE)
        if path == "/freigabe":
            return self._send(200, APPROVAL_PAGE)
        if path == "/posteingang/inbox":
            page = INBOX_PAGE.replace("__CUSTOMER__", CUSTOMER).replace("__DELAY__", str(cls.delay_ms))
            if cls.mode == "dialog":
                page = page.replace("</body>", APP_DIALOG + "</body>")
            return self._send(200, page)
        if path == "/api/me":
            if cls.mode == "approval":
                return self._json(403, {"error": "approval"})
            return self._json(200 if self._session() else 401, {})
        if path == f"/api/posteingang/{CUSTOMER}":
            if cls.mode == "approval":
                return self._json(403, {"error": "approval"})
            if cls.mode == "approval_inbox" and self._session():
                return self._json(409, {"error": "approval"})
            if not self._session():
                return self._json(401, {"error": "login"})
            page = int((parse_qs(parts.query).get("page") or ["0"])[0])
            docs = sort_docs(cls.docs)
            chunk = docs[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
            items = [{"id": d["id"], "title": d["title"], "day": d["day"].strftime(cls.day_format),
                      "href": f"/api/posteingang/{CUSTOMER}/doc/{d['id']}/{d['title'].split(':')[0]}.pdf"}
                     for d in chunk]
            return self._json(200, {"items": items, "last": (page + 1) * PAGE_SIZE >= len(docs)})
        if path.startswith(f"/api/posteingang/{CUSTOMER}/doc/"):
            if not self._session():
                return self._json(401, {"error": "login"})
            doc_id = path.split("/")[5]
            if doc_id in cls.fail_ids:
                return self._json(cls.fail_status, {"error": "kaputt"})
            cls.served += 1
            if cls.expire_after and cls.served % cls.expire_after == 0:
                cls.sessions.clear()  # this was the last download of the session
            return self._send(200, pdf_bytes(doc_id), "application/pdf")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        cls = type(self)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/login":
            data = json.loads(body or b"{}")
            cls.logins.append(data.get("user"))
            if data.get("user") != USER or data.get("password") != PASSWORD:
                return self._json(401, {"error": "credentials"})
            sid = secrets.token_hex(8)
            cls.sessions.add(sid)
            nxt = {"approval": "/freigabe", "rootstart": "/"}.get(cls.mode, "/uebersicht")
            return self._json(200, {"next": nxt}, {"Set-Cookie": f"sid={sid}; Path=/; HttpOnly; SameSite=Lax"})
        if self.path == "/api/decision":
            cls.decisions.append(body.decode())
            return self._json(200, {})
        if self.path == "/api/logout":
            sid = self._session()
            cls.sessions.discard(sid)
            return self._json(200, {})
        return self._send(404, "not found", "text/plain")


def serve(docs: list[dict], mode: str = "normal"):
    """(server, handler class, base url); stop with server.shutdown()."""
    handler = type("H", (FzeroHandler,), {"docs": list(docs), "sessions": set(), "mode": mode,
                                         "fail_ids": set(), "logins": [], "decisions": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler, f"http://127.0.0.1:{server.server_port}"
