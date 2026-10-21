#!/bin/bash
set -euo pipefail

SCRIPT_NAME="camera_offloader_v2.py"
TRIGGER_NAME="camera_offloader_trigger.sh"
UI_NAME="photo_offloader_ui.py"
MENUBAR_NAME="photo_offloader_menubar.py"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/Library/Application Support/PhotoOffloader"
VENV_DIR="$INSTALL_DIR/venv"
SCRIPT_PATH="$INSTALL_DIR/$SCRIPT_NAME"
TRIGGER_PATH="$INSTALL_DIR/$TRIGGER_NAME"
PLIST_PATH="$HOME/Library/LaunchAgents/com.user.photooffloader.plist"
UI_PLIST_PATH="$HOME/Library/LaunchAgents/com.user.photooffloader.menubar.plist"
LABEL="com.user.photooffloader"
UI_LABEL="com.user.photooffloader.menubar"

echo "==========================================="
echo " Installing Photo Offloader v2.4"
echo "==========================================="

mkdir -p "$INSTALL_DIR" "$HOME/Library/LaunchAgents"

for file in "$SCRIPT_NAME" "$TRIGGER_NAME" "$UI_NAME" "$MENUBAR_NAME" "requirements.txt" "requirements-ui.txt"; do
  if [[ ! -f "$SCRIPT_DIR/$file" ]]; then
    echo "Error: $file not found in $SCRIPT_DIR."
    exit 1
  fi
done

cp "$SCRIPT_DIR/$SCRIPT_NAME" "$SCRIPT_PATH"
cp "$SCRIPT_DIR/$TRIGGER_NAME" "$TRIGGER_PATH"
cp "$SCRIPT_DIR/$UI_NAME" "$INSTALL_DIR/$UI_NAME"
cp "$SCRIPT_DIR/$MENUBAR_NAME" "$INSTALL_DIR/$MENUBAR_NAME"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
cp "$SCRIPT_DIR/requirements-ui.txt" "$INSTALL_DIR/requirements-ui.txt"
chmod +x "$SCRIPT_PATH" "$TRIGGER_PATH" "$INSTALL_DIR/$UI_NAME" "$INSTALL_DIR/$MENUBAR_NAME"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python3" -m pip install --quiet --upgrade pip
if ! "$VENV_DIR/bin/python3" -c "import tkinter" >/dev/null 2>&1; then
  PYTHON_MINOR="$("$VENV_DIR/bin/python3" -c 'import sys; print("%s.%s" % sys.version_info[:2])')"
  echo "Error: tkinter is missing from this Python installation."
  echo "For Homebrew Python, try: brew install python-tk@$PYTHON_MINOR"
  exit 1
fi
"$VENV_DIR/bin/python3" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"
"$VENV_DIR/bin/python3" -m pip install --quiet -r "$INSTALL_DIR/requirements-ui.txt"

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
        <string>$INSTALL_DIR/$MENUBAR_NAME</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
EOF
plutil -lint "$UI_PLIST_PATH"

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

echo "Installation complete."
echo "Importer: $SCRIPT_PATH"
echo "Trigger:   $TRIGGER_PATH"
echo "Dashboard: $INSTALL_DIR/$UI_NAME"
echo "Menu bar:  $INSTALL_DIR/$MENUBAR_NAME"
echo "Log:       ~/.camera_transfer.log"
echo "Trigger log: ~/.camera_transfer_trigger.log"
