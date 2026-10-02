#!/bin/bash

# Stop immediately if any step fails.
set -e

INSTALL_DIR="$HOME/Library/Application Support/PhotoOffloader"
VENV_DIR="$INSTALL_DIR/venv"
PLIST_PATH="$HOME/Library/LaunchAgents/com.user.photooffloader.plist"
LOG_FILE_PATH="$HOME/.camera_transfer.log"

printf '===========================================\n'
printf ' Uninstalling Photo Offloader Automation\n'
printf '===========================================\n\n'

if [ -f "$PLIST_PATH" ]; then
    echo "➡️ Unloading LaunchAgent..."
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
fi

if [ -d "$VENV_DIR" ] || [ -d "$INSTALL_DIR" ]; then
    echo "➡️ Removing installed application files..."
    rm -rf "$INSTALL_DIR"
fi

if [ -f "$LOG_FILE_PATH" ]; then
    echo "➡️ Removing application log..."
    rm -f "$LOG_FILE_PATH"
fi

printf '\n===========================================\n'
printf ' ✅ Uninstall complete.\n'
printf '===========================================\n'
printf 'The imported media in ~/Pictures/CameraImports was left in place.\n'
