#!/bin/bash
set -euo pipefail

DIST="./photo_offloader_v2_dist"
ARCHIVE="./photo_offloader_v2.2.zip"

rm -rf "$DIST" "$ARCHIVE"
mkdir -p "$DIST"

FILES=(
  camera_offloader_v2.py
  camera_offloader_trigger.sh
  install.sh
  uninstall.sh
  running_bash.sh
  README.md
  CHANGELOG.md
  LICENSE
  requirements.txt
  requirements-dev.txt
)

for file in "${FILES[@]}"; do
  [[ -f "$file" ]] || { echo "Missing required bundle file: $file" >&2; exit 1; }
  cp "$file" "$DIST/"
done

chmod +x "$DIST"/*.sh
zip -qr "$ARCHIVE" "$DIST"
rm -rf "$DIST"

echo "Archive created: $ARCHIVE"
