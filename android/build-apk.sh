#!/usr/bin/env bash
# Build the scratchforge APK without Gradle.
#
# Gradle's daemon cannot open a loopback socket in this environment, so this
# drives the SDK tools directly: aapt2 -> javac -> d8 -> zipalign -> apksigner.
# The app has no third-party dependencies, so there is nothing to resolve and
# the whole build runs offline in a few seconds.
#
# Two Windows-isms this has to work around:
#   * the SDK tools are native Windows binaries, so every path handed to them
#     goes through `cygpath -w` (MSYS paths like /c/... are not understood)
#   * d8.bat / apksigner.bat mis-handle the space in "Immanuel David", so we
#     run their jars directly with java instead
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="${ANDROID_SDK:-C:/Users/Immanuel David/AppData/Local/Android/Sdk}"
JDK="${JDK_HOME:-C:/Program Files/Android/Android Studio/jbr}"
BT="$SDK/build-tools/36.0.0"
PLATFORM="$SDK/platforms/android-36/android.jar"

w() { cygpath -w "$1"; }                      # msys path -> windows path

AAPT2="$BT/aapt2.exe"
ZIPALIGN="$BT/zipalign.exe"
JAVA="$JDK/bin/java"
JAVAC="$JDK/bin/javac"
JAR="$JDK/bin/jar"
D8_JAR="$(w "$BT/lib/d8.jar")"
SIGNER_JAR="$(w "$BT/lib/apksigner.jar")"
PLATFORM_W="$(w "$PLATFORM")"

SRC="$HERE/app/src/main"
OUT="$HERE/build"
# Signing credentials come from the gitignored keystore/keystore.properties
# (or the KS_PASS env var). Nothing secret is ever committed.
KS="$HERE/keystore/scratchforge.keystore"
KS_PROPS="$HERE/keystore/keystore.properties"
ALIAS="scratchforge"
if [ -z "${KS_PASS:-}" ] && [ -f "$KS_PROPS" ]; then
  KS_PASS="$(grep -E '^storePassword=' "$KS_PROPS" | cut -d= -f2-)"
  ALIAS="$(grep -E '^keyAlias=' "$KS_PROPS" | cut -d= -f2- || echo scratchforge)"
fi
if [ -z "${KS_PASS:-}" ]; then
  echo "No signing password. Set KS_PASS or create $KS_PROPS" >&2
  exit 1
fi

rm -rf "$OUT"
mkdir -p "$OUT/flat" "$OUT/gen" "$OUT/classes" "$OUT/dex"

echo "==> aapt2 compile (resources)"
"$AAPT2" compile --dir "$(w "$SRC/res")" -o "$(w "$OUT/flat/res.zip")"

echo "==> aapt2 link (manifest + resources -> base apk, generates R.java)"
"$AAPT2" link \
  -I "$PLATFORM_W" \
  --manifest "$(w "$SRC/AndroidManifest.xml")" \
  --java "$(w "$OUT/gen")" \
  --min-sdk-version 23 \
  --target-sdk-version 34 \
  --version-code 1 \
  --version-name 1.0 \
  -o "$(w "$OUT/base.apk")" \
  "$(w "$OUT/flat/res.zip")"

echo "==> javac"
: > "$OUT/sources.txt"
while IFS= read -r f; do w "$f" >> "$OUT/sources.txt"; done < <(find "$SRC/java" "$OUT/gen" -name '*.java')
# JDK 21 refuses -bootclasspath together with -target, so android.jar goes on
# the classpath instead. It ships java.* stubs too, so everything resolves.
"$JAVAC" -source 17 -target 17 -encoding UTF-8 -nowarn -Xlint:-options \
  -classpath "$PLATFORM_W" \
  -d "$(w "$OUT/classes")" \
  "@$(w "$OUT/sources.txt")"

if [ -z "$(find "$OUT/classes" -name '*.class' -print -quit)" ]; then
  echo "javac produced no classes — aborting" >&2; exit 1
fi

echo "==> d8 (dex)"
: > "$OUT/classes.txt"
while IFS= read -r f; do w "$f" >> "$OUT/classes.txt"; done < <(find "$OUT/classes" -name '*.class')
"$JAVA" -cp "$D8_JAR" com.android.tools.r8.D8 \
  --release --min-api 23 \
  --lib "$PLATFORM_W" \
  --output "$(w "$OUT/dex")" \
  "@$(w "$OUT/classes.txt")"

echo "==> package (add classes.dex to the resource apk)"
cp "$OUT/base.apk" "$OUT/unsigned.apk"
( cd "$OUT/dex" && "$JAR" uf "$(w "$OUT/unsigned.apk")" classes.dex )

echo "==> zipalign"
"$ZIPALIGN" -f -p 4 "$(w "$OUT/unsigned.apk")" "$(w "$OUT/aligned.apk")"

echo "==> apksigner (v1 + v2 + v3)"
"$JAVA" -jar "$SIGNER_JAR" sign \
  --ks "$(w "$KS")" --ks-key-alias "$ALIAS" \
  --ks-pass "pass:$KS_PASS" --key-pass "pass:$KS_PASS" \
  --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true \
  --out "$(w "$OUT/scratchforge.apk")" \
  "$(w "$OUT/aligned.apk")"

echo "==> verify"
"$JAVA" -jar "$SIGNER_JAR" verify --verbose --print-certs "$(w "$OUT/scratchforge.apk")" | head -8

ls -la "$OUT/scratchforge.apk"
echo "APK: $OUT/scratchforge.apk"
