#!/bin/bash
set -euo pipefail

INSTALL_DIR="$HOME/Library/Application Support/PhotoOffloader"
PLIST_PATH="$HOME/Library/LaunchAgents/com.user.photooffloader.plist"
LABEL="com.user.photooffloader"

echo "Uninstalling Photo Offloader..."

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST_PATH"
rm -rf "$INSTALL_DIR"
rm -f "$HOME/.camera_transfer.log" "$HOME/.camera_transfer_trigger.log"

echo "Application files, trigger, LaunchAgent, and logs removed."
echo "Imported media under ~/Pictures/CameraImports was left untouched."
