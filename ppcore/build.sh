#!/usr/bin/env bash
# Build the pp-core bundle against an installed Portfolio Performance.
#
#   ppcore/build.sh /opt/gnubook/pp/dist/0.88.0/portfolio /tmp/gnubook.ppcore.jar
#
# Needs a Java 21 JDK (javac, jar). Compiling against the installed PP version means an incompatible PP
# release is noticed here, before the service is restarted.
set -euo pipefail

PP_DIR=${1:?usage: build.sh PP_DIR OUT_JAR}
OUT=${2:?usage: build.sh PP_DIR OUT_JAR}
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

if [ ! -d "$PP_DIR/plugins" ]; then
  echo "build.sh: $PP_DIR/plugins not found (Portfolio Performance installation?)" >&2
  exit 1
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# PP has several hundred bundles: the class path goes into an argument file, as one command line argument
# it would exceed the kernel's limit of 128 KiB per argument
CP=""
for entry in "$PP_DIR"/plugins/*; do
  case "$entry" in
    *.jar) CP="$CP:$entry" ;;
    *) [ -d "$entry" ] && CP="$CP:$entry" ;;
  esac
done
printf -- '-cp\n"%s"\n' "${CP#:}" > "$TMP/classpath.txt"

mkdir -p "$TMP/classes"
find "$SRC/src" -name '*.java' > "$TMP/sources.txt"
javac --release 21 -nowarn -encoding UTF-8 -proc:none -d "$TMP/classes" @"$TMP/classpath.txt" @"$TMP/sources.txt"
cp "$SRC/plugin.xml" "$TMP/classes/plugin.xml"
mkdir -p "$(dirname "$OUT")"
jar --create --file "$TMP/out.jar" --manifest "$SRC/META-INF/MANIFEST.MF" -C "$TMP/classes" .
mv "$TMP/out.jar" "$OUT"
echo "built $OUT"
