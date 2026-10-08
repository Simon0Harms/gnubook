"""Optional copy of the .gnucash backup into the user's own Nextcloud (WebDAV).

Every user can connect one Nextcloud account (server URL, login, app password – never the real password)
and choose, per book, a folder and file name there. After each backup run gnubook uploads the fresh
.gnucash file with a plain WebDAV PUT. Nextcloud writes uploads to a temporary .part file and renames it,
so the sync client never sees a half-written file, and the versions app keeps the older copies
(gnubook does not rotate remotely).

Only the standard library is used (urllib), so no new dependency is needed.
"""
from __future__ import annotations

from .i18n import gettext as _

import base64
import json
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

TIMEOUT = 60
DAV = "{DAV:}"
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class NextcloudError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------ validation

def normalize_server(url: str, allow_http: bool = False) -> str:
    """https://cloud.example.org[/sub] without trailing slash, index.php or remote.php."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise NextcloudError(_("Bitte die Adresse der Nextcloud angeben, z. B. https://cloud.example.org"))
    if parts.scheme == "http" and not allow_http:
        raise NextcloudError(_("Nur https-Adressen sind erlaubt ([nextcloud] allow_http in config.toml)."))
    if parts.username or parts.password or parts.query or parts.fragment:
        raise NextcloudError(_("Die Adresse darf keine Zugangsdaten, Parameter oder Anker enthalten."))
    path = re.sub(r"/(index\.php|remote\.php).*$", "", parts.path).rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


def clean_folder(folder: str) -> str:
    """'/Finanzen/GnuCash' style path inside the user's files; '' is the root folder."""
    segments = []
    for seg in (folder or "").replace("\\", "/").split("/"):
        seg = seg.strip()
        if not seg:
            continue
        if seg in (".", "..") or _CTRL.search(seg):
            raise NextcloudError(_("Ungültiger Ordnername: {a0}", a0=seg))
        segments.append(seg)
    return "/" + "/".join(segments) if segments else "/"


def clean_filename(name: str) -> str:
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", "..") or _CTRL.search(name):
        raise NextcloudError(_("Ungültiger Dateiname: {a0}", a0=name))
    if name.lower().endswith(".part"):
        raise NextcloudError(_("Dateinamen auf .part nimmt Nextcloud nicht an."))
    if not name.lower().endswith(".gnucash"):
        name += ".gnucash"
    return name


# ------------------------------------------------------------------------------------------ client

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # never resend credentials somewhere else
        return None


