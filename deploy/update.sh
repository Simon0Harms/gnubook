#!/usr/bin/env bash
# Update gnubook (installed with install.sh from a git clone). Run as root.
#   gnubook-update            # newest commit of the installed branch
#   gnubook-update v0.2.0     # a tag or commit
# Backs up config and data to /opt/gnubook/backup first and rolls back if the new version does not start.
set -euo pipefail

APP_DIR=${GNUBOOK_DIR:-/opt/gnubook}
REF=${1:-}
if [ "$(id -u)" -ne 0 ]; then echo "Bitte als root ausführen." >&2; exit 1; fi
if [ ! -d "$APP_DIR/src/.git" ]; then
  echo "$APP_DIR/src ist kein git-Checkout – bitte install.sh mit GNUBOOK_REPO erneut ausführen." >&2
  exit 1
fi
PORT=$(sed -n 's/^bind *= *"[^:]*:\([0-9]*\)".*/\1/p' "$APP_DIR/gunicorn.conf.py" 2>/dev/null | head -1)
PORT=${PORT:-8080}
STAMP=$(date +%Y%m%d-%H%M%S)
GIT="git -C $APP_DIR/src"

echo ">> Sicherung nach $APP_DIR/backup/gnubook-$STAMP.tar.gz"
mkdir -p "$APP_DIR/backup"
EXTRA=""
for f in gunicorn.conf.py ppcore.properties ppcore.env; do [ -f "$APP_DIR/$f" ] && EXTRA="$EXTRA $f"; done
# shellcheck disable=SC2086
tar -czf "$APP_DIR/backup/gnubook-$STAMP.tar.gz" -C "$APP_DIR" \
  --exclude=data/ppcore/workspace --exclude=data/ppcore/home config.toml data $EXTRA
ls -1t "$APP_DIR"/backup/gnubook-*.tar.gz | tail -n +11 | xargs -r rm --

OLD=$($GIT rev-parse HEAD)
$GIT fetch -q --tags origin
if [ -n "$REF" ]; then
  $GIT checkout -q "$REF"
else
  BRANCH=$($GIT symbolic-ref --short -q HEAD || echo main)
  $GIT checkout -q "$BRANCH"
  $GIT pull -q --ff-only origin "$BRANCH"
fi
NEW=$($GIT rev-parse HEAD)
if [ "$OLD" = "$NEW" ]; then echo ">> Bereits aktuell ($($GIT describe --always --tags))"; exit 0; fi

install_and_start() {
  "$APP_DIR/venv/bin/pip" install -q "$APP_DIR/src[postgres]" && systemctl restart gnubook && sleep 4 &&
    curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login"
}

echo ">> $($GIT log --oneline -1 "$OLD") -> $($GIT log --oneline -1 "$NEW")"
if install_and_start; then
  install -m 755 "$APP_DIR/src/deploy/update.sh" /usr/local/bin/gnubook-update
  install -d -m 750 -o gnubook -g gnubook "$APP_DIR/backup/book"
  install -m 644 "$APP_DIR/src/deploy/gnubook-backup.service" "$APP_DIR/src/deploy/gnubook-backup.timer" /etc/systemd/system/
  systemctl daemon-reload && systemctl enable -q --now gnubook-backup.timer
  if [ -L "$APP_DIR/pp/current" ]; then
    # Portfolio Performance (pp-core) is installed: rebuild it when its code changed. A build that does not
    # start is not activated; the previous one keeps running.
    install -m 644 "$APP_DIR/src/deploy/gnubook-pp.service" "$APP_DIR/src/deploy/gnubook-pp.timer" /etc/systemd/system/
    systemctl daemon-reload
    if ! $GIT diff --quiet "$OLD" "$NEW" -- ppcore deploy/install-pp.sh deploy/gnubook-ppcore.service; then
      bash "$APP_DIR/src/deploy/install-pp.sh" --rebuild ||
        echo "!! pp-core: der neue Build startet nicht – der bisherige läuft weiter (journalctl -u gnubook-ppcore)" >&2
    fi
  fi
  echo ">> Aktualisiert auf $($GIT describe --always --tags)"
else
  echo "!! Neue Version startet nicht – zurück auf $OLD" >&2
  $GIT checkout -q "$OLD"
  install_and_start || true
  journalctl -u gnubook -n 30 --no-pager >&2 || true
  exit 1
fi
