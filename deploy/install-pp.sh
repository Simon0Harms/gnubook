#!/usr/bin/env bash
# Install or update pp-core: Portfolio Performance without its user interface, as a local service for gnubook
# (docs/PORTFOLIO-PERFORMANCE.md). Run as root, after install.sh.
#
#   bash /opt/gnubook/src/deploy/install-pp.sh            # the PP release this gnubook version was tested with
#   bash /opt/gnubook/src/deploy/install-pp.sh latest     # newest PP release (gnubook-pp-update does this)
#   bash /opt/gnubook/src/deploy/install-pp.sh 0.88.0     # a specific PP release
#   bash /opt/gnubook/src/deploy/install-pp.sh --rebuild  # rebuild pp-core for the installed PP (gnubook-update)
#
# Environment: GNUBOOK_PP_PORT (8091), GNUBOOK_PP_VERIFY=0 (skip the signature check – not recommended),
# GNUBOOK_PP_KEYFILE (PP's public key as file, if keys.openpgp.org is not reachable), GNUBOOK_PP_RELEASES
# (a mirror of PP's GitHub releases; the signature is still checked), PPCORE_JAVA_HOME (JDK 21 or newer).
#
# Layout below /opt/gnubook/pp:
#   dist/<version>/portfolio/  the PP release as downloaded (root, read-only)
#   builds/<version>-<time>/   pp-core compiled against it, plus its OSGi configuration
#   current -> builds/...      the active build. A new build becomes active only if pp-core answers with it;
#                              otherwise the previous build is started again.
set -euo pipefail

