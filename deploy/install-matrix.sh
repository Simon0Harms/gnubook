#!/usr/bin/env bash
# Install or update the Matrix bot of gnubook (docs/MATRIX.md): login links and notifications (failed Belegeingang
# runs) via Matrix direct messages. Same principle as in Portriga: a sidecar (matrix-nio with E2EE) in a venv of its
# own (matrix-nio >= 0.26 with vodozemac, no libolm), talking to gnubook through data/matrix/inbox and data/matrix/outbox. Run as root, after install.sh.
#
#   bash /opt/gnubook/src/deploy/install-matrix.sh @gnubookbot:example.org https://buch.example.org [https://matrix.example.org]
#   bash /opt/gnubook/src/deploy/install-matrix.sh --update     # after an update of gnubook (gnubook-update does this)
#
# 1st argument: the bot's Matrix ID, 2nd: gnubook's public address (for login links), 3rd: the homeserver's
# client URL (default: https://<server part of the Matrix ID>). Then put the bot's password into
# /opt/gnubook/matrix/gnubook-matrix.env and run: systemctl enable --now gnubook-matrix
set -euo pipefail

APP_DIR=${GNUBOOK_DIR:-/opt/gnubook}
SRC=$APP_DIR/src
PY=$APP_DIR/venv/bin/python
MVENV=$APP_DIR/matrix-venv
ENVFILE=$APP_DIR/matrix/gnubook-matrix.env

say() { echo ">> $*"; }
die() { echo "!! $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Bitte als root ausführen."
[ -f "$SRC/deploy/matrix/gnubook_matrix_bot.py" ] || die "$SRC fehlt – zuerst gnubook installieren bzw. aktualisieren."
[ -f "$APP_DIR/config.toml" ] || die "$APP_DIR/config.toml fehlt – zuerst deploy/install.sh ausführen."
[ -x "$PY" ] || die "$APP_DIR/venv fehlt – zuerst deploy/install.sh ausführen."
id gnubook >/dev/null 2>&1 || die "Benutzer gnubook fehlt – zuerst deploy/install.sh ausführen."

UPDATE=0
BOT="" ; PUBLIC="" ; HS=""
case "${1:-}" in
  --update) UPDATE=1 ;;
  @*:*) BOT=$1; PUBLIC=${2:-}; HS=${3:-https://${BOT#*:}}
        [ -n "$PUBLIC" ] || die "Öffentliche Adresse von gnubook fehlt (2. Parameter, z. B. https://buch.example.org)." ;;
  *) die "Aufruf: install-matrix.sh @bot:server https://gnubook.example.org [https://matrix.server] | --update" ;;
esac
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------------------------- matrix-nio with E2EE
say "matrix-nio (mit E2EE) in $MVENV"
# matrix-nio >= 0.26 encrypts with vodozemac (binary wheel) – libolm and a compiler are no longer needed
if ! python3 -c 'import ensurepip, venv' >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends python3-venv >/dev/null
fi
[ -x "$MVENV/bin/python" ] || python3 -m venv "$MVENV"
"$MVENV/bin/pip" install -q --upgrade pip
"$MVENV/bin/pip" install -q --upgrade "matrix-nio[e2e]>=0.26"
"$MVENV/bin/python" -c 'import sys
from importlib.metadata import version
from nio.crypto import ENCRYPTION_ENABLED
print("   matrix-nio", version("matrix-nio"), "– E2EE", "ok" if ENCRYPTION_ENABLED else "FEHLT")
sys.exit(0 if ENCRYPTION_ENABLED else 1)' || die "matrix-nio ohne Verschlüsselung installiert (vodozemac fehlt)."

install -d -m 750 -o gnubook -g gnubook "$APP_DIR/matrix"
install -d -m 700 -o gnubook -g gnubook "$APP_DIR/matrix-store" "$APP_DIR/data/matrix" \
  "$APP_DIR/data/matrix/inbox" "$APP_DIR/data/matrix/outbox"

# ---------------------------------------------------------------------------------- bot configuration
if [ ! -f "$ENVFILE" ]; then
  [ "$UPDATE" = 0 ] || die "$ENVFILE fehlt – install-matrix.sh einmal mit Matrix-ID und Adresse ausführen."
  sed -e "s|^GNUBOOK_MATRIX_HOMESERVER=.*|GNUBOOK_MATRIX_HOMESERVER=$HS|" \
      -e "s|^GNUBOOK_MATRIX_USER=.*|GNUBOOK_MATRIX_USER=$BOT|" \
      -e "s|^GNUBOOK_MATRIX_SPOOL_DIR=.*|GNUBOOK_MATRIX_SPOOL_DIR=$APP_DIR/data/matrix|" \
      -e "s|^GNUBOOK_MATRIX_STORE=.*|GNUBOOK_MATRIX_STORE=$APP_DIR/matrix-store|" \
      "$SRC/deploy/matrix/env.example" > "$TMP/env"
  install -m 600 -o gnubook -g gnubook "$TMP/env" "$ENVFILE"
  echo "   $ENVFILE angelegt – Passwort des Bots eintragen (GNUBOOK_MATRIX_PASSWORD)"
fi

# ---------------------------------------------------------------------------------- config.toml
if [ "$UPDATE" = 0 ]; then
  "$PY" - "$APP_DIR/config.toml" "$BOT" "$PUBLIC" > "$TMP/config.toml" <<'PY'
import re
import sys

path, bot, public = sys.argv[1:4]
values = {"bot_mxid": f'"{bot}"', "public_url": f'"{public.rstrip("/")}"'}
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
start = next((i for i, line in enumerate(lines) if re.match(r"\s*\[matrix\]\s*(#.*)?$", line)), None)
if start is None:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines += ["\n", "[matrix]\n", "# Matrix bot (docs/MATRIX.md), written by deploy/install-matrix.sh\n"]
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
    "$PY" -c "import sys; from gnubook.config import load_config; assert load_config(sys.argv[1]).matrix.bot_mxid == sys.argv[2]" \
      "$TMP/config.toml" "$BOT" ||
      die "config.toml konnte nicht ergänzt werden – bitte [matrix] bot_mxid und public_url von Hand eintragen."
    install -d -m 750 "$APP_DIR/backup"
    cp -p "$APP_DIR/config.toml" "$APP_DIR/backup/config.toml.$(date +%Y%m%d-%H%M%S)"
    cat "$TMP/config.toml" > "$APP_DIR/config.toml"   # keeps owner and mode
    echo "   [matrix] in $APP_DIR/config.toml eingetragen"
  fi
fi

# ---------------------------------------------------------------------------------- service
say "Dienst gnubook-matrix"
install -m 644 "$SRC/deploy/matrix/gnubook-matrix.service" /etc/systemd/system/
systemctl daemon-reload
[ "$UPDATE" = 1 ] || systemctl restart gnubook
if grep -q '^GNUBOOK_MATRIX_PASSWORD=BitteHierDasBotPasswort' "$ENVFILE" && [ ! -s "$APP_DIR/matrix-store/bot-state.json" ]; then
  echo
  echo "Noch zu tun:"
  echo "  1. Passwort des Bots in $ENVFILE eintragen"
  echo "  2. systemctl enable --now gnubook-matrix && journalctl -fu gnubook-matrix"
  echo "  3. nach der ersten Anmeldung das Passwort wieder aus $ENVFILE entfernen"
  echo "  4. in gnubook: Konto und Sprache → Matrix-Konto verknüpfen"
else
  systemctl enable -q gnubook-matrix
  systemctl restart gnubook-matrix
  echo "   gnubook-matrix läuft (journalctl -fu gnubook-matrix)"
fi
