#!/bin/bash
set -euo pipefail

INSTALL_DIR="$HOME/Library/Application Support/PhotoOffloader"
PLIST_PATH="$HOME/Library/LaunchAgents/com.user.photooffloader.plist"
UI_PLIST_PATH="$HOME/Library/LaunchAgents/com.user.photooffloader.menubar.plist"
LABEL="com.user.photooffloader"
UI_LABEL="com.user.photooffloader.menubar"

echo "Uninstalling Photo Offloader..."

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$UI_LABEL" 2>/dev/null || true

# The dashboard is started separately by install.sh (nohup), not by launchd.
# Unload the agents first, then close only dashboard processes from this install.
DASHBOARD_PATH="$INSTALL_DIR/photo_offloader_ui.py"
DASHBOARD_PIDS="$(pgrep -f -- "$DASHBOARD_PATH" 2>/dev/null || true)"
if [[ -n "$DASHBOARD_PIDS" ]]; then
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    [[ "$pid" == "$" || "$pid" == "$PPID" ]] && continue
    kill -TERM "$pid" 2>/dev/null || true
  done <<< "$DASHBOARD_PIDS"

  # Give Tkinter time to close cleanly before removing its application files.
  for _ in {1..5}; do
    sleep 1
    still_running=0
    while IFS= read -r pid; do
      [[ "$pid" =~ ^[0-9]+$ ]] || continue
      [[ "$pid" == "$" || "$pid" == "$PPID" ]] && continue
      if kill -0 "$pid" 2>/dev/null; then
        still_running=1
      fi
    done <<< "$(pgrep -f -- "$DASHBOARD_PATH" 2>/dev/null || true)"
    [[ "$still_running" -eq 0 ]] && break
  done

  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    [[ "$pid" == "$" || "$pid" == "$PPID" ]] && continue
    kill -KILL "$pid" 2>/dev/null || true
  done <<< "$(pgrep -f -- "$DASHBOARD_PATH" 2>/dev/null || true)"
fi

rm -f "$PLIST_PATH" "$UI_PLIST_PATH"
rm -rf "$INSTALL_DIR"
rm -f "$HOME/.camera_transfer.log" "$HOME/.camera_transfer_trigger.log"

echo "Application files, trigger state, LaunchAgent, and logs removed."
echo "Persistent media catalog retained at: $HOME/Library/Application Support/PhotoOffloaderData/catalog.sqlite3"
echo "Imported media under ~/Pictures/CameraImports was left untouched."
