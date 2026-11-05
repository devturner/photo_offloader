#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/Library/Application Support/PhotoOffloader"
VENV_DIR="$INSTALL_DIR/venv"
# The installed app mirrors the repository layout (see photo_offloader/layout.py).
APP_DIRS=(bin scripts photo_offloader)
APP_FILES=(VERSION requirements.txt requirements-ui.txt)
SCRIPT_PATH="$INSTALL_DIR/bin/photo-offloader-import"
TRIGGER_PATH="$INSTALL_DIR/scripts/trigger.sh"
MENUBAR_PATH="$INSTALL_DIR/bin/photo-offloader-menubar"
DASHBOARD_PATH="$INSTALL_DIR/bin/photo-offloader-dashboard"
# Flat layout used before v2.5; removed on upgrade.
LEGACY_FILES=(camera_offloader_v2.py camera_offloader_trigger.sh photo_offloader_import_supervisor.sh
  photo_offloader_service.sh dashboard_core.py dashboard_server.py menubar_model.py photo_offloader_ui.py
  photo_offloader_menubar.py photo_offloader_backup_launcher.py google_photos_sync.py)
PLIST_PATH="$HOME/Library/LaunchAgents/com.user.photooffloader.plist"
UI_PLIST_PATH="$HOME/Library/LaunchAgents/com.user.photooffloader.menubar.plist"
LABEL="com.user.photooffloader"
UI_LABEL="com.user.photooffloader.menubar"

echo "==========================================="
echo " Installing Photo Offloader v$(cat "$SCRIPT_DIR/VERSION")"
echo "==========================================="

