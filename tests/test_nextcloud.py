"""Copy of the .gnucash backup into the user's Nextcloud (against a small fake WebDAV server)."""
import base64
import json
import sqlite3
import threading
from datetime import date
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from gnubook.backup import BackupWriter
from gnubook.nextcloud import (Client, NextcloudError, clean_filename, clean_folder, normalize_server,
                               parse_share_link)
from gnubook.writer import SplitInput, TxInput, create_transaction

LOGIN, APP_PW, DAV_USER = "simon@example.org", "app-pw-123", "simon"
SHARE, SHARE_PW = "AbCdEf123456", "share-pw-1"


class FakeNextcloud(BaseHTTPRequestHandler):
    files: dict  # "/a/b.gnucash" -> bytes
    dirs: set
    flow_done: bool = False
    readonly: set = set()

    def log_message(self, *args):
        pass

    legacy_only = False

    def _basic(self, user, pw):
        return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()

    def _auth(self):
        path = urlsplit(self.path).path
        if path.startswith("/public.php/dav/"):
            if self.legacy_only:
                self._send(404)
                return False
            expected = self._basic("anonymous", SHARE_PW)
        elif path.startswith("/public.php/webdav"):
            expected = self._basic(SHARE, SHARE_PW)
        else:
            expected = self._basic(LOGIN, APP_PW)
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    PREFIXES = (f"/remote.php/dav/files/{DAV_USER}", f"/public.php/dav/files/{SHARE}", "/public.php/webdav")

    def _prefix(self):
        p = unquote(urlsplit(self.path).path)
        return next(x for x in self.PREFIXES if p.startswith(x))

    def _path(self):
        p = unquote(urlsplit(self.path).path)
        pre = self._prefix()
        # a share sees only its folder: map into /Geteilt/gnubook
        inner = "/" + p[len(pre):].strip("/")
        if pre.startswith("/public.php"):
            inner = ("/Share" + inner).rstrip("/") if inner != "/" else "/Share"
        return inner

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self):
        if not self._auth():
            return
        if self.path.startswith("/ocs/v2.php/cloud/user"):
            self._send(200, json.dumps({"ocs": {"data": {"id": DAV_USER, "display-name": "Simon"}}}).encode())
        else:
            self._send(404)

    def do_POST(self):
        body = self._body()
        if self.path == "/index.php/login/v2":
            host = f"http://{self.headers['Host']}"
            self._send(200, json.dumps({"poll": {"token": "tok", "endpoint": f"{host}/login/v2/poll"},
                                        "login": f"{host}/login/v2/flow/abc"}).encode())
        elif self.path == "/login/v2/poll":
            assert parse_qs(body.decode())["token"] == ["tok"]
            if not type(self).flow_done:
                self._send(404)
            else:
                self._send(200, json.dumps({"server": "x", "loginName": LOGIN, "appPassword": APP_PW}).encode())
        else:
            self._send(404)

    def do_MKCOL(self):
        if not self._auth():
            return
        p = self._path()
        if p in self.dirs:
            return self._send(405)
        self.dirs.add(p)
        self._send(201)

    def do_PUT(self):
        if not self._auth():
            return
        p = self._path()
        data = self._body()
        parent = p.rsplit("/", 1)[0] or "/"
        if parent in self.readonly:
            return self._send(403)
        if parent not in self.dirs:
            return self._send(409)
        existed = p in self.files
        self.files[p] = data
        self._send(204 if existed else 201)

    def do_DELETE(self):
        if self.path.startswith("/ocs/v2.php/core/apppassword"):
            return self._send(200, b"{}")
        if not self._auth():
            return
        self.files.pop(self._path(), None)
        self._send(204)

    def do_PROPFIND(self):
        if not self._auth():
            return
        self._body()
        p = self._path()
        base = self._prefix()
        shown = (lambda e: e[len("/Share"):] or "/") if base.startswith("/public.php") else (lambda e: e)
        entries = [p] + sorted(d for d in self.dirs if d != p and (d.rsplit("/", 1)[0] or "/") == p)
        xml = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
        for e in entries:
            xml += (f"<d:response><d:href>{base}{shown(e).rstrip('/')}/</d:href><d:propstat><d:prop><d:resourcetype>"
                    "<d:collection/></d:resourcetype></d:prop></d:propstat></d:response>")
        xml += "</d:multistatus>"
        self._send(207, xml.encode(), "application/xml")


