#!/bin/bash
set -euo pipefail

SCRIPT_NAME="camera_offloader_v2.py"
TRIGGER_NAME="camera_offloader_trigger.sh"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/Library/Application Support/PhotoOffloader"
VENV_DIR="$INSTALL_DIR/venv"
SCRIPT_PATH="$INSTALL_DIR/$SCRIPT_NAME"
TRIGGER_PATH="$INSTALL_DIR/$TRIGGER_NAME"
PLIST_PATH="$HOME/Library/LaunchAgents/com.user.photooffloader.plist"
LABEL="com.user.photooffloader"

echo "==========================================="
echo " Installing Photo Offloader v2.3"
echo "==========================================="

mkdir -p "$INSTALL_DIR" "$HOME/Library/LaunchAgents"

for file in "$SCRIPT_NAME" "$TRIGGER_NAME" "requirements.txt"; do
  if [[ ! -f "$SCRIPT_DIR/$file" ]]; then
    echo "Error: $file not found in $SCRIPT_DIR."
    exit 1
  fi
done

cp "$SCRIPT_DIR/$SCRIPT_NAME" "$SCRIPT_PATH"
cp "$SCRIPT_DIR/$TRIGGER_NAME" "$TRIGGER_PATH"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
chmod +x "$SCRIPT_PATH" "$TRIGGER_PATH"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python3" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python3" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"

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

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "LaunchAgent loaded successfully."
else
  echo "Error: LaunchAgent did not load successfully."
  exit 1
fi

echo "Installation complete."
echo "Importer: $SCRIPT_PATH"
echo "Trigger:   $TRIGGER_PATH"
echo "Log:       ~/.camera_transfer.log"
echo "Trigger log: ~/.camera_transfer_trigger.log"
