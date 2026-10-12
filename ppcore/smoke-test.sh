#!/usr/bin/env bash
# Smoke test of pp-core against a Portfolio Performance installation: build, start without UI, call the API.
#
#   ppcore/smoke-test.sh PP_DIR        # e.g. /opt/gnubook/pp/current/portfolio or an unpacked release
#
# Works in a temporary directory on port 18091 (PPCORE_TEST_PORT); needs a JDK 21, curl and python3.
# Used by CI to notice when a Portfolio Performance release breaks pp-core.
set -euo pipefail

PP_DIR=$(cd "${1:?usage: smoke-test.sh PP_DIR}" && pwd)
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PORT=${PPCORE_TEST_PORT:-18091}
WORK=$(mktemp -d)
PID=""
cleanup() {
  if [ -n "$PID" ]; then kill "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT

bash "$SRC/build.sh" "$PP_DIR" "$WORK/ppcore.jar"
bash "$SRC/configure.sh" "$PP_DIR" "$WORK/ppcore.jar" "$WORK/conf"
TOKEN=smoke-test-token-0123456789
printf 'bind=127.0.0.1\nport=%s\ntoken=%s\ndata_dir=%s\n' "$PORT" "$TOKEN" "$WORK/data" > "$WORK/ppcore.properties"
PPCORE_PP_DIR="$PP_DIR" PPCORE_CONF_DIR="$WORK/conf" PPCORE_CONFIG="$WORK/ppcore.properties" \
  PPCORE_WORKSPACE="$WORK/data/workspace" bash "$SRC/ppcore-run" > "$WORK/ppcore.log" 2>&1 &
PID=$!

if ! python3 - "$PORT" "$TOKEN" "$PID" <<'PY'; then
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

port, token, pid = sys.argv[1], sys.argv[2], int(sys.argv[3])
base = f"http://127.0.0.1:{port}/api/v1"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(method, path, body=None, expect=200, auth=True):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with opener.open(req, timeout=120) as r:
            status, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    assert status == expect, f"{method} {path}: {status} {payload[:300]!r}"
    return json.loads(payload) if payload[:1] in (b"{", b"[") else payload


for _ in range(90):
    try:
        os.kill(pid, 0)
    except OSError:
        sys.exit("pp-core ended during start")
    try:
        health = call("GET", "/health")
        break
    except (OSError, AssertionError):
        time.sleep(2)
else:
    sys.exit("pp-core did not answer")
print("health:", health)
call("GET", "/health", expect=401, auth=False)

feeds = {f["id"] for f in call("GET", "/feeds")["feeds"]}
assert {"MANUAL", "YAHOO"} <= feeds, feeds  # PP's quote feeds are found inside OSGi
summary = call("POST", "/clients/smoke/create", {"currency": "EUR", "portfolio": "Depot", "account": "Konto"},
               expect=201)
assert summary["exists"] and summary["portfolios"] == 1 and summary["accounts"] == 1, summary
export = call("GET", "/clients/smoke/export?prices=all")
assert export["client"]["baseCurrency"] == "EUR" and len(export["portfolios"]) == 1, export["client"]
perf = call("GET", "/clients/smoke/performance?from=2024-01-01&to=2024-12-31")
assert "ttwror" in perf and "categories" in perf, list(perf)
holdings = call("GET", "/clients/smoke/holdings")
assert "positions" in holdings, list(holdings)
pdf = base64.b64encode(b"%PDF-1.4\n% not a real statement\n").decode()
result = call("POST", "/clients/smoke/import", {"files": [{"name": "x.pdf", "data": pdf}], "apply": False})
assert result["fileErrors"] and not result["items"], result
demo = call("POST", "/clients/demo/demo", {"start": "2025-01-01", "months": 14}, expect=201)
assert demo["securities"] == 2 and demo["transactions"] > 20, demo
dexp = call("GET", "/clients/demo/export?prices=none")
kinds = {t["type"] for t in dexp["transactions"]}
assert {"BUY", "SELL", "DIVIDENDS", "DEPOSIT", "FEES"} <= kinds, kinds
call("POST", "/clients/smoke/demo", {}, expect=409)  # never over a real file
dperf = call("GET", "/clients/demo/performance?from=2025-01-01&to=2025-12-31")
assert dperf["ttwror"] is not None, dperf
# manual deliveries
port = summary["portfolioList"][0]["uuid"]
inb = call("POST", "/clients/smoke/transactions", {"type": "DELIVERY_INBOUND", "portfolio": port, "isin": "IE00BKM4GZ66",
           "name": "Test EM IMI", "date": "2023-01-02", "shares": "20", "amount": "500.00", "fees": "1.50"},
           expect=201)
assert float(inb["amount"]) == 500 and inb["shares"] == "20", inb
call("POST", "/clients/smoke/transactions", {"type": "DELIVERY_OUTBOUND", "isin": "IE00BKM4GZ66",
     "date": "2023-10-04", "shares": "21", "amount": "0"}, expect=409)
call("POST", "/clients/smoke/transactions", {"type": "DELIVERY_OUTBOUND", "isin": "IE00BKM4GZ66",
     "date": "2023-10-04", "shares": "20"}, expect=400)  # no price, no value
out = call("POST", "/clients/smoke/transactions", {"type": "DELIVERY_OUTBOUND", "security": inb["security"],
           "date": "2023-10-04", "shares": "20", "amount": "520", "note": "Depotauslieferung"}, expect=201)
# outbound of a security that is not in the file yet: refused without force and nothing added, then created
before = call("GET", "/clients/smoke")["securities"]
newout = {"type": "DELIVERY_OUTBOUND", "isin": "LU2573966905", "name": "Amundi EM", "date": "2023-10-04",
          "shares": "2"}
call("POST", "/clients/smoke/transactions", newout, expect=409)
call("POST", "/clients/smoke/transactions", {**newout, "force": True}, expect=400)  # no price, no value
assert call("GET", "/clients/smoke")["securities"] == before
call("POST", "/clients/smoke/transactions", {**newout, "force": True, "amount": "30"}, expect=201)
assert call("GET", "/clients/smoke")["securities"] == before + 1
mexp = call("GET", "/clients/smoke/export?prices=none")
mt = {t["uuid"]: t for t in mexp["transactions"]}
assert mt[out["uuid"]]["type"] == "DELIVERY_OUTBOUND" and mt[inb["uuid"]]["type"] == "DELIVERY_INBOUND", mt
dsec = dexp["securities"][0]["uuid"]
dport = dexp["portfolios"][0]["uuid"]
priced = call("POST", "/clients/demo/transactions", {"type": "DELIVERY_OUTBOUND", "portfolio": dport,
              "security": dsec, "date": "2025-12-01", "shares": "1"}, expect=201)
assert float(priced["amount"]) > 0, priced
# Depotwechsel (TRANSFER) between two portfolios of a minimal hand-written file
XFER = """<client><version>70</version><baseCurrency>EUR</baseCurrency><securities><security>
<uuid>5ec00000-0000-4000-8000-000000000001</uuid><name>Xfer ETF</name><currencyCode>EUR</currencyCode>
<isin>IE00B4L5Y983</isin><prices><price t="2024-01-02" v="10000000000"/></prices><isRetired>false</isRetired>
</security></securities><accounts><account><uuid>acc00000-0000-4000-8000-000000000001</uuid><name>Konto</name>
<currencyCode>EUR</currencyCode><isRetired>false</isRetired><transactions/></account></accounts><portfolios>
<portfolio><uuid>p0000000-0000-4000-8000-000000000001</uuid><name>Depot A</name><isRetired>false</isRetired>
<referenceAccount reference="../../../accounts/account"/><transactions/></portfolio>
<portfolio><uuid>p0000000-0000-4000-8000-000000000002</uuid><name>Depot B</name><isRetired>false</isRetired>
<referenceAccount reference="../../../accounts/account"/><transactions/></portfolio>
</portfolios><plans/><watchlists/><properties/><settings/></client>"""
req = urllib.request.Request(base + "/clients/xfer/file", data=XFER.encode(), method="PUT",
                             headers={"Authorization": f"Bearer {token}", "X-Filename": "xfer.xml"})
with opener.open(req, timeout=120) as r:
    assert r.status == 200, r.status
PA, PB = "p0000000-0000-4000-8000-000000000001", "p0000000-0000-4000-8000-000000000002"
SEC = "5ec00000-0000-4000-8000-000000000001"
call("POST", "/clients/xfer/transactions", {"type": "DELIVERY_INBOUND", "portfolio": PA, "security": SEC,
     "date": "2024-01-02", "shares": "10", "amount": "1000"}, expect=201)
xfer = {"type": "TRANSFER", "portfolio": PA, "target": PB, "security": SEC, "date": "2024-01-02"}
call("POST", "/clients/xfer/transactions", {**xfer, "shares": "11"}, expect=409)
call("POST", "/clients/xfer/transactions", {**xfer, "target": PA, "shares": "1"}, expect=400)
call("POST", "/clients/xfer/transactions", {**xfer, "target": "", "shares": "1"}, expect=400)
tr = call("POST", "/clients/xfer/transactions", {**xfer, "shares": "4", "note": "Depotwechsel"}, expect=201)
assert tr["type"] == "TRANSFER" and float(tr["amount"]) == 400 and tr["target"] == PB, tr
xt = {t["uuid"]: t for t in call("GET", "/clients/xfer/export?prices=none")["transactions"]}
out_t, in_t = xt[tr["uuid"]], xt[tr["targetUuid"]]
assert out_t["type"] == "TRANSFER_OUT" and in_t["type"] == "TRANSFER_IN", (out_t, in_t)
assert out_t["cross"]["type"] == "portfolio-transfer" and out_t["cross"]["uuid"] == in_t["uuid"], out_t
assert out_t.get("note") == "Depotwechsel" and in_t["owner"] == PB, in_t
call("DELETE", f"/clients/xfer/transactions/{tr['uuid']}")
xt = {t["uuid"] for t in call("GET", "/clients/xfer/export?prices=none")["transactions"]}
assert tr["uuid"] not in xt and tr["targetUuid"] not in xt, xt
# new securities account with a new cash account, and one with an existing cash account
np = call("POST", "/clients/xfer/portfolios", {"name": "Depot C", "accountName": "Konto C", "currency": "usd"},
          expect=201)
assert np["accountCreated"] and np["currency"] == "USD", np
call("POST", "/clients/xfer/portfolios", {"name": "depot c", "account": np["account"]}, expect=409)
call("POST", "/clients/xfer/portfolios", {"name": "Depot D", "accountName": "konto c"}, expect=409)
call("POST", "/clients/xfer/portfolios", {"name": "Depot D", "accountName": "X", "currency": "XYZ"}, expect=400)
call("POST", "/clients/xfer/portfolios", {"name": "Depot D"}, expect=400)
nd = call("POST", "/clients/xfer/portfolios", {"name": "Depot D", "account": np["account"]}, expect=201)
assert not nd["accountCreated"] and nd["account"] == np["account"], nd
xs = call("GET", "/clients/xfer")
assert {p["name"] for p in xs["portfolioList"]} == {"Depot A", "Depot B", "Depot C", "Depot D"}, xs
assert {a["name"] for a in xs["accountList"]} == {"Konto", "Konto C"}, xs
# a transfer to a portfolio in another currency is refused
call("POST", "/clients/xfer/transactions", {**xfer, "target": np["portfolio"], "shares": "1"}, expect=400)
# PDF import (statement as text, as PP's importers see it) into a file with several securities accounts: the
# securities account is asked for; a GET of the session only shows it – it imports nothing and does not fail
STATEMENT = """Baader Bank AG Weihenstephaner Straße 4 85716 Unterschleißheim
www.finanzen.net/zero
Seite 1/1
06.04.2022
Vorgangs-Nr.: 12345678
Wertpapierabrechnung: Kauf
Auftragsdatum: 05.04.2022 Ausführungsplatz: GETTEX - MM Munich
Auftragszeit: 16:54:28:84
Nominale ISIN: DE0008430026 WKN: 843002 Kurs
STK 2 Muster Rück AG EUR 300,00
Namens-Aktien o.N.
Kurswert EUR 600,00
Zu Lasten Konto 1111111111 Valuta: 07.04.2022 EUR 600,00
Handels- Handels-
Nominale Kurs Ausführungsplatz datum uhrzeit
STK 2 EUR 300,00 GETTEX - MM Munich 05.04.2022 16:54:29:17
"""
doc = base64.b64encode(STATEMENT.encode()).decode()
imp = call("POST", "/clients/xfer/import", {"files": [{"name": "2022-04-06_1_Kauf.txt", "data": doc}], "apply": True})
assert imp["needsTarget"] and imp["imported"] == 0, imp
assert any("target account missing" in m["message"] for i in imp["items"] for m in i["messages"]), imp["items"]
assert {i["file"] for i in imp["items"] if i["kind"] != "SecurityItem"} == {"2022-04-06_1_Kauf.txt"}, imp["items"]
view = call("GET", f"/clients/xfer/import/{imp['session']}")
assert view["needsTarget"] and view["imported"] == 0, view
assert call("GET", "/clients/xfer")["transactions"] == xs["transactions"]  # nothing imported by the GET
done = call("POST", f"/clients/xfer/import/{imp['session']}/apply",
            {"portfolio": PA, "account": "acc00000-0000-4000-8000-000000000001", "force": []})
assert done["imported"] >= 1 and not done["needsTarget"], done
again = call("POST", "/clients/xfer/import", {"files": [{"name": "again.txt", "data": doc}], "apply": True})
assert not again["needsTarget"] and again["imported"] == 0, again  # the bank's target is remembered
assert [i["status"] for i in again["items"]] == ["WARNING"], again["items"]  # possible duplicate
raw = call("GET", "/clients/smoke/file")
assert b"<client" in raw[:200], raw[:200]
print("pp-core smoke test passed:", len(feeds), "price sources")
PY
  tail -n 80 "$WORK/ppcore.log" >&2
  exit 1
fi
