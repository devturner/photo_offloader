#!/bin/bash
# Runs one USB backup started by the trigger. A child of the short-lived trigger job can be killed
# by launchd, so it's submitted as its own job, which must be removed or launchd would rerun it.
set -u
python="$1"; backup="$2"; trigger_log="$3"; install_dir="$4"; label="$5"
LAUNCHCTL="${PHOTO_OFFLOADER_LAUNCHCTL:-/bin/launchctl}"
log() { printf '[%s] %s\n' "$(/bin/date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$trigger_log"; }
finish() {
    local result=$?
    trap - EXIT HUP INT TERM
    "$LAUNCHCTL" remove "$label" >/dev/null 2>&1 || log "WARNING: could not remove launchd job: $label"
    exit "$result"
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
cd "$install_dir" || exit 125
"$python" "$backup"
result=$?
log "USB backup finished with status $result"
exit "$result"
