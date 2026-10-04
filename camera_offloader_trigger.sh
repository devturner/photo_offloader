#!/bin/bash
# launchd trigger for Photo Offloader.
# Watches /Volumes indirectly via launchd and launches an import only when a
# recognized camera DCIM directory is mounted. Paths are kept as shell words so
# volume names such as "NIKON Z 8" are never split or re-quoted through nested
# shells.

set -euo pipefail

INSTALL_DIR="${PHOTO_OFFLOADER_INSTALL_DIR:-$HOME/Library/Application Support/PhotoOffloader}"
PYTHON="${PHOTO_OFFLOADER_PYTHON:-$INSTALL_DIR/venv/bin/python3}"
IMPORTER="${PHOTO_OFFLOADER_IMPORTER:-$INSTALL_DIR/camera_offloader_v2.py}"
LOG="${PHOTO_OFFLOADER_TRIGGER_LOG:-$HOME/.camera_transfer_trigger.log}"
STATE_FILE="${PHOTO_OFFLOADER_TRIGGER_STATE:-$INSTALL_DIR/.last_camera_source}"
LOCK_DIR="${PHOTO_OFFLOADER_TRIGGER_LOCK:-$INSTALL_DIR/.trigger.lock}"
SETTLE_SECONDS="${PHOTO_OFFLOADER_TRIGGER_SETTLE_SECONDS:-1}"
PGREP="${PHOTO_OFFLOADER_PGREP:-/usr/bin/pgrep}"
OSASCRIPT="${PHOTO_OFFLOADER_OSASCRIPT:-/usr/bin/osascript}"
VOLUMES_ROOT="${PHOTO_OFFLOADER_VOLUMES_ROOT:-/Volumes}"

log() {
    mkdir -p "$(dirname "$LOG")"
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

if [[ ! -x "$PYTHON" || ! -f "$IMPORTER" ]]; then
    log "Importer installation not found."
    exit 0
fi

mkdir -p "$(dirname "$STATE_FILE")"

# launchd can deliver several /Volumes events while a device is mounting. Use
# an atomic directory creation as a short-lived trigger lock. This is separate
# from the Python import lock, which remains the final concurrency safeguard.
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "Another trigger invocation is already evaluating the mounted volumes; ignoring event."
    exit 0
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

sleep "$SETTLE_SECONDS"

camera_dcim=""
for volume in "$VOLUMES_ROOT"/NIKON* "$VOLUMES_ROOT"/SONY* "$VOLUMES_ROOT"/EOS*; do
    [[ -d "$volume" ]] || continue
    if [[ -d "$volume/DCIM" ]]; then
        camera_dcim="$volume/DCIM"
        break
    fi
done

if [[ -z "$camera_dcim" ]]; then
    rm -f "$STATE_FILE"
    log "Volume event ignored: no recognized camera DCIM found."
    exit 0
fi

# The same mounted card can cause several /Volumes events. Do not launch it
# again until the card disappears and the state file is cleared.
if [[ -f "$STATE_FILE" ]] && [[ "$(cat "$STATE_FILE")" == "$camera_dcim" ]]; then
    log "Camera source already handled for this mount: $camera_dcim"
    exit 0
fi

# The importer has its own advisory lock. Avoid opening extra Terminal windows
# while an import is already running, but retain the importer lock as the final
# safety barrier against races.
if "$PGREP" -f -- "$IMPORTER" >/dev/null 2>&1; then
    log "Importer already running; ignoring duplicate volume event for $camera_dcim"
    exit 0
fi

log "Recognized camera source: $camera_dcim"

# Build a shell-safe command without interpolating the path into AppleScript.
# %q is bash-specific and preserves spaces and other shell metacharacters in
# the volume name.
command="$(printf '%q ' "$PYTHON" "$IMPORTER" --source "$camera_dcim")"

if "$OSASCRIPT" - "$command" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
    tell application "Terminal"
        do script (item 1 of argv)
    end tell
end run
APPLESCRIPT
then
    printf '%s\n' "$camera_dcim" > "$STATE_FILE"
    log "Launched importer for: $camera_dcim"
else
    log "Failed to launch Terminal importer for: $camera_dcim"
fi