@dataclass
class Client:
    server: str
    login: str
    app_password: str
    user_id: str = ""  # WebDAV user id; may differ from the login name (e.g. e-mail login)
    timeout: float = TIMEOUT

    # ---------------------------------------------------------------- low level
    def _request(self, method: str, url: str, data=None, headers=None, ok=(200, 201, 204, 207)):
        h = {"Authorization": "Basic " + base64.b64encode(f"{self.login}:{self.app_password}".encode()).decode(),
             "OCS-APIRequest": "true", "User-Agent": "gnubook"}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if exc.code in ok:
                return exc.code, body
            raise NextcloudError(self._explain(exc.code, method))
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NextcloudError(_("Nextcloud nicht erreichbar: {a0}", a0=reason))

    @staticmethod
    def _explain(code: int, method: str) -> str:
        if code == 401:
            return _("Anmeldung an der Nextcloud abgelehnt (App-Passwort prüfen).")
        if code == 403:
            return _("Keine Schreibrechte im Zielordner.")
        if code == 404:
            return _("Nicht gefunden – Adresse der Nextcloud oder Ordner prüfen.")
        if code == 507:
            return _("Kein Speicherplatz mehr in der Nextcloud.")
        if 300 <= code < 400:
            return _("Nextcloud leitet um – bitte die endgültige Adresse (https) eintragen.")
        return _("Nextcloud antwortet mit HTTP {a0} ({a1}).", a0=code, a1=method)

    def _dav(self, path: str) -> str:
        segs = [quote(s, safe="") for s in path.split("/") if s]
        return f"{self.server}/remote.php/dav/files/{quote(self.user_id or self.login, safe='')}/" + "/".join(segs)

    # ---------------------------------------------------------------- API
    def whoami(self) -> dict:
        """Checks login and app password; returns {'id', 'display'}."""
        _status, body = self._request("GET", f"{self.server}/ocs/v2.php/cloud/user?format=json",
                                      headers={"Accept": "application/json"}, ok=(200,))
        try:
            data = json.loads(body)["ocs"]["data"]
        except (ValueError, KeyError, TypeError):
            raise NextcloudError(_("Unter dieser Adresse antwortet keine Nextcloud."))
        return {"id": data.get("id") or self.login, "display": data.get("display-name") or data.get("displayname") or ""}

    def ensure_folder(self, folder: str):
        path = ""
        for seg in [s for s in folder.split("/") if s]:
            path += "/" + seg
            # 201 created, 405 already there
            self._request("MKCOL", self._dav(path), ok=(201, 405))

    def upload(self, local: Path, folder: str, filename: str):
        self.ensure_folder(folder)
        with open(local, "rb") as fh:
            data = fh.read()
        self._request("PUT", self._dav(f"{folder}/{filename}"), data=data,
                      headers={"Content-Type": "application/octet-stream"}, ok=(201, 204))

    def delete(self, path: str):
        self._request("DELETE", self._dav(path), ok=(204, 404))

    def folders(self, folder: str) -> list[str]:
        """Names of the sub folders of `folder` (PROPFIND depth 1)."""
        body = (b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>')
        _status, xml = self._request("PROPFIND", self._dav(folder), data=body,
                                     headers={"Depth": "1", "Content-Type": "application/xml"}, ok=(207,))
        own = urlsplit(self._dav(folder)).path.rstrip("/")
        names = []
        for resp in ElementTree.fromstring(xml).iter(DAV + "response"):
            href = unquote(urlsplit(resp.findtext(DAV + "href") or "").path).rstrip("/")
            if href == unquote(own) or resp.find(f".//{DAV}resourcetype/{DAV}collection") is None:
                continue
            names.append(href.rsplit("/", 1)[-1])
        return sorted(names, key=str.lower)

    def test_write(self, folder: str):
        """Creates the folder, writes and removes a small probe file."""
        import tempfile

        probe = ".gnubook-schreibtest.txt"
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b"gnubook")
        try:
            self.ensure_folder(folder)
            self._request("PUT", self._dav(f"{folder}/{probe}"), data=Path(fh.name).read_bytes(), ok=(201, 204))
            self.delete(f"{folder}/{probe}")
        finally:
            Path(fh.name).unlink(missing_ok=True)

    def revoke(self):
        """Deletes the app password on the server (on disconnect). Errors are ignored by the caller."""
        self._request("DELETE", f"{self.server}/ocs/v2.php/core/apppassword", ok=(200,))


# ------------------------------------------------------------------------------------------ login flow v2

def login_flow_start(server: str, timeout: float = TIMEOUT) -> dict:
    """Returns {'login': url for the browser, 'token': poll token, 'endpoint': poll url}."""
    req = urllib.request.Request(f"{server}/index.php/login/v2", data=b"", method="POST",
                                 headers={"User-Agent": "gnubook"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return {"login": data["login"], "token": data["poll"]["token"], "endpoint": data["poll"]["endpoint"]}
    except urllib.error.HTTPError as exc:
        raise NextcloudError(_("Nextcloud antwortet mit HTTP {a0} ({a1}).", a0=exc.code, a1="login/v2"))
    except (urllib.error.URLError, OSError) as exc:
        raise NextcloudError(_("Nextcloud nicht erreichbar: {a0}", a0=getattr(exc, "reason", exc)))
    except (ValueError, KeyError, TypeError):
        raise NextcloudError(_("Unter dieser Adresse antwortet keine Nextcloud."))


def login_flow_poll(server: str, endpoint: str, token: str, timeout: float = 15) -> dict | None:
    """None while the user has not confirmed yet; then {'server', 'login', 'app_password'}."""
    if urlsplit(endpoint).netloc != urlsplit(server).netloc:  # only ever poll the server the user named
        raise NextcloudError(_("Unter dieser Adresse antwortet keine Nextcloud."))
    req = urllib.request.Request(endpoint, data=f"token={quote(token)}".encode(), method="POST",
                                 headers={"User-Agent": "gnubook",
                                          "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise NextcloudError(_("Nextcloud antwortet mit HTTP {a0} ({a1}).", a0=exc.code, a1="login/v2/poll"))
    except (urllib.error.URLError, OSError) as exc:
        raise NextcloudError(_("Nextcloud nicht erreichbar: {a0}", a0=getattr(exc, "reason", exc)))
    return {"server": data.get("server") or server, "login": data["loginName"], "app_password": data["appPassword"]}
