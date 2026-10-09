#!/usr/bin/env bash
# Write the OSGi configuration that starts Portfolio Performance's bundles headless with the pp-core
# application instead of PP's user interface.
#
#   ppcore/configure.sh PP_DIR PPCORE_JAR CONFIG_DIR
#
# CONFIG_DIR gets config.ini and org.eclipse.equinox.simpleconfigurator/bundles.info (PP's bundle list plus
# pp-core). PP's own installation is not changed.
set -euo pipefail

PP_DIR=${1:?usage: configure.sh PP_DIR PPCORE_JAR CONFIG_DIR}
JAR=${2:?usage: configure.sh PP_DIR PPCORE_JAR CONFIG_DIR}
CONF=${3:?usage: configure.sh PP_DIR PPCORE_JAR CONFIG_DIR}

SRC_INFO="$PP_DIR/configuration/org.eclipse.equinox.simpleconfigurator/bundles.info"
[ -f "$SRC_INFO" ] || { echo "configure.sh: $SRC_INFO not found" >&2; exit 1; }
JAR=$(cd "$(dirname "$JAR")" && pwd)/$(basename "$JAR")
[ -f "$JAR" ] || { echo "configure.sh: $JAR not found" >&2; exit 1; }

pick() { (cd "$PP_DIR/plugins" && ls -d $1 2>/dev/null | sort | tail -n 1); }
SC=$(pick 'org.eclipse.equinox.simpleconfigurator_*.jar')
OSGI=$(pick 'org.eclipse.osgi_3*.jar')
COMPAT=$(pick 'org.eclipse.osgi.compatibility.state_*.jar')
[ -n "$SC" ] && [ -n "$OSGI" ] || { echo "configure.sh: Equinox bundles missing in $PP_DIR/plugins" >&2; exit 1; }

rm -rf "$CONF.new"
mkdir -p "$CONF.new/org.eclipse.equinox.simpleconfigurator"
grep -v '^gnubook\.ppcore,' "$SRC_INFO" > "$CONF.new/org.eclipse.equinox.simpleconfigurator/bundles.info"
echo "gnubook.ppcore,0.1.0,file:$JAR,4,false" >> "$CONF.new/org.eclipse.equinox.simpleconfigurator/bundles.info"

{
  echo "# written by gnubook ppcore/configure.sh – do not edit"
  echo "osgi.bundles=reference\\:file\\:$SC@1\\:start"
  echo "org.eclipse.equinox.simpleconfigurator.configUrl=file\\:org.eclipse.equinox.simpleconfigurator/bundles.info"
  echo "osgi.bundles.defaultStartLevel=4"
  echo "osgi.framework=file\\:plugins/$OSGI"
  [ -n "$COMPAT" ] && echo "osgi.framework.extensions=reference\\:file\\:$COMPAT"
  echo "osgi.requiredJavaVersion=21"
  echo "eclipse.application=gnubook.ppcore.server"
  echo "eclipse.ignoreApp=false"
  echo "osgi.framework.system.packages.extra=com.sun.net.httpserver"
} > "$CONF.new/config.ini"

rm -rf "$CONF.old"
[ -d "$CONF" ] && mv "$CONF" "$CONF.old"
mv "$CONF.new" "$CONF"
rm -rf "$CONF.old"
echo "configured $CONF"