APP_DIR=${GNUBOOK_DIR:-/opt/gnubook}
TESTED_PP_VERSION=0.88.0
# Portfolio Performance's release files are signed by Andreas Buchen with this key
PP_KEY=E46E6F8FF02E4C83569084589239277F560C95AC
PP_RELEASES=${GNUBOOK_PP_RELEASES:-https://github.com/portfolio-performance/portfolio/releases}
PP_ROOT=$APP_DIR/pp
SRC=$APP_DIR/src
ARG=${1:-$TESTED_PP_VERSION}

say() { echo ">> $*"; }
die() { echo "!! $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Bitte als root ausführen."
[ -f "$SRC/ppcore/build.sh" ] || die "$SRC/ppcore fehlt – zuerst gnubook installieren bzw. aktualisieren."
[ -f "$APP_DIR/config.toml" ] || die "$APP_DIR/config.toml fehlt – zuerst deploy/install.sh ausführen."
id gnubook >/dev/null 2>&1 || die "Benutzer gnubook fehlt – zuerst deploy/install.sh ausführen."

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------------------------------------- Java
java_major() {  # $1 = JDK directory; prints the major version of its javac
  "$1/bin/javac" -version 2>&1 | sed -n 's/^javac \([0-9]*\).*/\1/p' | head -n 1
}

find_jdk() {  # prints the directory of a JDK 21 or newer
  local c v
  for c in "${PPCORE_JAVA_HOME:-}" /usr/lib/jvm/java-21-openjdk-* /usr/lib/jvm/temurin-21* /usr/lib/jvm/*; do
    [ -n "$c" ] && [ -x "$c/bin/javac" ] && [ -x "$c/bin/java" ] || continue
    v=$(java_major "$c")
    if [ -n "$v" ] && [ "$v" -ge 21 ]; then echo "$c"; return 0; fi
  done
  return 0
}

REBUILD=0
if [ "$ARG" = "--rebuild" ]; then
  REBUILD=1
  [ -L "$PP_ROOT/current" ] || { echo ">> pp-core ist nicht installiert – nichts zu tun."; exit 0; }
  VERSION=$(cat "$PP_ROOT/current/pp-version")
fi

JDK=$(find_jdk)
if [ "$REBUILD" = 0 ]; then
  say "Pakete installieren"
  apt-get update -qq
  PKGS="curl ca-certificates gpg"
  if [ -z "$JDK" ]; then
    apt-cache show openjdk-21-jdk-headless >/dev/null 2>&1 ||
      die "Kein JDK 21 gefunden und das Paket openjdk-21-jdk-headless gibt es hier nicht. Bitte ein JDK 21 installieren (z. B. Eclipse Temurin 21) und PPCORE_JAVA_HOME=/pfad/zum/jdk setzen."
    PKGS="$PKGS openjdk-21-jdk-headless"
  fi
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $PKGS >/dev/null
  [ -n "$JDK" ] || JDK=$(find_jdk)
fi
[ -n "$JDK" ] || die "Kein JDK 21 (javac) gefunden – PPCORE_JAVA_HOME setzen."
echo "   Java: $JDK (javac $(java_major "$JDK"))"

# ---------------------------------------------------------------------------------- PP release
case "$(dpkg --print-architecture 2>/dev/null || uname -m)" in
  amd64|x86_64) ARCH=x86_64 ;;
  arm64|aarch64) ARCH=aarch64 ;;
  *) die "Portfolio Performance gibt es für Linux nur für x86_64 und aarch64." ;;
esac

if [ "$REBUILD" = 0 ]; then
  if [ "$ARG" = "latest" ]; then
    VERSION=$(curl -fsS -o /dev/null -w '%{redirect_url}' "$PP_RELEASES/latest" | sed -n 's#.*/tag/##p' || true)
    [ -n "$VERSION" ] || die "Neueste PP-Version nicht ermittelbar – Version angeben, z. B. install-pp.sh $TESTED_PP_VERSION"
  else
    VERSION=$ARG
  fi
fi
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || die "Ungültige PP-Version: $VERSION"

verify() {  # $1 file, $2 signature
  if [ "${GNUBOOK_PP_VERIFY:-1}" = "0" ]; then
    echo "   Signaturprüfung übersprungen (GNUBOOK_PP_VERIFY=0)"
    return 0
  fi
  local gh="$TMP/gnupg"
  install -d -m 700 "$gh"
  if [ -n "${GNUBOOK_PP_KEYFILE:-}" ]; then
    cp "$GNUBOOK_PP_KEYFILE" "$gh/key.asc"
  else
    curl -fsSL --retry 3 -o "$gh/key.asc" "https://keys.openpgp.org/vks/v1/by-fingerprint/$PP_KEY" ||
      die "Schlüssel $PP_KEY von keys.openpgp.org nicht ladbar. Schlüssel als Datei mit GNUBOOK_PP_KEYFILE angeben (oder, nicht empfohlen, GNUBOOK_PP_VERIFY=0)."
  fi
  gpg --homedir "$gh" --batch --quiet --import "$gh/key.asc" >/dev/null 2>&1 || true
  gpg --homedir "$gh" --batch --export "$PP_KEY" > "$gh/pp.gpg" 2>/dev/null || true
  [ -s "$gh/pp.gpg" ] || die "Der Schlüssel $PP_KEY ist nicht in der Schlüsseldatei."
  if ! gpgv --homedir "$gh" --keyring "$gh/pp.gpg" "$2" "$1" >"$gh/out" 2>&1; then
    cat "$gh/out" >&2
    die "Die Signatur von $(basename "$1") stimmt nicht – Datei verworfen."
  fi
  echo "   Signatur geprüft (Schlüssel $PP_KEY)"
}

DIST=$PP_ROOT/dist/$VERSION
if [ ! -d "$DIST/portfolio/plugins" ]; then
  FILE=PortfolioPerformance-$VERSION-linux.gtk.$ARCH.tar.gz
  say "Portfolio Performance $VERSION herunterladen"
  curl -fsSL --retry 3 -o "$TMP/$FILE" "$PP_RELEASES/download/$VERSION/$FILE" ||
    die "Download fehlgeschlagen: $PP_RELEASES/download/$VERSION/$FILE"
  curl -fsSL --retry 3 -o "$TMP/$FILE.asc" "$PP_RELEASES/download/$VERSION/$FILE.asc" ||
    die "Signatur fehlt: $PP_RELEASES/download/$VERSION/$FILE.asc"
  verify "$TMP/$FILE" "$TMP/$FILE.asc"
  install -d -m 755 "$PP_ROOT" "$PP_ROOT/dist" "$PP_ROOT/builds"
  rm -rf "$DIST.new"
  mkdir -p "$DIST.new"
  tar -xzf "$TMP/$FILE" -C "$DIST.new" --no-same-owner
  [ -d "$DIST.new/portfolio/plugins" ] || die "Unerwarteter Inhalt in $FILE"
  chmod -R u+rwX,go+rX,go-w "$DIST.new"
  rm -rf "$DIST"
  mv "$DIST.new" "$DIST"
fi

# ---------------------------------------------------------------------------------- pp-core build
install -d -m 755 "$PP_ROOT/builds"
BUILD_NAME=$VERSION-$(date +%Y%m%d-%H%M%S)
BUILD=$PP_ROOT/builds/$BUILD_NAME
mkdir -p "$BUILD"
ln -s "$DIST/portfolio" "$BUILD/portfolio"
echo "$VERSION" > "$BUILD/pp-version"
git -C "$SRC" rev-parse --short HEAD > "$BUILD/gnubook-commit" 2>/dev/null || true
say "pp-core für Portfolio Performance $VERSION bauen"
if ! PATH="$JDK/bin:$PATH" bash "$SRC/ppcore/build.sh" "$BUILD/portfolio" "$BUILD/gnubook.ppcore.jar" >"$TMP/build.log" 2>&1; then
  tail -n 30 "$TMP/build.log" >&2
  rm -rf "$BUILD"
  die "pp-core lässt sich nicht gegen PP $VERSION bauen (geänderte PP-Schnittstelle?). Die laufende Version bleibt aktiv."
fi
bash "$SRC/ppcore/configure.sh" "$BUILD/portfolio" "$BUILD/gnubook.ppcore.jar" "$BUILD/configuration" >/dev/null
chown -R gnubook:gnubook "$BUILD/configuration"   # Equinox keeps its cache there

# ---------------------------------------------------------------------------------- settings
PROPS=$APP_DIR/ppcore.properties
if [ ! -f "$PROPS" ]; then
  say "$PROPS anlegen"
  TOKEN=$("$APP_DIR/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))')
  cat > "$PROPS" <<EOF
# pp-core (see $SRC/ppcore/README.md) – written by install-pp.sh
bind=127.0.0.1
port=${GNUBOOK_PP_PORT:-8091}
token=$TOKEN
data_dir=$APP_DIR/data/ppcore
keep_backups=20
max_upload_mb=64
EOF
fi
chown root:gnubook "$PROPS"
chmod 640 "$PROPS"
prop() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$PROPS" | tail -n 1 | tr -d '\r'; }
TOKEN=$(prop token)
PORT=$(prop port); PORT=${PORT:-8091}
HOST=$(prop bind)
case "$HOST" in ""|0.0.0.0) HOST=127.0.0.1 ;; esac
[ ${#TOKEN} -ge 24 ] || die "token in $PROPS fehlt oder ist kürzer als 24 Zeichen."
install -d -m 750 -o gnubook -g gnubook "$APP_DIR/data/ppcore"

ENVF=$APP_DIR/ppcore.env
if [ ! -f "$ENVF" ]; then
  printf '# JVM of pp-core (read by gnubook-ppcore.service)\nPPCORE_JAVA=%s\nPPCORE_JAVA_OPTS=-Xmx768m\n' \
    "$JDK/bin/java" > "$ENVF"
elif grep -q '^PPCORE_JAVA=' "$ENVF"; then
  sed -i "s#^PPCORE_JAVA=.*#PPCORE_JAVA=$JDK/bin/java#" "$ENVF"
else
  echo "PPCORE_JAVA=$JDK/bin/java" >> "$ENVF"
fi
chmod 644 "$ENVF"

if [ "$REBUILD" = 0 ]; then
  # [pp] url and token in gnubook's config.toml (other keys there stay as they are)
  "$APP_DIR/venv/bin/python" - "$APP_DIR/config.toml" "http://$HOST:$PORT" "$TOKEN" > "$TMP/config.toml" <<'PY'
import re
import sys

path, url, token = sys.argv[1:4]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
start = next((i for i, line in enumerate(lines) if re.match(r"\s*\[pp\]\s*(#.*)?$", line)), None)
if start is None:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines += ["\n", "[pp]\n", "# pp-core (Portfolio Performance), written by deploy/install-pp.sh\n",
              f'url = "{url}"\n', f'token = "{token}"\n']
else:
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    body = lines[start + 1:end]
    for key, value in (("token", token), ("url", url)):
        hit = [i for i, line in enumerate(body) if re.match(rf"\s*{key}\s*=", line)]
        if hit:
            body[hit[0]] = f'{key} = "{value}"\n'
        else:
            body.insert(0, f'{key} = "{value}"\n')
    lines[start + 1:end] = body
sys.stdout.write("".join(lines))
PY
  if ! cmp -s "$TMP/config.toml" "$APP_DIR/config.toml"; then
    "$APP_DIR/venv/bin/python" -c "import sys; from gnubook.config import load_config; c = load_config(sys.argv[1]); assert c.pp.url and c.pp.token" "$TMP/config.toml" ||
      die "config.toml konnte nicht ergänzt werden – bitte [pp] url und token von Hand eintragen (Token: $PROPS)."
    install -d -m 750 "$APP_DIR/backup"
    cp -p "$APP_DIR/config.toml" "$APP_DIR/backup/config.toml.$(date +%Y%m%d-%H%M%S)"
    cat "$TMP/config.toml" > "$APP_DIR/config.toml"   # keeps owner and mode
    echo "   [pp] in $APP_DIR/config.toml eingetragen"
  fi
fi

# ---------------------------------------------------------------------------------- services
install -m 644 "$SRC/deploy/gnubook-ppcore.service" "$SRC/deploy/gnubook-pp.service" "$SRC/deploy/gnubook-pp.timer" \
  /etc/systemd/system/
cat > /usr/local/bin/gnubook-pp-update <<WRAP
#!/bin/sh
# Update Portfolio Performance (pp-core): gnubook-pp-update [VERSION]   (default: newest release)
exec bash $SRC/deploy/install-pp.sh "\${1:-latest}"
WRAP
chmod 755 /usr/local/bin/gnubook-pp-update
systemctl daemon-reload

restarts() { systemctl show -p NRestarts --value gnubook-ppcore 2>/dev/null || true; }

health() {  # does pp-core answer with PP version $1? Gives up early when systemd had to restart it.
  local _ base
  base=$(restarts)
  for _ in $(seq 1 60); do
    if curl -fsS -m 5 -H "Authorization: Bearer $TOKEN" -o "$TMP/health.json" "http://$HOST:$PORT/api/v1/health" 2>/dev/null; then
      grep -q "\"ppVersion\": *\"$1" "$TMP/health.json" && return 0
    fi
    [ "$(restarts)" = "$base" ] || return 1
    sleep 3
  done
  return 1
}

activate() {  # point "current" to builds/$1 and restart pp-core
  ln -sfn "builds/$1" "$PP_ROOT/current.new"
  mv -Tf "$PP_ROOT/current.new" "$PP_ROOT/current"
  systemctl restart gnubook-ppcore
}

PREV=$(basename "$(readlink "$PP_ROOT/current" 2>/dev/null || true)")
say "pp-core starten"
systemctl enable -q gnubook-ppcore
activate "$BUILD_NAME"
if ! health "$VERSION"; then
  journalctl -u gnubook-ppcore -n 40 --no-pager >&2 || true
  if [ -n "$PREV" ] && [ "$PREV" != "$BUILD_NAME" ] && [ -d "$PP_ROOT/builds/$PREV" ]; then
    echo "!! pp-core startet mit diesem Build nicht – zurück auf $PREV" >&2
    activate "$PREV"
    rm -rf "$BUILD"
    if health "$(cat "$PP_ROOT/builds/$PREV/pp-version")"; then
      echo "   der bisherige Build läuft wieder" >&2
    else
      echo "!! auch der bisherige Build antwortet nicht – journalctl -u gnubook-ppcore" >&2
    fi
  else
    echo "!! pp-core startet nicht – Logs: journalctl -u gnubook-ppcore" >&2
  fi
  exit 1
fi
echo "   pp-core läuft: $(tr -d '\n' < "$TMP/health.json")"

# keep the active and the previous build (for rollback) plus two more; PP releases nobody uses go
for b in $(ls -1dt "$PP_ROOT"/builds/*/ 2>/dev/null | tail -n +5); do
  n=$(basename "$b")
  [ "$n" = "$BUILD_NAME" ] || [ "$n" = "$PREV" ] || rm -rf "$b"
done
for d in "$PP_ROOT"/dist/*/; do
  [ -d "$d" ] || continue
  v=$(basename "$d")
  grep -qx "$v" "$PP_ROOT"/builds/*/pp-version 2>/dev/null || rm -rf "$d"
done

if [ "$REBUILD" = 1 ]; then
  echo ">> pp-core neu gebaut und gestartet (Portfolio Performance $VERSION)"
  exit 0
fi
systemctl enable -q --now gnubook-pp.timer
systemctl try-restart gnubook   # reads [pp] (also when an earlier, failed run wrote it)
cat <<NEXT

>> Portfolio Performance $VERSION läuft als pp-core auf $HOST:$PORT.
   Weiter im Web: Portfolio Performance → Einstellungen – PP-Datei hochladen oder neu anlegen,
   Konten prüfen und die Übernahme ins GnuCash-Buch einschalten.
   Status: gnubook pp-status · Logs: journalctl -u gnubook-ppcore
   PP aktualisieren: gnubook-pp-update [VERSION]
NEXT
