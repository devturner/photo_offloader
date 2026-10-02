#!/bin/bash

echo "==========================================="
echo "🚀 Running Camera Offloader v2 Manually"
echo "==========================================="

INSTALL_DIR="$HOME/Library/Application Support/PhotoOffloader"

if [ -f "$INSTALL_DIR/venv/bin/python3" ]; then
    # Executes cleanly inside your permanent isolated application virtual environment context
    "$INSTALL_DIR/venv/bin/python3" "$INSTALL_DIR/camera_offloader_v2.py" "$@"
else
    echo "❌ Error: Camera Offloader is not installed on this machine yet."
    echo "   Please run ./install.sh first!"
fi

echo ""
read -p "Press Enter to exit..."
