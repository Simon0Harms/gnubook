"""HTTP client for pp-core, the headless Portfolio Performance service (see ppcore/README.md).

Only the standard library is used. pp-core normally runs on the same host (127.0.0.1), proxies from the
environment are therefore ignored.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date


class PPCoreError(RuntimeError):
    """pp-core answered with an error or could not be reached."""

    def __init__(self, message: str, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def unreachable(self) -> bool:
        return self.status == 0


class PPCoreClient:
    def __init__(self, url: str, token: str, timeout: float = 60.0):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ------------------------------------------------------------------ transport
    def _request(self, method: str, path: str, body=None, params: dict | None = None, raw: bytes | None = None,
                 headers: dict | None = None, timeout: float | None = None, want_bytes: bool = False):
        url = f"{self.url}/api/v1{path}"
        if params:
            query = {k: v for k, v in params.items() if v not in (None, "")}
            if query:
                url += "?" + urllib.parse.urlencode(query)
        data = None
        hdrs = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if raw is not None:
            data = raw
            hdrs["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                payload = resp.read()
                if want_bytes:
                    return payload, dict(resp.headers)
                return json.loads(payload or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read() or b"{}")
            except ValueError:
                err = {}
            raise PPCoreError(err.get("message") or f"HTTP {exc.code}", exc.code, err.get("error", "")) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise PPCoreError(f"pp-core nicht erreichbar ({self.url}): {reason}") from None

    # ------------------------------------------------------------------ API
    def health(self) -> dict:
        return self._request("GET", "/health", timeout=10)

    def feeds(self) -> list[dict]:
        return self._request("GET", "/feeds").get("feeds", [])

    def summary(self, cid: str) -> dict:
        return self._request("GET", f"/clients/{cid}")

    def upload(self, cid: str, content: bytes, filename: str) -> dict:
        safe = "".join(ch for ch in (filename or "portfolio.xml") if ch.isprintable() and ord(ch) < 128) or "x.xml"
        return self._request("PUT", f"/clients/{cid}/file", raw=content, headers={"X-Filename": safe},
                             timeout=max(self.timeout, 120))

    def download(self, cid: str) -> tuple[bytes, str]:
        data, headers = self._request("GET", f"/clients/{cid}/file", want_bytes=True)
        disp = headers.get("Content-Disposition") or headers.get("Content-disposition") or ""
        name = disp.split("filename=", 1)[1].strip('"') if "filename=" in disp else "portfolio.xml"
        return data, name

    def create(self, cid: str, currency: str = "EUR", portfolio: str = "Depot",
               account: str = "Verrechnungskonto") -> dict:
        return self._request("POST", f"/clients/{cid}/create",
                             {"currency": currency, "portfolio": portfolio, "account": account})

    def demo(self, cid: str, start: date, months: int, seed: int = 7) -> dict:
        """Fictional demo file (replaces only an earlier demo file)."""
        return self._request("POST", f"/clients/{cid}/demo",
                             {"start": start.isoformat(), "months": months, "seed": seed})

    def export(self, cid: str, prices: str = "all") -> dict:
        """prices: 'all', 'none' or 'YYYY-MM-DD' (only prices from that day on)."""
        return self._request("GET", f"/clients/{cid}/export", params={"prices": prices},
                             timeout=max(self.timeout, 120))

    def import_pdfs(self, cid: str, files: list[tuple[str, bytes]], portfolio: str | None = None,
                    account: str | None = None, apply: bool = True, auto_feed: bool = True) -> dict:
        body = {"files": [{"name": name, "data": base64.b64encode(data).decode()} for name, data in files],
                "portfolio": portfolio, "account": account, "apply": apply, "autoFeed": auto_feed}
        return self._request("POST", f"/clients/{cid}/import", body, timeout=max(self.timeout, 300))

    def import_session(self, cid: str, session: str) -> dict:
        return self._request("GET", f"/clients/{cid}/import/{session}")

    def import_apply(self, cid: str, session: str, force=(), portfolio: str | None = None,
                     account: str | None = None, extractor: str | None = None, auto_feed: bool = True) -> dict:
        body = {"force": [int(i) for i in force], "autoFeed": auto_feed}
        if portfolio:
            body["portfolio"] = portfolio
        if account:
            body["account"] = account
        if extractor:
            body["extractor"] = extractor
        return self._request("POST", f"/clients/{cid}/import/{session}/apply", body,
                             timeout=max(self.timeout, 300))

    def quotes_start(self, cid: str, securities=(), wait: int = 0) -> dict:
        return self._request("POST", f"/clients/{cid}/quotes", {"securities": list(securities)},
                             params={"wait": wait or None}, timeout=max(self.timeout, wait + 30))

    def quotes_status(self, cid: str) -> dict:
        return self._request("GET", f"/clients/{cid}/quotes")

    def performance(self, cid: str, start: date, end: date, portfolio: str | None = None) -> dict:
        return self._request("GET", f"/clients/{cid}/performance",
                             params={"from": start.isoformat(), "to": end.isoformat(), "portfolio": portfolio},
                             timeout=max(self.timeout, 120))

    def holdings(self, cid: str, day: date | None = None) -> dict:
        return self._request("GET", f"/clients/{cid}/holdings",
                             params={"date": day.isoformat() if day else None}, timeout=max(self.timeout, 120))

    def search(self, cid: str, query: str) -> list[dict]:
        return self._request("GET", f"/clients/{cid}/search", params={"q": query}).get("results", [])

    def update_security(self, cid: str, uuid: str, fields: dict) -> dict:
        return self._request("PATCH", f"/clients/{cid}/securities/{urllib.parse.quote(uuid)}", fields)

    def delete_transaction(self, cid: str, uuid: str) -> dict:
        return self._request("DELETE", f"/clients/{cid}/transactions/{urllib.parse.quote(uuid)}")
