#!/bin/bash
# launchd trigger for Photo Offloader.
# Watches /Volumes indirectly via launchd and launches an import when a mounted
# volume contains a top-level DCIM directory. Paths are kept as shell words so
# volume names such as "NIKON Z 8" are never split or re-quoted through nested
# shells.

set -euo pipefail

INSTALL_DIR="${PHOTO_OFFLOADER_INSTALL_DIR:-$HOME/Library/Application Support/PhotoOffloader}"
PYTHON="${PHOTO_OFFLOADER_PYTHON:-$INSTALL_DIR/venv/bin/python3}"
IMPORTER="${PHOTO_OFFLOADER_IMPORTER:-$INSTALL_DIR/camera_offloader_v2.py}"
DASHBOARD="${PHOTO_OFFLOADER_DASHBOARD:-$INSTALL_DIR/photo_offloader_ui.py}"
OPEN_DASHBOARD="${PHOTO_OFFLOADER_OPEN_DASHBOARD:-1}"
LOG="${PHOTO_OFFLOADER_TRIGGER_LOG:-$HOME/.camera_transfer_trigger.log}"
LOG_MAX_BYTES="${PHOTO_OFFLOADER_LOG_MAX_BYTES:-1048576}"
LOG_BACKUPS="${PHOTO_OFFLOADER_LOG_BACKUPS:-2}"
LOG_SIGNATURE_FILE="${PHOTO_OFFLOADER_TRIGGER_LOG_SIGNATURE:-$INSTALL_DIR/.trigger-log-signature}"
STATE_FILE="${PHOTO_OFFLOADER_TRIGGER_STATE:-$INSTALL_DIR/.last_camera_source}"
# Identity (volume UUID) of the card that STATE_FILE refers to. The path alone is
# not enough: every card from the same camera mounts under the same volume name.
IDENTITY_FILE="${PHOTO_OFFLOADER_TRIGGER_IDENTITY:-$STATE_FILE.identity}"
# Epoch seconds when a successfully handled card was first seen missing. If the
# same card is back within REMOUNT_GRACE_SECONDS it was mount churn; after that
# it was really removed (e.g. to shoot more) and re-inserting starts a new session.
ABSENT_FILE="${PHOTO_OFFLOADER_TRIGGER_ABSENT:-$STATE_FILE.absent}"
REMOUNT_GRACE_SECONDS="${PHOTO_OFFLOADER_REMOUNT_GRACE_SECONDS:-20}"
PAUSE_FILE="${PHOTO_OFFLOADER_PAUSE_FILE:-$INSTALL_DIR/.paused}"
LOCK_DIR="${PHOTO_OFFLOADER_TRIGGER_LOCK:-$INSTALL_DIR/.trigger.lock}"
SETTLE_SECONDS="${PHOTO_OFFLOADER_TRIGGER_SETTLE_SECONDS:-1}"
PGREP="${PHOTO_OFFLOADER_PGREP:-/usr/bin/pgrep}"
OSASCRIPT="${PHOTO_OFFLOADER_OSASCRIPT:-/usr/bin/osascript}"
TERMINAL_APP="${PHOTO_OFFLOADER_TERMINAL_APP:-iTerm2}"
TERMINAL_OUTPUT_FILE="${PHOTO_OFFLOADER_TERMINAL_OUTPUT_FILE:-$INSTALL_DIR/.terminal-output-enabled}"
VOLUMES_ROOT="${PHOTO_OFFLOADER_VOLUMES_ROOT:-/Volumes}"
DISKUTIL="${PHOTO_OFFLOADER_DISKUTIL:-/usr/sbin/diskutil}"
LAUNCHCTL="${PHOTO_OFFLOADER_LAUNCHCTL:-/bin/launchctl}"
IMPORT_WORKER_LABEL="com.user.photooffloader.import"
EJECT_STATE_FILE="${PHOTO_OFFLOADER_EJECT_STATE:-$INSTALL_DIR/.last_camera_eject_attempt}"
IMPORT_STATUS_FILE="${PHOTO_OFFLOADER_IMPORT_STATUS:-$INSTALL_DIR/.last_camera_import_status}"
AUTO_EJECT_FILE="${PHOTO_OFFLOADER_AUTO_EJECT_FILE:-$INSTALL_DIR/.auto-eject-enabled}"
# launchd does not load interactive shell startup files. Keep common Homebrew
# paths available to ExifTool, ffmpeg, and other optional media helpers.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

