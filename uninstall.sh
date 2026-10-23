#!/bin/bash
# Safely stop Photo Offloader background jobs before removing installed files.
set -uo pipefail

INSTALL_DIR="$HOME/Library/Application Support/PhotoOffloader"
DATA_DIR="$HOME/Library/Application Support/PhotoOffloaderData"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LABEL="com.user.photooffloader"
UI_LABEL="com.user.photooffloader.menubar"
IMPORT_LABEL="com.user.photooffloader.import"
VERBOSE=0
DRY_RUN=0
WAIT_SECONDS="${PHOTO_OFFLOADER_UNINSTALL_WAIT_SECONDS:-10}"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"

usage() {
  cat <<'USAGE'
Usage: ./uninstall.sh [--verbose|-v] [--dry-run] [--help]

  -v, --verbose  Print each cleanup step and its result.
      --dry-run  Report what would be done without changing anything.
      --help     Show this help.

The media catalog and imported photos are always preserved by default.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -v|--verbose) VERBOSE=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

log() {
  local level="$1"; shift
  if [[ "$level" == "INFO" || "$VERBOSE" -eq 1 ]]; then
    printf '[%s] %-5s %s\n' "$(/bin/date '+%Y-%m-%d %H:%M:%S')" "$level" "$*"
  fi
}
run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log INFO "DRY RUN: $*"
    return 0
  fi
  if [[ "$VERBOSE" -eq 1 ]]; then log INFO "Running: $*"; fi
  "$@"
  local rc=$?
  if [[ "$rc" -eq 0 ]]; then log DEBUG "Succeeded: $*"; else log WARN "Command exited $rc: $*"; fi
  return 0
}
exists_in_launchd() {
  launchctl print "$DOMAIN/$1" >/dev/null 2>&1
}
bootout_agent() {
  local label="$1" plist="$2"
  if exists_in_launchd "$label"; then
    log INFO "Unloading LaunchAgent $label"
    if [[ "$DRY_RUN" -eq 0 ]]; then
      if launchctl bootout "$DOMAIN/$label" 2>/dev/null; then
        log DEBUG "Unloaded $label"
      else
        log WARN "Could not bootout $label by service target; trying plist path"
        launchctl bootout "$DOMAIN" "$plist" 2>/dev/null || true
      fi
    else
      log INFO "DRY RUN: launchctl bootout $DOMAIN/$label"
    fi
  else
    log DEBUG "LaunchAgent $label is not loaded"
  fi
}
# Find only processes whose command line contains an exact installed script path.
find_pids() {
  local target="$1"
  /usr/bin/pgrep -f -- "$target" 2>/dev/null | while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    printf '%s\n' "$pid"
  done
}
stop_matching_processes() {
  local target="$1" label="$2" pids pid
  pids="$(find_pids "$target" || true)"
  if [[ -z "$pids" ]]; then
    log DEBUG "No running process found for $label"
    return
  fi
  log INFO "Stopping $label process(es): $(printf '%s' "$pids" | tr '\n' ' ')"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log INFO "DRY RUN: would send TERM then KILL if needed"
    return
  fi
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    kill -TERM "$pid" 2>/dev/null || true
  done <<< "$pids"

  for ((i=0; i<WAIT_SECONDS; i++)); do
    local remaining=0
    while IFS= read -r pid; do
      [[ "$pid" =~ ^[0-9]+$ ]] || continue
      if kill -0 "$pid" 2>/dev/null; then remaining=1; fi
    done <<< "$(find_pids "$target" || true)"
    [[ "$remaining" -eq 0 ]] && break
    sleep 1
  done

  pids="$(find_pids "$target" || true)"
  if [[ -n "$pids" ]]; then
    log WARN "$label did not exit after TERM; sending KILL to remaining PID(s): $(printf '%s' "$pids" | tr '\n' ' ')"
    while IFS= read -r pid; do
      [[ "$pid" =~ ^[0-9]+$ ]] || continue
      kill -KILL "$pid" 2>/dev/null || true
    done <<< "$pids"
  fi
}

echo "Uninstalling Photo Offloader$([[ "$VERBOSE" -eq 1 ]] && echo ' (verbose mode)' || true)..."
[[ "$DRY_RUN" -eq 0 ]] || echo "DRY RUN ONLY — no changes will be made."
log INFO "Install directory: $INSTALL_DIR"
log INFO "LaunchAgent domain: $DOMAIN"

# Pause the trigger first, then unload the known jobs (including the import worker).
if [[ -d "$INSTALL_DIR" ]]; then
  if [[ "$DRY_RUN" -eq 0 ]]; then
    : > "$INSTALL_DIR/.paused"
    log DEBUG "Created pause marker"
  else
    log INFO "DRY RUN: would create $INSTALL_DIR/.paused"
  fi
fi
bootout_agent "$LABEL" "$AGENTS_DIR/$LABEL.plist"
bootout_agent "$UI_LABEL" "$AGENTS_DIR/$UI_LABEL.plist"
bootout_agent "$IMPORT_LABEL" "$AGENTS_DIR/$IMPORT_LABEL.plist"

# Stop any installed-script processes that were launched outside launchd.
stop_matching_processes "$INSTALL_DIR/camera_offloader_trigger.sh" "camera trigger"
stop_matching_processes "$INSTALL_DIR/photo_offloader_import_supervisor.sh" "import supervisor"
stop_matching_processes "$INSTALL_DIR/camera_offloader_v2.py" "Python importer"
stop_matching_processes "$INSTALL_DIR/photo_offloader_ui.py" "dashboard"
stop_matching_processes "$INSTALL_DIR/photo_offloader_menubar.py" "menu-bar app"

# Confirm the registered jobs are gone before removing their plist files.
for label in "$LABEL" "$UI_LABEL" "$IMPORT_LABEL"; do
  if exists_in_launchd "$label"; then
    log WARN "LaunchAgent still appears registered: $label"
  else
    log DEBUG "Verified unloaded: $label"
  fi
done

if [[ "$DRY_RUN" -eq 0 ]]; then
  log INFO "Removing LaunchAgent plist files"
  rm -f "$AGENTS_DIR/$LABEL.plist" "$AGENTS_DIR/$UI_LABEL.plist" "$AGENTS_DIR/$IMPORT_LABEL.plist"
  if [[ -d "$INSTALL_DIR" ]]; then
    log INFO "Removing installed application directory"
    rm -rf "$INSTALL_DIR"
  fi
  log INFO "Removing application log files"
  rm -f "$HOME/.camera_transfer.log" "$HOME/.camera_transfer_trigger.log"
else
  log INFO "DRY RUN: would remove known Photo Offloader plists, $INSTALL_DIR, and application logs"
fi

if [[ "$DRY_RUN" -eq 0 ]]; then
  remaining=0
  for label in "$LABEL" "$UI_LABEL" "$IMPORT_LABEL"; do
    if exists_in_launchd "$label"; then
      log WARN "Cleanup incomplete: $label is still registered"
      remaining=1
    fi
  done
  if [[ "$remaining" -ne 0 ]]; then
    echo "WARNING: Some launchd jobs remain. Review the verbose output before reinstalling." >&2
    exit 1
  fi
  echo "Application scripts, known LaunchAgents, state, and logs removed."
else
  echo "Dry run complete; nothing was changed."
fi
echo "Persistent media catalog preserved: $DATA_DIR/catalog.sqlite3"
echo "Imported media under ~/Pictures/CameraImports and ~/Pictures/CameraImportsRaw was left untouched."