@pytest.fixture
def nc():
    handler = type("H", (FakeNextcloud,), {"files": {}, "dirs": {"/", "/Share"}, "flow_done": False,
                                         "readonly": set(), "legacy_only": False})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    handler.url = f"http://127.0.0.1:{server.server_port}"
    yield handler
    server.shutdown()


def _client(nc):
    return Client(nc.url, LOGIN, APP_PW, DAV_USER)


def test_validation():
    assert normalize_server("cloud.example.org/") == "https://cloud.example.org"
    assert normalize_server("https://x.org/nc/index.php/apps/files") == "https://x.org/nc"
    with pytest.raises(NextcloudError):
        normalize_server("http://x.org")
    assert normalize_server("http://x.org", allow_http=True) == "http://x.org"
    with pytest.raises(NextcloudError):
        normalize_server("https://u:p@x.org")
    assert clean_folder("Finanzen//GnuCash/") == "/Finanzen/GnuCash"
    assert clean_folder("") == "/"
    for bad in ("/a/../b", "a/./b", "a\x00b"):
        with pytest.raises(NextcloudError):
            clean_folder(bad)
    assert clean_filename("Haushalt") == "Haushalt.gnucash"
    assert clean_filename("Bücher 2026.gnucash") == "Bücher 2026.gnucash"
    for bad in ("", "a/b", "..", "x.part"):
        with pytest.raises(NextcloudError):
            clean_filename(bad)


def test_client_upload_and_folders(nc, tmp_path):
    c = _client(nc)
    assert c.whoami() == {"id": DAV_USER, "display": "Simon"}
    f = tmp_path / "x.gnucash"
    f.write_bytes(b"one")
    c.upload(f, "/Finanzen/Gnu Cash", "Haushalt ä.gnucash")
    assert nc.files["/Finanzen/Gnu Cash/Haushalt ä.gnucash"] == b"one"
    f.write_bytes(b"two")
    c.upload(f, "/Finanzen/Gnu Cash", "Haushalt ä.gnucash")  # overwrite in place (keeps Nextcloud versions)
    assert nc.files["/Finanzen/Gnu Cash/Haushalt ä.gnucash"] == b"two"
    assert c.folders("/") == ["Finanzen", "Share"]
    assert c.folders("/Finanzen") == ["Gnu Cash"]
    c.test_write("/Neu")
    assert "/Neu" in nc.dirs and not any(k.startswith("/Neu/") for k in nc.files)
    with pytest.raises(NextcloudError, match="Passwort"):
        Client(nc.url, LOGIN, "falsch", DAV_USER).whoami()
    nc.readonly.add("/Geteilt")
    nc.dirs.add("/Geteilt")
    with pytest.raises(NextcloudError, match="Schreibrechte"):
        c.test_write("/Geteilt")


def test_backup_writer_uploads_without_local_file(state, nc, tmp_path):
    target = {"user_id": 1, "kind": "account", "legacy": 0, "server": nc.url, "login": LOGIN, "app_password": APP_PW, "dav_user": DAV_USER,
              "folder": "/gnubook", "filename": "buch.gnucash"}
    bw = BackupWriter(state.book, "", keep=3, delay=0, remote=lambda: [target], work_dir=tmp_path / "work")
    bw.run_now()
    assert bw.remote_status[1]["error"] is None and bw.remote_status[1]["ok"]
    data = nc.files["/gnubook/buch.gnucash"]
    out = tmp_path / "check.gnucash"
    out.write_bytes(data)
    with sqlite3.connect(out) as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] > 5
    assert not list((tmp_path / "work").iterdir())  # temporary copy removed

    bad = dict(target, app_password="falsch", user_id=2)
    bw.remote = lambda: [bad, target]
    bw.run_now()
    assert bw.remote_status[2]["error"] and bw.remote_status[1]["error"] is None


def _settings(client):
    return client.get("/settings").text


