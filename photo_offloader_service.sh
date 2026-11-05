#!/bin/bash
# Start, stop and inspect the Photo Offloader background service.
#
# Photo Offloader runs as two LaunchAgents:
#   com.user.photooffloader           the trigger: watches for camera cards
#   com.user.photooffloader.menubar   the menu-bar app (KeepAlive)
# Quitting the menu bar or closing the dashboard leaves the trigger running.
# This script is the supported way to shut the whole thing down (and back up).
#
#   photo_offloader_service.sh status
#   photo_offloader_service.sh stop [--wait | --force] [--disable] [--keep-ui]
#   photo_offloader_service.sh start
#   photo_offloader_service.sh restart
#
# `stop` always unloads the trigger first, so no new import can begin. It never
# interrupts a running transfer unless you pass --force:
#   --wait     let the current transfer finish, then stop the menu bar too
#   --force    cancel the transfer (the importer shuts down cleanly on SIGTERM)
#   --disable  also keep the agents from starting at the next login
#   --keep-ui  stop only the trigger: leave the menu bar and dashboards running so they
#              can show "stopped" and offer Start (this is what their Stop buttons use)
# Without --disable, a stopped service comes back at next login or on `start`.
set -uo pipefail

INSTALL_DIR="${PHOTO_OFFLOADER_INSTALL_DIR:-$HOME/Library/Application Support/PhotoOffloader}"
AGENTS_DIR="${PHOTO_OFFLOADER_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LAUNCHCTL="${PHOTO_OFFLOADER_LAUNCHCTL:-/bin/launchctl}"
PGREP="${PHOTO_OFFLOADER_PGREP:-/usr/bin/pgrep}"
KILL="${PHOTO_OFFLOADER_KILL:-/bin/kill}"
POLL_SECONDS="${PHOTO_OFFLOADER_STOP_POLL_SECONDS:-2}"
WAIT_SECONDS="${PHOTO_OFFLOADER_STOP_WAIT_SECONDS:-1800}"
TERM_GRACE_SECONDS="${PHOTO_OFFLOADER_STOP_TERM_GRACE_SECONDS:-15}"

LABEL="com.user.photooffloader"
UI_LABEL="com.user.photooffloader.menubar"
IMPORT_LABEL="com.user.photooffloader.import"
DOMAIN="gui/$(id -u)"
IMPORTER="$INSTALL_DIR/camera_offloader_v2.py"
DASHBOARD="$INSTALL_DIR/photo_offloader_ui.py"
PAUSE_FILE="${PHOTO_OFFLOADER_PAUSE_FILE:-$INSTALL_DIR/.paused}"

usage() {
    cat <<'USAGE'
Usage: photo_offloader_service.sh <command>

  status                          Show what is running.
  stop [--wait|--force] [--disable] [--keep-ui]
                                  Stop the service and menu bar. A running transfer
                                  is never interrupted unless --force is given;
                                  --wait lets it finish first. --keep-ui leaves the
                                  menu bar and dashboards open so you can start again.
  start                           Start the service and menu bar.
  restart                         Stop (after any running transfer) and start again.
USAGE
}

is_loaded() { "$LAUNCHCTL" print "$DOMAIN/$1" >/dev/null 2>&1; }

importer_pids() { "$PGREP" -f "$IMPORTER" 2>/dev/null || true; }

transfer_running() {
    [[ -n "$(importer_pids)" ]] || is_loaded "$IMPORT_LABEL"
}

unload() {
    local label="$1"
    is_loaded "$label" || return 0
    "$LAUNCHCTL" bootout "$DOMAIN/$label" >/dev/null 2>&1
}

close_dashboards() {
    local pid
    for pid in $("$PGREP" -f "$DASHBOARD" 2>/dev/null || true); do
        "$KILL" -TERM "$pid" 2>/dev/null || true
    done
}

wait_for_transfer_to_end() {  # $1 = seconds to wait
    local deadline=$(( $(date +%s) + $1 ))
    while transfer_running; do
        [[ "$(date +%s)" -lt "$deadline" ]] || return 1
        sleep "$POLL_SECONDS"
    done
    return 0
}

