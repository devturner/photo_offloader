#!/bin/bash
# Triggered by launchd when /Volumes changes. It launches the importer only
# when a configured camera DCIM directory is actually present.

set -u

INSTALL_DIR="$HOME/Library/Application Support/PhotoOffloader"
PYTHON="$INSTALL_DIR/venv/bin/python3"
IMPORTER="$INSTALL_DIR/camera_offloader_v2.py"
LOG="$HOME/.camera_transfer_trigger.log"

CARD_PATTERNS=(
  "/Volumes/NIKON*/DCIM"
  "/Volumes/SONY*/DCIM"
  "/Volumes/EOS*/DCIM"
)

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

if [[ ! -x "$PYTHON" || ! -f "$IMPORTER" ]]; then
  log "Importer installation not found."
  exit 0
fi

for pattern in "${CARD_PATTERNS[@]}"; do
  for dcim in $pattern; do
    if [[ -d "$dcim" ]]; then
      log "Recognized camera source: $dcim"
      /usr/bin/osascript -e "tell application \"Terminal\" to do script \"'$PYTHON' '$IMPORTER' --source '$dcim'\"" >/dev/null 2>&1 || \
        log "Failed to launch Terminal importer for $dcim"
      exit 0
    fi
  done
done

log "Volume event ignored: no recognized camera DCIM found."
exit 0
