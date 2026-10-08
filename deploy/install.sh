#!/usr/bin/env bash
# Install gnubook on Debian 12/13 or Ubuntu 22.04+ (e.g. a Proxmox LXC). Run as root.
#
#   GNUBOOK_REPO=https://github.com/Simon0Harms/gnubook.git bash install.sh   # clone from GitHub
#   bash deploy/install.sh                                                    # from a local checkout
#
# Everything lives below /opt/gnubook (code, venv, config, data, backups).
set -euo pipefail

APP_DIR=${GNUBOOK_DIR:-/opt/gnubook}
REPO=${GNUBOOK_REPO:-}
REF=${GNUBOOK_REF:-main}
PORT=${GNUBOOK_PORT:-8080}
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

if [ "$(id -u)" -ne 0 ]; then echo "Bitte als root ausführen." >&2; exit 1; fi

echo ">> Pakete installieren"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv git curl ca-certificates >/dev/null

echo ">> Benutzer und Verzeichnisse"
id gnubook >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin gnubook
install -d -m 755 "$APP_DIR"
install -d -m 750 -o gnubook -g gnubook "$APP_DIR/data"
install -d -m 750 "$APP_DIR/backup"

echo ">> Quellcode"
if [ -n "$REPO" ]; then
  if [ -d "$APP_DIR/src/.git" ]; then
    git -C "$APP_DIR/src" fetch -q --tags origin
    git -C "$APP_DIR/src" checkout -q "$REF"
    git -C "$APP_DIR/src" pull -q --ff-only || true
  else
    rm -rf "$APP_DIR/src"
    git clone -q --branch "$REF" "$REPO" "$APP_DIR/src"
  fi
elif [ -f "$SCRIPT_DIR/../pyproject.toml" ]; then
  SRC=$(cd "$SCRIPT_DIR/.." && pwd)
  if [ "$SRC" != "$APP_DIR/src" ]; then
    rm -rf "$APP_DIR/src.new"
    cp -a "$SRC" "$APP_DIR/src.new"
    rm -rf "$APP_DIR/src.new/.venv"
    rm -rf "$APP_DIR/src"
    mv "$APP_DIR/src.new" "$APP_DIR/src"
  fi
else
  echo "Kein Quellcode gefunden: GNUBOOK_REPO setzen oder das Skript aus einem Checkout starten." >&2
  exit 1
fi

echo ">> Python-Umgebung"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q "$APP_DIR/src[postgres]"

echo ">> Konfiguration"
if [ ! -f "$APP_DIR/config.toml" ]; then
  "$APP_DIR/venv/bin/gnubook" init-config "$APP_DIR/config.toml" --data-dir "$APP_DIR/data" >/dev/null
  echo "   $APP_DIR/config.toml angelegt"
fi
chown root:gnubook "$APP_DIR/config.toml"
chmod 640 "$APP_DIR/config.toml"
if [ ! -f "$APP_DIR/gunicorn.conf.py" ]; then
  sed "s/0.0.0.0:8080/0.0.0.0:$PORT/" "$APP_DIR/src/deploy/gunicorn.conf.py" > "$APP_DIR/gunicorn.conf.py"
fi

cat > /usr/local/bin/gnubook <<WRAP
#!/bin/sh
# gnubook command line (runs as the service user so files in data/ keep the right owner)
export GNUBOOK_CONFIG=\${GNUBOOK_CONFIG:-$APP_DIR/config.toml}
if [ "\$(id -u)" -eq 0 ]; then
  exec runuser -u gnubook -- env GNUBOOK_CONFIG="\$GNUBOOK_CONFIG" $APP_DIR/venv/bin/gnubook "\$@"
fi
exec $APP_DIR/venv/bin/gnubook "\$@"
WRAP
chmod 755 /usr/local/bin/gnubook
install -m 755 "$APP_DIR/src/deploy/update.sh" /usr/local/bin/gnubook-update
install -m 644 "$APP_DIR/src/deploy/gnubook.service" /etc/systemd/system/gnubook.service
systemctl daemon-reload
systemctl enable -q gnubook

if gnubook check >/dev/null 2>&1; then
  systemctl restart gnubook
  echo ">> gnubook läuft: http://$(hostname -I | awk '{print $1}'):$PORT"
else
  cat <<NEXT

>> Fast fertig. Noch zu tun:
   1. Buch-URL in $APP_DIR/config.toml eintragen ([book] url), z. B.
        url = "postgresql://gnucash:PASSWORT@192.168.1.20:5432/gnucash"
   2. Passwort setzen:   gnubook hash-password   -> Ergebnis als password_hash eintragen
   3. Optional API-Token für den FinTS-Importer:   gnubook gen-token
   4. Prüfen und starten:   gnubook check && systemctl restart gnubook
NEXT
fi
