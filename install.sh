#!/bin/bash

# Stop immediately if any step fails.
set -e

SCRIPT_NAME="camera_offloader_v2.py"
INSTALL_DIR="$HOME/Library/Application Support/PhotoOffloader"
VENV_DIR="$INSTALL_DIR/venv"
SCRIPT_PATH="$INSTALL_DIR/$SCRIPT_NAME"
PLIST_PATH="$HOME/Library/LaunchAgents/com.user.photooffloader.plist"

echo "==========================================="
echo " Installing Photo Offloader Automation"
echo "==========================================="

echo "➡️ Creating application directory..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

echo "➡️ Copying active import script..."
if [ -f "$SCRIPT_NAME" ]; then
    cp "$SCRIPT_NAME" "$SCRIPT_PATH"
    chmod +x "$SCRIPT_PATH"
else
    echo "❌ Error: $SCRIPT_NAME not found in the current folder."
    exit 1
fi

echo "➡️ Setting up isolated Python environment..."
python3 -m venv "$VENV_DIR"

echo "➡️ Installing dependencies (alive-progress)..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet alive-progress

echo "➡️ Configuring macOS automation service..."
cat <<EOF > "$PLIST_PATH"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://apple.com">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.photooffloader</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/osascript</string>
        <string>-e</string>
        <string>tell application "Terminal" to do script "'$VENV_DIR/bin/python3' '$SCRIPT_PATH'; exit"</string>
    </array>
    <key>WatchPaths</key>
    <array>
        <string>/Volumes</string>
    </array>
</dict>
</plist>
EOF

echo "➡️ Registering background service with macOS..."
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "==========================================="
echo " 🎉 Installation complete successfully!"
echo "==========================================="
echo "The camera importer will launch whenever a card is inserted."
echo "Log file: ~/.camera_transfer.log"
