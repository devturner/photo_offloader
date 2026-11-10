#!/bin/bash
# Build an app bundle that runs one of Photo Offloader's Python programs under its own name and
# icon, so macOS doesn't show it as "Python".
#   build_app.sh menubar|app APP_PATH VENV_PYTHON SCRIPT VERSION
# menubar  the menu bar: no Dock icon, started by its LaunchAgent
# app      the app you open: starts everything and shows the dashboard window
# Exits 3 when VENV_PYTHON has no shared library to load (install.sh then runs the program with
# python3 directly) and 1 on any other failure. A failed build leaves nothing at APP_PATH.
set -euo pipefail

usage() {
  echo "Usage: build_app.sh menubar|app APP_PATH VENV_PYTHON SCRIPT VERSION" >&2
  exit 2
}
[[ $# -eq 5 ]] || usage
case "$1" in
  menubar)
    BUNDLE_ID="com.user.photooffloader.menubar"
    EXECUTABLE="photo-offloader-menubar"
    # LSUIElement: a menu-bar app, so no Dock icon and no entry in the app switcher.
    EXTRA_KEYS="    <key>LSUIElement</key>
    <true/>"
    ;;
  app)
    BUNDLE_ID="com.user.photooffloader"
    EXECUTABLE="photo-offloader"
    EXTRA_KEYS=""
    ;;
  *) usage ;;
esac
APP="$2"
PYTHON="$3"
SCRIPT="$4"
VERSION="$5"
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# The venv records Homebrew's opt path, which survives Python patch upgrades, so the library
# path is taken from it rather than from a Cellar version folder.
LIBPYTHON="$("$PYTHON" -I - <<'PY' 2>/dev/null || true
import os
import sysconfig

framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
if framework:
    path = os.path.join(
        sysconfig.get_config_var("PYTHONFRAMEWORKPREFIX"), sysconfig.get_config_var("LDLIBRARY")
    )
else:
    path = os.path.join(sysconfig.get_config_var("LIBDIR"), sysconfig.get_config_var("LDLIBRARY"))
if os.path.isfile(path) and not path.endswith(".a"):
    print(path)
PY
)"
if [[ -z "$LIBPYTHON" ]]; then
  echo "$PYTHON has no shared Python library to load." >&2
  exit 3
fi

STAGING="$(mktemp -d "${TMPDIR:-/tmp}/photo-offloader-app.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
BUNDLE="$STAGING/bundle.app"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources" "$STAGING/AppIcon.iconset"

clang -O2 -Wall -Wextra -Werror -o "$BUNDLE/Contents/MacOS/$EXECUTABLE" "$HERE/launcher.c"

for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$HERE/AppIcon.png" \
    --out "$STAGING/AppIcon.iconset/icon_${size}x${size}.png" >/dev/null
  sips -z "$((size * 2))" "$((size * 2))" "$HERE/AppIcon.png" \
    --out "$STAGING/AppIcon.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$STAGING/AppIcon.iconset" -o "$BUNDLE/Contents/Resources/AppIcon.icns"

printf '%s\n%s\n%s\n' "$LIBPYTHON" "$PYTHON" "$SCRIPT" >"$BUNDLE/Contents/Resources/launcher.conf"

cat >"$BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>Photo Offloader</string>
    <key>CFBundleDisplayName</key>
    <string>Photo Offloader</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleExecutable</key>
    <string>$EXECUTABLE</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundleVersion</key>
    <string>$VERSION</string>
$EXTRA_KEYS
</dict>
</plist>
EOF
plutil -lint -s "$BUNDLE/Contents/Info.plist"

# An ad-hoc signature covers Info.plist and the icon, so macOS sees one consistent app.
codesign --sign - --force "$BUNDLE" 2>/dev/null

rm -rf "$APP"
mv "$BUNDLE" "$APP"
