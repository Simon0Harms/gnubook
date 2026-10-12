#!/usr/bin/env bash
# Install or update the Belegeingang: documents from the finanzen.net zero postbox, fetched by a headless browser
# (docs/POSTBOX.md). Installs Playwright for Python into gnubook's venv and Playwright's own browser build (no X
# server, no VNC), writes [postbox] into config.toml and installs the systemd units. Run as root, after install.sh.
#
#   bash /opt/gnubook/src/deploy/install-postbox.sh            # install with Firefox
#   bash /opt/gnubook/src/deploy/install-postbox.sh chromium   # with Chromium instead
#   bash /opt/gnubook/src/deploy/install-postbox.sh --update   # after an update of gnubook (gnubook-update does this)
#
# The browser build belongs to the installed Playwright version; --update fetches the matching one when pip has
# updated Playwright. Browsers live in /opt/gnubook/pw-browsers, the browser's HOME, run requests and screenshots
# of failed runs in /opt/gnubook/data/postbox.
set -euo pipefail

APP_DIR=${GNUBOOK_DIR:-/opt/gnubook}
SRC=$APP_DIR/src
BROWSERS=$APP_DIR/pw-browsers
PY=$APP_DIR/venv/bin/python
ARG=${1:-}

say() { echo ">> $*"; }
die() { echo "!! $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Bitte als root ausführen."
[ -f "$SRC/pyproject.toml" ] || die "$SRC fehlt – zuerst gnubook installieren bzw. aktualisieren."
[ -f "$APP_DIR/config.toml" ] || die "$APP_DIR/config.toml fehlt – zuerst deploy/install.sh ausführen."
[ -x "$PY" ] || die "$APP_DIR/venv fehlt – zuerst deploy/install.sh ausführen."
id gnubook >/dev/null 2>&1 || die "Benutzer gnubook fehlt – zuerst deploy/install.sh ausführen."

UPDATE=0
case "$ARG" in
  --update)
    UPDATE=1
    BROWSER=$("$PY" -c 'import sys; from gnubook.config import load_config; print(load_config(sys.argv[1]).postbox.browser)' \
      "$APP_DIR/config.toml" 2>/dev/null || echo firefox) ;;
  ""|firefox) BROWSER=firefox ;;
  chromium) BROWSER=chromium ;;
  *) die "Unbekannter Parameter: $ARG (firefox, chromium oder --update)" ;;
esac
case "$BROWSER" in firefox|chromium) ;; *) BROWSER=firefox ;; esac

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------------------------- Playwright and browser
say "Playwright (Python) in $APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q "${SRC}[postgres,postbox]"
echo "   Playwright $("$PY" -c 'import importlib.metadata as m; print(m.version("playwright"))')"

say "Browser $BROWSER (Build von Playwright) nach $BROWSERS"
install -d -m 755 "$BROWSERS"
# first the browser build that belongs to the installed Playwright (pip may just have updated it) ...
PLAYWRIGHT_BROWSERS_PATH="$BROWSERS" "$PY" -m playwright install "$BROWSER"
chmod -R a+rX "$BROWSERS"
# ... then its system libraries (also on --update: a new build may need new ones). Playwright knows the package
# names for Debian and Ubuntu; elsewhere the distribution's own browser brings them.
deps() {
  PLAYWRIGHT_BROWSERS_PATH="$BROWSERS" "$PY" -m playwright install-deps "$BROWSER" && return 0
  echo "   Playwright kennt die Pakete dieser Distribution nicht – installiere den Browser der Distribution"
  echo "   wegen seiner Bibliotheken (benutzt wird trotzdem der Build von Playwright)"
  if [ "$BROWSER" = firefox ]; then PKG=firefox-esr; else PKG=chromium; fi
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$PKG" >/dev/null
}
if ! deps; then
  [ "$UPDATE" = 1 ] || die "Die Systembibliotheken für $BROWSER ließen sich nicht installieren."
  echo "!! Systembibliotheken für $BROWSER nicht installiert (apt belegt?) – der Test unten zeigt, ob der Browser startet" >&2
fi

install -d -m 750 -o gnubook -g gnubook "$APP_DIR/data/postbox" "$APP_DIR/data/postbox/requests"
install -d -m 700 -o gnubook -g gnubook "$APP_DIR/data/postbox/home"

say "Browser starten (headless, als Benutzer gnubook, mit den Einschränkungen des Dienstes)"
SMOKE='import sys
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    browser = getattr(pw, sys.argv[1]).launch(headless=True)
    page = browser.new_page()
    page.set_content("<p id=x>ok</p>")
    assert page.inner_text("#x") == "ok"
    print("   " + sys.argv[1] + " " + browser.version + " startet")
    browser.close()'