def test_web_flow(app, client, state, nc):
    reg = app.extensions["gnubook"]
    reg.cfg.nextcloud.allow_http = True
    assert "Sicherung in meine Nextcloud" in _settings(client)

    # login flow v2
    r = client.post("/settings/nextcloud/flow", data={"server": nc.url}, headers={"X-CSRF-Token": client.csrf})
    assert r.json["login"].endswith("/login/v2/flow/abc")
    r = client.post("/settings/nextcloud/flow/poll", headers={"X-CSRF-Token": client.csrf})
    assert r.json == {"status": "pending"}
    nc.flow_done = True
    r = client.post("/settings/nextcloud/flow/poll", headers={"X-CSRF-Token": client.csrf})
    assert r.json == {"status": "done"}
    acc = reg.system.nextcloud_account(1)
    assert acc["dav_user"] == DAV_USER and acc["kind"] == "account"
    assert acc["app_password"].startswith("enc:v1:") and APP_PW not in acc["app_password"]  # encrypted at rest
    assert reg.system.nextcloud_password(acc) == APP_PW
    assert "Verbunden als" in _settings(client)

    # folder picker
    nc.dirs.add("/Finanzen")
    assert client.get("/settings/nextcloud/folders?path=/").json["folders"] == ["Finanzen", "Share"]
    assert client.get("/settings/nextcloud/folders?path=/../x").status_code == 400

    # target: write test, then upload after a change
    r = client.post("/settings/nextcloud/target", data={"csrf_token": client.csrf, "folder": "/Finanzen/GnuCash",
                                                          "filename": "Haushalt", "enabled": "1"})
    assert r.status_code == 302
    t = reg.system.nextcloud_target(1, state.id)
    assert (t["folder"], t["filename"], t["enabled"]) == ("/Finanzen/GnuCash", "Haushalt.gnucash", 1)
    state.backup.delay = 0
    idx = state.book.load_accounts()
    create_transaction(state.book, idx, TxInput(date(2026, 10, 3), "Nextcloud-Test", [
        SplitInput(idx.find("Aktiva:Barvermögen:Girokonto Musterbank").guid, D("-1")),
        SplitInput(idx.find("Aufwendungen:Lebensmittel").guid, D("1"))]))
    state.backup.run_now()
    assert "/Finanzen/GnuCash/Haushalt.gnucash" in nc.files
    assert "Zuletzt hochgeladen" in _settings(client)

    # invalid folder is refused and nothing changes
    client.post("/settings/nextcloud/target", data={"csrf_token": client.csrf, "folder": "/a/../b",
                                                     "filename": "x", "enabled": "1"})
    assert reg.system.nextcloud_target(1, state.id)["folder"] == "/Finanzen/GnuCash"

    # user loses access to the book -> no more upload for them
    assert len(reg.nextcloud_uploads(state.id)) == 1
    reg.system.set_book_users(state.id, [])
    assert reg.nextcloud_uploads(state.id) == []
    reg.system.set_book_users(state.id, [1])

    # disconnect removes account and targets
    client.post("/settings/nextcloud/disconnect", data={"csrf_token": client.csrf})
    assert reg.system.nextcloud_account(1) is None and reg.system.nextcloud_target(1, state.id) is None


def test_manual_connect_and_https_only(app, client, nc):
    reg = app.extensions["gnubook"]
    client.post("/settings/nextcloud/connect", data={"csrf_token": client.csrf, "server": nc.url,
                                                      "login": LOGIN, "app_password": APP_PW})
    assert reg.system.nextcloud_account(1) is None  # http refused by default
    reg.cfg.nextcloud.allow_http = True
    client.post("/settings/nextcloud/connect", data={"csrf_token": client.csrf, "server": nc.url,
                                                      "login": LOGIN, "app_password": "falsch"})
    assert reg.system.nextcloud_account(1) is None
    client.post("/settings/nextcloud/connect", data={"csrf_token": client.csrf, "server": nc.url,
                                                      "login": LOGIN, "app_password": APP_PW})
    assert reg.system.nextcloud_account(1)["dav_user"] == DAV_USER


def test_disabled_in_config(app, client):
    app.extensions["gnubook"].cfg.nextcloud.enabled = False
    assert "Sicherung in meine Nextcloud" not in _settings(client)
    r = client.post("/settings/nextcloud/connect", data={"csrf_token": client.csrf, "server": "https://x"})
    assert r.status_code == 404


def test_parse_share_link():
    assert parse_share_link("https://cloud.example.org/s/AbCdEf123456") == ("https://cloud.example.org", SHARE)
    assert parse_share_link("https://x.org/nc/index.php/s/AbCdEf123456/") == ("https://x.org/nc", SHARE)
    for bad in ("https://x.org/apps/files", "https://x.org/s/a"):
        with pytest.raises(NextcloudError):
            parse_share_link(bad)


