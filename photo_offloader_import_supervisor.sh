#!/bin/bash
# Standalone detached supervisor for automatic camera imports.
set -u
python="$1"; importer="$2"; source_path="$3"; state_file="$4"
trigger_log="$5"; install_dir="$6"; supervisor_log="$7"; status_file="$8"
no_eject="${9:-1}"
supervisor_pid="$$"; phase="initializing"
log_both() {
    local message="[$(/bin/date '+%Y-%m-%d %H:%M:%S')] $*"
    printf '%s\n' "$message" >> "$trigger_log"
    printf '%s\n' "$message" >> "$supervisor_log"
}
write_status() {
    local tmp="${status_file}.tmp.$$"
    if printf '%s\n' "$1" > "$tmp"; then /bin/mv -f "$tmp" "$status_file"
    else /bin/rm -f "$tmp"; log_both "Could not write import status file: $status_file"; fi
}
clear_state() {
    if [[ -f "$state_file" ]] && [[ "$(/bin/cat "$state_file" 2>/dev/null)" == "$source_path" ]]; then
        /bin/rm -f "$state_file"
        log_both "Cleared trigger state so this card can be retried."
    fi
}
finish() {
    local result=$?
    trap - EXIT
    if [[ "$result" -ne 0 ]]; then
        log_both "Supervisor exited unexpectedly with status $result during phase '$phase' for: $source_path"
        write_status "failed|$source_path|$result"
        # Preserve handled state after failure to prevent tight poll/relaunch loops.
        # The trigger clears it when the source DCIM disappears on unmount.
    fi
    exit "$result"
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
write_status "running|$source_path|$supervisor_pid"
log_both "Background supervisor started (pid $supervisor_pid) for: $source_path"
phase="working-directory"
if ! cd "$install_dir"; then
    log_both "Background supervisor could not enter install directory: $install_dir"
    exit 125
fi
phase="python-launch"
log_both "Launching importer with Python: $python; importer: $importer"
if [[ ! -d "$source_path" ]]; then
    log_both "Source DCIM disappeared before importer launch; refusing stale-path import: $source_path"
    write_status "failed|$source_path|source-missing"
    phase="source-missing"
    exit 0
fi
if [[ "$no_eject" == "1" ]]; then
    log_both "Automatic eject is disabled; passing --no-eject to importer."
    importer_args=(--source "$source_path" --no-eject)
else
    importer_args=(--source "$source_path")
fi
if "$python" "$importer" "${importer_args[@]}"; then
    phase="success-status"
    log_both "Background importer exited successfully for: $source_path"
    write_status "success|$source_path|$supervisor_pid"
    phase="complete"
    exit 0
else
    result=$?
    phase="failed-status"
    log_both "Background importer failed with status $result for: $source_path"
    write_status "failed|$source_path|$result"
    # Keep the failed source marked as handled until unmount/remount. Clearing it
    # here would make the periodic trigger immediately launch the same failure again.
    exit "$result"
fi