if command -v systemd-run >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  # same sandbox as gnubook-postbox.service
  systemd-run --quiet --wait --pipe --collect -p RuntimeMaxSec=300 -p User=gnubook -p Group=gnubook -p NoNewPrivileges=true \
    -p ProtectSystem=strict -p ProtectHome=true -p PrivateTmp=true -p "ReadWritePaths=$APP_DIR/data" \
    -E HOME="$APP_DIR/data/postbox/home" -E PLAYWRIGHT_BROWSERS_PATH="$BROWSERS" \
    "$PY" -c "$SMOKE" "$BROWSER" </dev/null 2>&1 | cat
  # stdin/stdout are no terminal: systemd would chown a terminal to the service user, which fails in an LXC (208/STDIN)
  [ "${PIPESTATUS[0]}" = 0 ] ||
    die "Der Browser startet nicht (Meldung oben, sonst: journalctl -n 30 -u 'run-*'). Mit dem anderen Browser versuchen: install-postbox.sh chromium"
else
  timeout 300 runuser -u gnubook -- env HOME="$APP_DIR/data/postbox/home" PLAYWRIGHT_BROWSERS_PATH="$BROWSERS" \
    "$PY" -c "$SMOKE" "$BROWSER" ||
    die "Der Browser startet nicht (Meldung oben). Mit dem anderen Browser versuchen: install-postbox.sh chromium"
fi

# ---------------------------------------------------------------------------------- config.toml
if [ "$UPDATE" = 0 ]; then
  # [postbox] enabled, browser and browsers_path (other keys there stay as they are)
  "$PY" - "$APP_DIR/config.toml" "$BROWSER" "$BROWSERS" > "$TMP/config.toml" <<'PY'
import re
import sys

path, browser, browsers = sys.argv[1:4]
values = {"enabled": "true", "browser": f'"{browser}"', "browsers_path": f'"{browsers}"'}
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
start = next((i for i, line in enumerate(lines) if re.match(r"\s*\[postbox\]\s*(#.*)?$", line)), None)
if start is None:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines += ["\n", "[postbox]\n", "# Belegeingang (docs/POSTBOX.md), written by deploy/install-postbox.sh\n"]
    lines += [f"{key} = {value}\n" for key, value in values.items()]
else:
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    body = lines[start + 1:end]
    for key, value in values.items():
        hit = [i for i, line in enumerate(body) if re.match(rf"\s*{key}\s*=", line)]
        if hit:
            body[hit[0]] = f"{key} = {value}\n"
        else:
            body.insert(0, f"{key} = {value}\n")
    lines[start + 1:end] = body
sys.stdout.write("".join(lines))
PY
  if ! cmp -s "$TMP/config.toml" "$APP_DIR/config.toml"; then
    "$PY" -c "import sys; from gnubook.config import load_config; assert load_config(sys.argv[1]).postbox.enabled" \
      "$TMP/config.toml" ||
      die "config.toml konnte nicht ergänzt werden – bitte [postbox] enabled = true von Hand eintragen."
    install -d -m 750 "$APP_DIR/backup"
    cp -p "$APP_DIR/config.toml" "$APP_DIR/backup/config.toml.$(date +%Y%m%d-%H%M%S)"
    cat "$TMP/config.toml" > "$APP_DIR/config.toml"   # keeps owner and mode
    echo "   [postbox] in $APP_DIR/config.toml eingetragen"
  fi
fi

# ---------------------------------------------------------------------------------- services
say "Dienste"
install -m 644 "$SRC/deploy/gnubook-postbox.service" "$SRC/deploy/gnubook-postbox.timer" \
  "$SRC/deploy/gnubook-postbox-request.service" "$SRC/deploy/gnubook-postbox-request.path" /etc/systemd/system/
systemctl daemon-reload
if [ "$UPDATE" = 0 ]; then
  # only on the first installation – a timer switched off on purpose stays off after updates
  systemctl enable -q --now gnubook-postbox.timer gnubook-postbox-request.path
  systemctl restart gnubook
  cat <<'NEXT'

>> Belegeingang ist installiert. Weiter in gnubook unter „Belegeingang“:
   1. Benutzername und Passwort von finanzen.net zero, „Belege ab“ und den Nextcloud-Ordner eintragen.
   2. „Probelauf“ – meldet sich an und liest nur die Liste; das Ergebnis steht unter „Letzte Läufe“.
   3. „Jetzt abrufen“, dann „Automatisch abrufen“ einschalten.
   Abrufzeiten: systemctl list-timers gnubook-postbox.timer   (ändern: systemctl edit gnubook-postbox.timer)
   Protokoll:   journalctl -u gnubook-postbox -u gnubook-postbox-request
NEXT
fi