@pytest.mark.parametrize("legacy", [False, True])
def test_share_link_only_reaches_its_folder(app, client, state, nc, legacy):
    reg = app.extensions["gnubook"]
    reg.cfg.nextcloud.allow_http = True
    nc.legacy_only = legacy
    client.post("/settings/nextcloud/share", data={"csrf_token": client.csrf, "link": f"{nc.url}/s/{SHARE}",
                                                    "share_password": "falsch"})
    assert reg.system.nextcloud_account(1) is None
    client.post("/settings/nextcloud/share", data={"csrf_token": client.csrf, "link": f"{nc.url}/s/{SHARE}",
                                                    "share_password": SHARE_PW})
    acc = reg.system.nextcloud_account(1)
    assert (acc["kind"], acc["login"], bool(acc["legacy"])) == ("share", SHARE, legacy)
    assert SHARE_PW not in acc["app_password"]
    page = client.get("/settings").text
    assert "freigegebenen Ordner" in page and 'value="/"' in page  # default target: the shared folder itself

    client.post("/settings/nextcloud/target", data={"csrf_token": client.csrf, "folder": "/Jahre",
                                                     "filename": "Haushalt", "enabled": "1"})
    state.backup.run_now()
    assert "/Share/Jahre/Haushalt.gnucash" in nc.files  # inside the shared folder only
    assert client.get("/settings/nextcloud/folders?path=/").json["folders"] == ["Jahre"]
    client.post("/settings/nextcloud/disconnect", data={"csrf_token": client.csrf})
    assert reg.system.nextcloud_account(1) is None


def test_plaintext_passwords_are_migrated_and_key_change_is_reported(app, state, nc):
    reg = app.extensions["gnubook"]
    with reg.system.conn() as c:  # row as written by the first Nextcloud version
        c.execute("INSERT INTO nextcloud_accounts (user_id, server, login, app_password, dav_user, created_at) "
                  "VALUES (1, ?, ?, ?, ?, 'x')", (nc.url, LOGIN, APP_PW, DAV_USER))
    reg.system.set_nextcloud_target(1, state.id, "/gnubook", "b.gnucash")
    assert reg.system.encrypt_nextcloud_passwords() == 1
    assert reg.system.nextcloud_account(1)["app_password"].startswith("enc:v1:")
    assert reg.nextcloud_uploads(state.id)[0]["app_password"] == APP_PW

    from gnubook.crypto import SecretBox
    reg.system.box = SecretBox("anderer-schluessel-" + "x" * 30)
    item = reg.nextcloud_uploads(state.id)[0]
    assert item["error"] and item["app_password"] == ""
    state.backup.run_now()
    assert "entschlüsseln" in state.backup.remote_status[1]["error"]


def test_backup_writer_copies_the_pp_file_next_to_the_book(state, nc, tmp_path):
    target = {"user_id": 1, "kind": "account", "legacy": 0, "server": nc.url, "login": LOGIN,
              "app_password": APP_PW, "dav_user": DAV_USER, "folder": "/gnubook", "filename": "Hauptbuch.gnucash"}
    pp = [(".xml", b"<client>1</client>")]
    bw = BackupWriter(state.book, str(tmp_path / "buch.gnucash"), keep=3, delay=3600, remote=lambda: [target],
                      work_dir=tmp_path / "work", extras=lambda: pp)  # the background thread stays out
    bw.request()
    bw.flush()  # book changed: .gnucash copy and PP file
    assert nc.files["/gnubook/Hauptbuch-PP.xml"] == b"<client>1</client>" and "/gnubook/Hauptbuch.gnucash" in nc.files
    versions = len(list(tmp_path.glob("buch*.gnucash")))
    pp[0] = (".xml", b"<client>2</client>")
    nc.files.pop("/gnubook/Hauptbuch.gnucash")
    bw.request_extras()
    bw.flush()  # only the PP file changed: no new .gnucash version
    assert nc.files["/gnubook/Hauptbuch-PP.xml"] == b"<client>2</client>"
    assert "/gnubook/Hauptbuch.gnucash" not in nc.files and len(list(tmp_path.glob("buch*.gnucash"))) == versions

    def broken():
        raise OSError("pp-core nicht erreichbar")
    bw.extras = broken
    bw.request()
    bw.flush()  # the book copy still goes up
    assert "/gnubook/Hauptbuch.gnucash" in nc.files and bw.remote_status[1]["error"] is None
