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
DASHBOARD="${PHOTO_OFFLOADER_DASHBOARD:-$INSTALL_DIR/photo_offloader_ui.py}"
OPEN_DASHBOARD="${PHOTO_OFFLOADER_OPEN_DASHBOARD:-1}"
LOG="${PHOTO_OFFLOADER_TRIGGER_LOG:-$HOME/.camera_transfer_trigger.log}"
STATE_FILE="${PHOTO_OFFLOADER_TRIGGER_STATE:-$INSTALL_DIR/.last_camera_source}"
PAUSE_FILE="${PHOTO_OFFLOADER_PAUSE_FILE:-$INSTALL_DIR/.paused}"
LOCK_DIR="${PHOTO_OFFLOADER_TRIGGER_LOCK:-$INSTALL_DIR/.trigger.lock}"
SETTLE_SECONDS="${PHOTO_OFFLOADER_TRIGGER_SETTLE_SECONDS:-1}"
PGREP="${PHOTO_OFFLOADER_PGREP:-/usr/bin/pgrep}"
OSASCRIPT="${PHOTO_OFFLOADER_OSASCRIPT:-/usr/bin/osascript}"
TERMINAL_APP="${PHOTO_OFFLOADER_TERMINAL_APP:-iTerm2}"
TERMINAL_OUTPUT_FILE="${PHOTO_OFFLOADER_TERMINAL_OUTPUT_FILE:-$INSTALL_DIR/.terminal-output-enabled}"
VOLUMES_ROOT="${PHOTO_OFFLOADER_VOLUMES_ROOT:-/Volumes}"

log() {
    mkdir -p "$(dirname "$LOG")"
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

if [[ ! -x "$PYTHON" || ! -f "$IMPORTER" ]]; then
    log "Importer installation not found."
    exit 0
fi

BUILD_INFO_LOG="$("$PYTHON" - "$INSTALL_DIR/build_info.json" <<'PYBUILDINFO' 2>/dev/null || printf 'version=unknown git_commit=unknown git_branch=unknown'
import json
import sys
from pathlib import Path

info = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("version={version} git_commit={git_commit} git_branch={git_branch} git_tag={git_tag} git_dirty={git_dirty}".format(
    version=info.get("version", "unknown"),
    git_commit=info.get("git_commit", "unknown"),
    git_branch=info.get("git_branch", "unknown"),
    git_tag=info.get("git_tag", "unknown"),
    git_dirty=info.get("git_dirty", "unknown"),
))
PYBUILDINFO
)"
log "Build provenance: $BUILD_INFO_LOG"

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

# Pause only blocks new automatic import sessions; an active copy is never killed.
if [[ -f "$PAUSE_FILE" ]]; then
    log "Automatic import is paused; ignoring volume event."
    exit 0
fi

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
    log "No recognized camera DCIM currently mounted."
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
if "$PGREP" -f "$IMPORTER" >/dev/null 2>&1; then
    log "Importer already running; ignoring duplicate volume event for $camera_dcim"
    exit 0
fi

# Check again immediately before launching in case the user paused after the scan.
if [[ -f "$PAUSE_FILE" ]]; then
    log "Automatic import was paused before launch; not starting a copy."
    exit 0
fi

log "Recognized camera source: $camera_dcim"

# Open one dashboard window when an import starts. LaunchAgent jobs run in the
# logged-in user's GUI session, so the Tk window is visible without a Terminal
# command. This is best-effort: a UI failure must never block a safe copy.
if [[ "$OPEN_DASHBOARD" == "1" && -f "$DASHBOARD" ]]; then
    if ! "$PGREP" -f "$DASHBOARD" >/dev/null 2>&1; then
        nohup "$PYTHON" "$DASHBOARD" >> "$INSTALL_DIR/dashboard.log" 2>&1 </dev/null &
        log "Opened progress dashboard for: $camera_dcim"
    else
        log "Progress dashboard is already running."
    fi
fi

# Keep the import attached to the dashboard/status file by default; Terminal output is
# optional and controlled from the dashboard. Both paths use the same importer
# and preserve the shell-safe handling of camera volume names containing spaces.
command="$(printf '%q ' "$PYTHON" "$IMPORTER" --source "$camera_dcim")"

if [[ -f "$TERMINAL_OUTPUT_FILE" ]]; then
    if [[ "$TERMINAL_APP" == "iTerm2" ]]; then
        if "$OSASCRIPT" - "$command" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
    tell application "iTerm2"
        create window with default profile command (item 1 of argv)
    end tell
end run
APPLESCRIPT
        then
            printf '%s\n' "$camera_dcim" > "$STATE_FILE"
            log "Launched iTerm2 importer for: $camera_dcim"
        else
            log "Failed to launch iTerm2 importer; falling back to background import."
            nohup "$PYTHON" "$IMPORTER" --source "$camera_dcim" >> "$HOME/.camera_transfer.log" 2>&1 </dev/null &
            printf '%s\n' "$camera_dcim" > "$STATE_FILE"
        fi
    else
        if "$OSASCRIPT" - "$command" "$TERMINAL_APP" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
    tell application (item 2 of argv)
        do script (item 1 of argv)
    end tell
end run
APPLESCRIPT
        then
            printf '%s\n' "$camera_dcim" > "$STATE_FILE"
            log "Launched $TERMINAL_APP importer for: $camera_dcim"
        else
            log "Failed to launch $TERMINAL_APP importer; falling back to background import."
            nohup "$PYTHON" "$IMPORTER" --source "$camera_dcim" >> "$HOME/.camera_transfer.log" 2>&1 </dev/null &
            printf '%s\n' "$camera_dcim" > "$STATE_FILE"
        fi
    fi
else
    nohup "$PYTHON" "$IMPORTER" --source "$camera_dcim" >> "$HOME/.camera_transfer.log" 2>&1 </dev/null &
    printf '%s\n' "$camera_dcim" > "$STATE_FILE"
    log "Started background importer (Terminal output disabled) for: $camera_dcim"
fi