# Stop and verify any worker submitted by an older trigger before replacing
# installed scripts. Fail closed: never overwrite code still used by a live worker.
IMPORT_LABEL="com.user.photooffloader.import"
DOMAIN="gui/$(id -u)"
SHUTDOWN_WAIT_SECONDS="${PHOTO_OFFLOADER_SHUTDOWN_WAIT_SECONDS:-10}"
if ! [[ "$SHUTDOWN_WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "Error: PHOTO_OFFLOADER_SHUTDOWN_WAIT_SECONDS must be a non-negative integer." >&2
  exit 2
fi

import_process_running() {
  local target
  for target in \
    "$INSTALL_DIR/scripts/import_supervisor.sh" \
    "$SCRIPT_PATH" \
    "$INSTALL_DIR/photo_offloader_import_supervisor.sh" \
    "$INSTALL_DIR/camera_offloader_v2.py"; do
    if /usr/bin/pgrep -f -- "$target" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

if launchctl print "$DOMAIN/$IMPORT_LABEL" >/dev/null 2>&1; then
  echo "Stopping existing import worker before upgrading..."
  launchctl kill SIGTERM "$DOMAIN/$IMPORT_LABEL" 2>/dev/null || true
  # Unregister the submitted job as well as signaling its process.
  launchctl bootout "$DOMAIN/$IMPORT_LABEL" 2>/dev/null || true
fi

shutdown_verified=0
for ((attempt=0; attempt<=SHUTDOWN_WAIT_SECONDS; attempt++)); do
  job_running=0
  launchctl print "$DOMAIN/$IMPORT_LABEL" >/dev/null 2>&1 && job_running=1
  process_running=0
  import_process_running && process_running=1
  if [[ "$job_running" -eq 0 && "$process_running" -eq 0 ]]; then
    shutdown_verified=1
    break
  fi
  [[ "$attempt" -lt "$SHUTDOWN_WAIT_SECONDS" ]] && sleep 1
done

if [[ "$shutdown_verified" -ne 1 ]]; then
  echo "Error: existing import worker shutdown could not be verified after ${SHUTDOWN_WAIT_SECONDS}s." >&2
  echo "Installed files were not replaced. Close the worker or run ./uninstall.sh --verbose, then retry." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR" "$HOME/Library/LaunchAgents"

for item in "${APP_DIRS[@]}" "${APP_FILES[@]}"; do
  if [[ ! -e "$SCRIPT_DIR/$item" ]]; then
    echo "Error: $item not found in $SCRIPT_DIR."
    exit 1
  fi
done

# A dashboard left running from the previous version would keep serving old code.
for dashboard in "$DASHBOARD_PATH" "$INSTALL_DIR/photo_offloader_ui.py"; do
  /usr/bin/pkill -f -- "$dashboard" 2>/dev/null || true
done

for dir in "${APP_DIRS[@]}"; do
  rm -rf "${INSTALL_DIR:?}/$dir"
  cp -R "$SCRIPT_DIR/$dir" "$INSTALL_DIR/$dir"
done
find "$INSTALL_DIR/photo_offloader" -name "__pycache__" -type d -prune -exec rm -rf {} +
for file in "${APP_FILES[@]}"; do
  cp "$SCRIPT_DIR/$file" "$INSTALL_DIR/$file"
done
for legacy in "${LEGACY_FILES[@]}"; do
  rm -f "$INSTALL_DIR/$legacy"
done
rm -rf "$INSTALL_DIR/dashboard_web" "$INSTALL_DIR/__pycache__"
chmod +x "$INSTALL_DIR"/bin/* "$INSTALL_DIR"/scripts/*.sh

# Capture provenance at install time so installed logs identify the exact source
# revision, even though the installed Application Support directory is not a git checkout.
BUILD_VERSION="$(cat "$INSTALL_DIR/VERSION")"
BUILD_COMMIT="$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || printf 'unknown')"
BUILD_BRANCH="$(git -C "$SCRIPT_DIR" branch --show-current 2>/dev/null || printf 'unknown')"
BUILD_TAG="$(git -C "$SCRIPT_DIR" describe --tags --always 2>/dev/null || printf 'unknown')"
if git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [[ -n "$(git -C "$SCRIPT_DIR" status --porcelain 2>/dev/null)" ]]; then
    BUILD_DIRTY="true"
  else
    BUILD_DIRTY="false"
  fi
else
  BUILD_DIRTY="unknown"
fi
BUILD_TIME_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
export BUILD_VERSION BUILD_COMMIT BUILD_BRANCH BUILD_TAG BUILD_DIRTY BUILD_TIME_UTC SCRIPT_PATH
python3 - <<'PYBUILD'
import json
import os
from pathlib import Path

info = {
    "version": os.environ.get("BUILD_VERSION", "unknown"),
    "git_commit": os.environ.get("BUILD_COMMIT", "unknown"),
    "git_branch": os.environ.get("BUILD_BRANCH", "unknown"),
    "git_tag": os.environ.get("BUILD_TAG", "unknown"),
    "git_dirty": os.environ.get("BUILD_DIRTY", "unknown"),
    "build_time_utc": os.environ.get("BUILD_TIME_UTC", "unknown"),
    "source_path": os.environ.get("SCRIPT_PATH", "unknown"),
}
target = Path.home() / "Library" / "Application Support" / "PhotoOffloader" / "build_info.json"
target.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
print("Build provenance: " + " ".join(f"{key}={value}" for key, value in info.items()))
PYBUILD

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python3" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python3" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"
"$VENV_DIR/bin/python3" -m pip install --quiet -r "$INSTALL_DIR/requirements-ui.txt"
# Optional: a native dashboard window. Without it the dashboard opens in your browser.
if ! "$VENV_DIR/bin/python3" -m pip install --quiet "pywebview>=5.0" >/dev/null 2>&1; then
  echo "Note: pywebview could not be installed; the dashboard will open in your web browser instead."
fi

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$TRIGGER_PATH</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <!-- Poll as a fallback because removable-volume WatchPaths events can be missed. -->
    <key>StartInterval</key>
    <integer>30</integer>
    <key>WatchPaths</key>
    <array>
        <string>/Volumes</string>
    </array>
    <key>ThrottleInterval</key>
    <integer>2</integer>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST_PATH"

cat > "$UI_PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$UI_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_DIR/bin/python3</string>
        <string>$MENUBAR_PATH</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/menubar.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/menubar-error.log</string>
</dict>
</plist>
EOF
plutil -lint "$UI_PLIST_PATH"

# Logs captured by launchd/nohup have no rotation of their own; keep them bounded.
for captured_log in "$INSTALL_DIR/dashboard.log" "$INSTALL_DIR/menubar.log" "$INSTALL_DIR/menubar-error.log"; do
  if [[ -f "$captured_log" && "$(wc -c < "$captured_log" | tr -d ' ')" -gt 1048576 ]]; then
    mv -f "$captured_log" "$captured_log.1"
  fi
done

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$UI_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl bootstrap "gui/$(id -u)" "$UI_PLIST_PATH"

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && launchctl print "gui/$(id -u)/$UI_LABEL" >/dev/null 2>&1; then
  echo "Importer and menu-bar LaunchAgents loaded successfully."
else
  echo "Error: one or more Photo Offloader LaunchAgents did not load successfully."
  exit 1
fi

# Explicitly kick the menu-bar agent after loading and open the dashboard so
# successful installation gives immediate visible feedback.
launchctl kickstart -k "gui/$(id -u)/$UI_LABEL"
nohup "$VENV_DIR/bin/python3" "$DASHBOARD_PATH" >> "$INSTALL_DIR/dashboard.log" 2>&1 </dev/null &
echo "Opened the Photo Offloader dashboard."

echo "Installation complete."
echo "Importer: $SCRIPT_PATH"
echo "Trigger:   $TRIGGER_PATH"
echo "Dashboard: $DASHBOARD_PATH"
echo "Menu bar:  $MENUBAR_PATH"
echo "Menu bar log: $INSTALL_DIR/menubar.log"
echo "Menu bar errors: $INSTALL_DIR/menubar-error.log"
echo "Dashboard log: $INSTALL_DIR/dashboard.log"
echo "Log:       ~/.camera_transfer.log"
echo "Trigger log: ~/.camera_transfer_trigger.log"
echo "Stop / start the service: \"$INSTALL_DIR/scripts/service.sh\" stop | start | status"