cmd_status() {
    local service="stopped" menubar="stopped" imports="active" transfer="none"
    is_loaded "$LABEL" && service="running"
    is_loaded "$UI_LABEL" && menubar="running"
    [[ -f "$PAUSE_FILE" ]] && imports="paused"
    transfer_running && transfer="in progress"
    printf 'Service:  %s\n' "$service"
    printf 'Menu bar: %s\n' "$menubar"
    printf 'Imports:  %s\n' "$imports"
    printf 'Transfer: %s\n' "$transfer"
    [[ "$service" == "running" ]] && return 0
    return 3
}

cmd_stop() {
    local mode="" disable=0 keep_ui=0 arg
    for arg in "$@"; do
        case "$arg" in
            --wait|--force) mode="$arg" ;;
            --disable) disable=1 ;;
            --keep-ui) keep_ui=1 ;;
            *) echo "Unknown option for stop: $arg" >&2; usage >&2; return 2 ;;
        esac
    done

    if [[ "$disable" -eq 1 ]]; then
        "$LAUNCHCTL" disable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
        "$LAUNCHCTL" disable "$DOMAIN/$UI_LABEL" >/dev/null 2>&1 || true
    fi

    if ! is_loaded "$LABEL" && { [[ "$keep_ui" -eq 1 ]] || ! is_loaded "$UI_LABEL"; }; then
        echo "Photo Offloader is already stopped."
        return 0
    fi

    if transfer_running && [[ -z "$mode" ]]; then
        echo "A transfer is in progress, so nothing was stopped."
        echo "  Re-run with --wait to stop after the transfer finishes,"
        echo "  or with --force to cancel the transfer and stop now."
        return 3
    fi

    # Unload the trigger first so no new import can start while we wind down.
    unload "$LABEL"

    if transfer_running; then
        if [[ "$mode" == "--force" ]]; then
            local pid
            for pid in $(importer_pids); do
                "$KILL" -TERM "$pid" 2>/dev/null || true
            done
            wait_for_transfer_to_end "$TERM_GRACE_SECONDS" \
                || echo "Warning: the transfer did not exit within ${TERM_GRACE_SECONDS}s; it may still be shutting down."
        else
            echo "Waiting for the current transfer to finish (new imports are already disabled)..."
            if ! wait_for_transfer_to_end "$WAIT_SECONDS"; then
                echo "Timed out waiting for the transfer. The service is stopped for new imports;"
                echo "the menu-bar app was left running. Run 'stop' again once it finishes."
                return 3
            fi
        fi
    fi

    if [[ "$keep_ui" -eq 1 ]]; then
        echo "Photo Offloader stopped. Start it again from the dashboard, the menu bar, or 'photo_offloader_service.sh start'."
    else
        close_dashboards
        # The menu-bar app may be the process that called us, so it goes last.
        unload "$UI_LABEL"
        echo "Photo Offloader stopped. Run 'photo_offloader_service.sh start' to bring it back."
    fi
    [[ "$disable" -eq 1 ]] && echo "It will stay off at login until you run 'start'."
    return 0
}

cmd_start() {
    local label plist failed=0
    for label in "$LABEL" "$UI_LABEL"; do
        "$LAUNCHCTL" enable "$DOMAIN/$label" >/dev/null 2>&1 || true
        is_loaded "$label" && continue
        plist="$AGENTS_DIR/$label.plist"
        if [[ ! -f "$plist" ]]; then
            echo "$label is not installed. Run ./install.sh first." >&2
            failed=1
            continue
        fi
        if ! "$LAUNCHCTL" bootstrap "$DOMAIN" "$plist" >/dev/null 2>&1; then
            echo "Could not load $label from $plist" >&2
            failed=1
        fi
    done
    [[ "$failed" -eq 0 ]] || return 1
    echo "Photo Offloader started."
}

case "${1:-}" in
    status)  cmd_status ;;
    stop)    shift; cmd_stop "$@" ;;
    start)   cmd_start ;;
    restart)
        cmd_stop --wait || exit $?
        cmd_start
        ;;
    -h|--help|help) usage ;;
    *)       usage >&2; exit 2 ;;
esac
