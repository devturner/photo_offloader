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
DISKUTIL="${PHOTO_OFFLOADER_DISKUTIL:-/usr/sbin/diskutil}"
EJECT_STATE_FILE="${PHOTO_OFFLOADER_EJECT_STATE:-$INSTALL_DIR/.last_camera_eject_attempt}"
# launchd does not load interactive shell startup files. Keep common Homebrew
# paths available to ExifTool, ffmpeg, and other optional media helpers.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

log() {
    mkdir -p "$(dirname "$LOG")"
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

# Run background imports under a small supervisor. If Python exits unsuccessfully,
# clear this mount's handled-state so the next trigger poll can retry instead of
# silently treating a failed launch as a completed import.
start_background_import() {
    local source_path="$1"
    local import_log="$HOME/.camera_transfer.log"
    local supervisor='
python="$1"
importer="$2"
source_path="$3"
state_file="$4"
trigger_log="$5"
install_dir="$6"
cd "$install_dir" || exit 125
if "$python" "$importer" --source "$source_path"; then
    printf "[%s] Background importer exited successfully for: %s\\n" "$(date "+%Y-%m-%d %H:%M:%S")" "$source_path" >> "$trigger_log"
else
    result=$?
    printf "[%s] Background importer failed with status %s for: %s\\n" "$(date "+%Y-%m-%d %H:%M:%S")" "$result" "$source_path" >> "$trigger_log"
    if [[ -f "$state_file" ]] && [[ "$(cat "$state_file")" == "$source_path" ]]; then
        rm -f "$state_file"
        printf "[%s] Cleared trigger state so this card can be retried.\\n" "$(date "+%Y-%m-%d %H:%M:%S")" >> "$trigger_log"
    fi
fi
'
    # launchd jobs may be short-lived. Detach the supervisor and importer from
    # that job explicitly, with a predictable Homebrew/system PATH and working
    # directory; otherwise a successful card-detection log can be followed by
    # no visible importer progress until a terminal is opened.
    printf '%s\n' "$source_path" > "$STATE_FILE"
    nohup /bin/bash -c "$supervisor" photo-offloader-import-supervisor \
        "$PYTHON" "$IMPORTER" "$source_path" "$STATE_FILE" "$LOG" "$INSTALL_DIR" \
        >> "$import_log" 2>&1 </dev/null &
    local pid=$!
    log "Started detached background importer supervisor (pid $pid) for: $source_path"
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
    rm -f "$STATE_FILE" "$EJECT_STATE_FILE"
    log "No recognized camera DCIM currently mounted."
    exit 0
fi

# The same mounted card can cause several /Volumes events. Do not launch it
# again until the card disappears and the state file is cleared.
if [[ -f "$STATE_FILE" ]] && [[ "$(cat "$STATE_FILE")" == "$camera_dcim" ]]; then
    # A state entry is written at launch time. Never eject while the importer is
    # still active; once it has exited, the completed/no-work mount can be ejected.
    if "$PGREP" -f "$IMPORTER" >/dev/null 2>&1; then
        if [[ ! -f "$EJECT_STATE_FILE" ]] || [[ "$(cat "$EJECT_STATE_FILE")" != "$camera_dcim:running" ]]; then
            log "Camera already handled for this mount; importer is still running, keeping card mounted: $camera_dcim"
            printf '%s\n' "$camera_dcim:running" > "$EJECT_STATE_FILE"
        fi
        exit 0
    fi

    # Suppress duplicate success/eject messages after a successful eject. A
    # failed eject is deliberately retried on the next volume poll.
    if [[ -f "$EJECT_STATE_FILE" ]] && [[ "$(cat "$EJECT_STATE_FILE")" == "$camera_dcim:ejected" ]]; then
        exit 0
    fi

    # Only the detached supervisor records this explicit success line. Terminal
    # launches perform their own success-gated ejection inside the importer.
    if ! grep -Fq "Background importer exited successfully for: $camera_dcim" "$LOG"; then
        if [[ ! -f "$EJECT_STATE_FILE" ]] || [[ "$(cat "$EJECT_STATE_FILE")" != "$camera_dcim:unconfirmed" ]]; then
            log "Camera source already handled for this mount, but no successful completion was recorded; leaving card mounted."
            printf '%s\\n' "$camera_dcim:unconfirmed" > "$EJECT_STATE_FILE"
        fi
        exit 0
    fi

    volume_path="${camera_dcim%/DCIM}"
    log "Camera source already handled for this mount; no importer is running. Requesting safe ejection: $volume_path"
    if "$DISKUTIL" eject "$volume_path" >> "$LOG" 2>&1; then
        printf '%s\n' "$camera_dcim:ejected" > "$EJECT_STATE_FILE"
        log "Card ejected successfully: $volume_path"
    else
        log "Card ejection failed; card remains mounted and will be retried: $volume_path"
        rm -f "$EJECT_STATE_FILE"
    fi
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
            start_background_import "$camera_dcim"
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
    start_background_import "$camera_dcim"
    log "Terminal output disabled; importer is running in the background for: $camera_dcim"
fi
