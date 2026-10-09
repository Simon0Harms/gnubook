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
mexp = call("GET", "/clients/smoke/export?prices=none")
mt = {t["uuid"]: t for t in mexp["transactions"]}
assert mt[out["uuid"]]["type"] == "DELIVERY_OUTBOUND" and mt[inb["uuid"]]["type"] == "DELIVERY_INBOUND", mt
dsec = dexp["securities"][0]["uuid"]
dport = dexp["portfolios"][0]["uuid"]
priced = call("POST", "/clients/demo/transactions", {"type": "DELIVERY_OUTBOUND", "portfolio": dport,
              "security": dsec, "date": "2025-12-01", "shares": "1"}, expect=201)
assert float(priced["amount"]) > 0, priced
raw = call("GET", "/clients/smoke/file")
assert b"<client" in raw[:200], raw[:200]
print("pp-core smoke test passed:", len(feeds), "price sources")
PY
  tail -n 80 "$WORK/ppcore.log" >&2
  exit 1
fi