# The trigger runs every 30 seconds, so logging must be quiet when nothing
# changes. Lines are buffered for the run and written once at exit, and only if
# they differ from the previous run's lines (timestamps ignored). A run that
# logs nothing resets that comparison, so a genuine repeat event is still logged.
# The log is size-capped: it rotates to $LOG.1 .. $LOG.$LOG_BACKUPS.
LOG_BUFFER=()
LOG_MESSAGES=()

log() {
    LOG_BUFFER+=("[$(date '+%Y-%m-%d %H:%M:%S')] $*")
    LOG_MESSAGES+=("$*")
}

rotate_log() {
    [[ -f "$LOG" ]] || return 0
    local size i
    size="$(wc -c < "$LOG" 2>/dev/null | tr -d ' ')"
    [[ "${size:-0}" -gt "$LOG_MAX_BYTES" ]] || return 0
    rm -f "$LOG.$LOG_BACKUPS"
    for ((i = LOG_BACKUPS - 1; i >= 1; i--)); do
        [[ -f "$LOG.$i" ]] && mv -f "$LOG.$i" "$LOG.$((i + 1))"
    done
    mv -f "$LOG" "$LOG.1"
    return 0
}

flush_log() {
    if [[ ${#LOG_BUFFER[@]} -eq 0 ]]; then
        rm -f "$LOG_SIGNATURE_FILE" 2>/dev/null || true
        return 0
    fi
    local signature previous=""
    signature="$(printf '%s\n' "${LOG_MESSAGES[@]}" | cksum)"
    [[ -f "$LOG_SIGNATURE_FILE" ]] && previous="$(cat "$LOG_SIGNATURE_FILE" 2>/dev/null || true)"
    if [[ "$signature" != "$previous" ]]; then
        mkdir -p "$(dirname "$LOG")"
        rotate_log
        printf '%s\n' "${LOG_BUFFER[@]}" >> "$LOG"
        mkdir -p "$(dirname "$LOG_SIGNATURE_FILE")" 2>/dev/null || true
        printf '%s\n' "$signature" > "$LOG_SIGNATURE_FILE" 2>/dev/null || true
    fi
    LOG_BUFFER=()
    LOG_MESSAGES=()
}
trap flush_log EXIT

# Identity of a mounted volume: its VolumeUUID (empty when it cannot be read).
volume_identity() {
    "$DISKUTIL" info -plist "$1" 2>/dev/null \
        | tr '\n' ' ' \
        | sed -n 's/.*<key>VolumeUUID<\/key>[[:space:]]*<string>\([^<]*\)<\/string>.*/\1/p' \
        | head -n 1 || true
}

build_provenance() {
    "$PYTHON" - "$INSTALL_DIR/build_info.json" <<'PYBUILDINFO' 2>/dev/null || printf 'version=unknown git_commit=unknown git_branch=unknown'
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
}

# Run background imports under a small supervisor. If Python exits unsuccessfully,
# clear this mount's handled-state so the next trigger poll can retry instead of
# silently treating a failed launch as a completed import.
start_background_import() {
    local source_path="$1"
    local import_log="$HOME/.camera_transfer.log"
    local supervisor_script="${PHOTO_OFFLOADER_SUPERVISOR:-$INSTALL_DIR/photo_offloader_import_supervisor.sh}"

    printf '%s\n' "$source_path" > "$STATE_FILE"
    if [[ ! -f "$supervisor_script" ]]; then
        log "Background supervisor script is missing: $supervisor_script"
        rm -f "$STATE_FILE"
        return 1
    fi

    # A child of a short-lived LaunchAgent can be terminated by launchd even
    # when nohup is used. Submit the worker as its own user-domain launchd job.
    # The unique label also prevents duplicate workers for a single user session.
    local domain="gui/$(id -u)"
    if "$LAUNCHCTL" print "$domain/$IMPORT_WORKER_LABEL" >/dev/null 2>&1; then
        log "Import worker job is already registered; preserving handled state and not submitting a duplicate: $IMPORT_WORKER_LABEL"
        return 0
    fi

    # The importer has its own eject implementation. Pass an explicit no-eject
    # setting when the trigger's auto-eject opt-in marker is absent.
    local no_eject="0"
    [[ -f "$AUTO_EJECT_FILE" ]] || no_eject="1"
    if [[ ! -d "$source_path" ]]; then
        log "Source DCIM disappeared before worker submission; skipping launch: $source_path"
        rm -f "$STATE_FILE"
        return 0
    fi
    flush_log
    if "$LAUNCHCTL" submit -l "$IMPORT_WORKER_LABEL" -- /bin/bash "$supervisor_script" \
        "$PYTHON" "$IMPORTER" "$source_path" "$STATE_FILE" "$LOG" "$INSTALL_DIR" "$import_log" "$IMPORT_STATUS_FILE" "$no_eject" \
        >> "$import_log" 2>&1; then
        log "Submitted importer supervisor to launchd as $IMPORT_WORKER_LABEL for: $source_path"
    else
        local result=$?
        log "ERROR: launchctl could not submit import worker (status $result) for: $source_path"
        rm -f "$STATE_FILE"
        return "$result"
    fi
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
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true; flush_log' EXIT

sleep "$SETTLE_SECONDS"

# Pause only blocks new automatic import sessions; an active copy is never killed.
if [[ -f "$PAUSE_FILE" ]]; then
    log "Automatic import is paused; ignoring volume event."
    exit 0
fi

camera_dcim=""
# Any mounted volume with a top-level DCIM directory is eligible.
# Volume names and camera-brand markers are intentionally ignored.
for volume in "$VOLUMES_ROOT"/*; do
    [[ -d "$volume" && -d "$volume/DCIM" ]] || continue
    camera_dcim="$volume/DCIM"
    log "Recognized mounted volume with DCIM directory: $camera_dcim"
    break
done

camera_identity=""
if [[ -n "$camera_dcim" ]]; then
    camera_identity="$(volume_identity "${camera_dcim%/DCIM}")"
    # Cards from the same camera body share a volume name, so a matching path
    # is not proof that this is the card that was already handled.
    if [[ -f "$STATE_FILE" && "$(cat "$STATE_FILE" 2>/dev/null)" == "$camera_dcim" ]]; then
        handled_identity=""
        [[ -f "$IDENTITY_FILE" ]] && handled_identity="$(cat "$IDENTITY_FILE" 2>/dev/null || true)"
        if [[ -n "$handled_identity" && -n "$camera_identity" && "$handled_identity" != "$camera_identity" ]]; then
            log "A different card ($camera_identity) is mounted at the path of the previously handled card ($handled_identity); treating it as a new card: $camera_dcim"
            rm -f "$STATE_FILE" "$EJECT_STATE_FILE" "$IDENTITY_FILE"
        fi
    fi
    if [[ -f "$ABSENT_FILE" && -f "$STATE_FILE" && "$(cat "$STATE_FILE" 2>/dev/null)" == "$camera_dcim" ]]; then
        absent_since="$(cat "$ABSENT_FILE" 2>/dev/null || true)"
        [[ "$absent_since" =~ ^[0-9]+$ ]] || absent_since=0
        absent_for=$(( $(date +%s) - absent_since ))
        if (( absent_for >= REMOUNT_GRACE_SECONDS )); then
            log "Card re-inserted after ${absent_for}s away; starting a new import session to pick up new files: $camera_dcim"
            rm -f "$STATE_FILE" "$EJECT_STATE_FILE" "$IDENTITY_FILE"
        fi
    fi
    rm -f "$ABSENT_FILE"
fi

if [[ -z "$camera_dcim" ]]; then
    # A transient /Volumes/DCIM visibility gap must not make a successfully
    # handled card eligible for another import. Keep successful state across
    # brief mount churn; failed/incomplete attempts can still be retried after
    # the source is genuinely removed. This deliberately prioritizes one run
    # per successful card session over automatic re-import after a clean success.
    handled_source=""
    if [[ -f "$STATE_FILE" ]]; then
        handled_source="$(cat "$STATE_FILE" 2>/dev/null || true)"
    fi
    if [[ -n "$handled_source" ]] && grep -Fq "success|$handled_source|" "$IMPORT_STATUS_FILE" 2>/dev/null; then
        if [[ ! -f "$ABSENT_FILE" ]]; then
            date +%s > "$ABSENT_FILE"
            log "No recognized camera DCIM currently mounted; preserving successful handled-state through mount visibility gap: $handled_source"
        fi
    elif [[ -f "$STATE_FILE" ]]; then
        rm -f "$STATE_FILE" "$EJECT_STATE_FILE" "$IDENTITY_FILE" "$ABSENT_FILE"
        log "No recognized camera DCIM currently mounted; cleared incomplete handled-state."
    else
        rm -f "$EJECT_STATE_FILE" "$IDENTITY_FILE" "$ABSENT_FILE"  # idle poll: nothing to report
    fi
    exit 0
fi

# The same mounted card can cause several /Volumes events. Do not launch it
# again until the card disappears and the state file is cleared.
if [[ -f "$STATE_FILE" ]] && [[ "$(cat "$STATE_FILE")" == "$camera_dcim" ]]; then
    # A submitted launchd worker can remain registered briefly between runs.
    # Never erase handled-state just because launchd still knows the label:
    # doing so makes the next periodic poll submit the same failing source again.
    local_domain="gui/$(id -u)"
    if "$LAUNCHCTL" print "$local_domain/$IMPORT_WORKER_LABEL" >/dev/null 2>&1; then
        log "Import worker remains registered; preserving handled state and suppressing duplicate launch: $camera_dcim"
        exit 0
    fi

    # A state entry is written at launch time. Never eject while the importer is
    # still active; once it has exited, the completed/no-work mount can be ejected.
    if "$PGREP" -f "$IMPORTER" >/dev/null 2>&1; then
        if [[ ! -f "$EJECT_STATE_FILE" ]] || [[ "$(cat "$EJECT_STATE_FILE")" != "$camera_dcim:running" ]]; then
            log "Camera already handled for this mount; importer is still running, keeping card mounted: $camera_dcim"
            printf '%s\n' "$camera_dcim:running" > "$EJECT_STATE_FILE"
        fi
        exit 0
    fi

    # A recorded failure is terminal for this mount. Do not immediately relaunch
    # a failing/disconnected source on every poll; the no-DCIM path below clears
    # state on unmount, allowing a fresh attempt after the card is reconnected.
    if grep -Fq "failed|$camera_dcim|" "$IMPORT_STATUS_FILE" 2>/dev/null; then
        log "Previous import failed for this mount; suppressing automatic retry until the card is unmounted: $camera_dcim"
        exit 0
    fi

    # Once a source has been submitted, do not clear its handled-state merely
    # because a completion marker is absent. launchd may unregister the worker
    # before a status write is visible, and clearing state here re-launches the
    # same mounted card on every poll. Failures and incomplete attempts remain
    # suppressed until unmount; a real unmount clears state in the no-DCIM path.
    if [[ ! -f "$TERMINAL_OUTPUT_FILE" ]] && ! grep -Fq "success|$camera_dcim|" "$IMPORT_STATUS_FILE" 2>/dev/null; then
        log "No successful completion record for this already-handled mount; suppressing relaunch until unmount: $camera_dcim"
        exit 0
    fi

    # Suppress duplicate success/eject messages after a successful eject. A
    # failed eject is deliberately retried on the next volume poll.
    if [[ -f "$EJECT_STATE_FILE" ]] && [[ "$(cat "$EJECT_STATE_FILE")" == "$camera_dcim:ejected" ]]; then
        exit 0
    fi

    # Only the detached supervisor records this explicit success line. Terminal
    # launches perform their own success-gated ejection inside the importer.
    # Auto-eject is opt-in. When disabled, leave the card mounted without
    # requiring an import-success marker; that marker only gates actual ejection.
    if [[ ! -f "$AUTO_EJECT_FILE" ]]; then
        if [[ ! -f "$EJECT_STATE_FILE" ]] || [[ "$(cat "$EJECT_STATE_FILE")" != "$camera_dcim:manual" ]]; then
            log "Camera source already handled for this mount; auto-eject is disabled, leaving card mounted for Finder access: $camera_dcim"
            printf '%s\n' "$camera_dcim:manual" > "$EJECT_STATE_FILE"
        fi
        exit 0
    fi

    if ! grep -Fq "success|$camera_dcim|" "$IMPORT_STATUS_FILE" 2>/dev/null; then
        if [[ ! -f "$EJECT_STATE_FILE" ]] || [[ "$(cat "$EJECT_STATE_FILE")" != "$camera_dcim:unconfirmed" ]]; then
            log "Auto-eject is enabled, but no successful completion was recorded; leaving card mounted: $camera_dcim"
            printf '%s\n' "$camera_dcim:unconfirmed" > "$EJECT_STATE_FILE"
        fi
        exit 0
    fi

    volume_path="${camera_dcim%/DCIM}"
    log "Camera source already handled for this mount; no importer is running. Requesting safe ejection: $volume_path"
    if eject_output="$("$DISKUTIL" eject "$volume_path" 2>&1)"; then
        printf '%s\n' "$camera_dcim:ejected" > "$EJECT_STATE_FILE"
        log "Card ejected successfully: $volume_path"
    else
        log "Card ejection failed; card remains mounted and will be retried: $volume_path"
        rm -f "$EJECT_STATE_FILE"
    fi
    [[ -z "$eject_output" ]] || log "diskutil: $eject_output"
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
if [[ ! -d "$camera_dcim" ]]; then
    log "Camera DCIM disappeared before importer launch; skipping stale source: $camera_dcim"
    rm -f "$STATE_FILE" "$EJECT_STATE_FILE" "$IDENTITY_FILE"
    exit 0
fi

log "Recognized camera source: $camera_dcim"
log "Build provenance: $(build_provenance)"
printf '%s\n' "$camera_identity" > "$IDENTITY_FILE"

# Ask for the dashboard when an import starts. LaunchAgent jobs run in the logged-in user's
# GUI session, so the window is visible without a Terminal command. The launcher is
# single-instance and `--if-closed` makes it a no-op while a window or tab is already showing
# the dashboard. Best-effort: a UI failure must never block a safe copy.
if [[ "$OPEN_DASHBOARD" == "1" && -f "$DASHBOARD" ]]; then
    nohup "$PYTHON" "$DASHBOARD" --if-closed >> "$INSTALL_DIR/dashboard.log" 2>&1 </dev/null &
    log "Requested progress dashboard for: $camera_dcim"
fi

# Keep the import attached to the dashboard/status file by default; Terminal output is
# optional and controlled from the dashboard. Both paths use the same importer
# and preserve the shell-safe handling of camera volume names containing spaces.
IMPORTER_EJECT_ARGS=()
if [[ ! -f "$AUTO_EJECT_FILE" ]]; then
    IMPORTER_EJECT_ARGS+=(--no-eject)
fi
command="$(printf '%q ' "$PYTHON" "$IMPORTER" --source "$camera_dcim" ${IMPORTER_EJECT_ARGS[@]+"${IMPORTER_EJECT_ARGS[@]}"})"

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
            nohup "$PYTHON" "$IMPORTER" --source "$camera_dcim" ${IMPORTER_EJECT_ARGS[@]+"${IMPORTER_EJECT_ARGS[@]}"} >> "$HOME/.camera_transfer.log" 2>&1 </dev/null &
            printf '%s\n' "$camera_dcim" > "$STATE_FILE"
        fi
    fi
else
    start_background_import "$camera_dcim"
    log "Terminal output disabled; importer is running in the background for: $camera_dcim"
fi
