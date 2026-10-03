#!/bin/bash
set -u

echo "==========================================="
echo " Running Camera Offloader v2.2 Manually"
echo "==========================================="

INSTALL_DIR="$HOME/Library/Application Support/PhotoOffloader"

if [[ -x "$INSTALL_DIR/venv/bin/python3" && -f "$INSTALL_DIR/camera_offloader_v2.py" ]]; then
    "$INSTALL_DIR/venv/bin/python3" "$INSTALL_DIR/camera_offloader_v2.py" "$@"
    status=$?
else
    echo "Error: Camera Offloader is not installed. Run ./install.sh first."
    status=1
fi

if [[ -t 0 ]]; then
    echo ""
    read -r -p "Press Enter to exit..."
fi

exit "$status"
