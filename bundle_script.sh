#!/bin/bash

# 1. Create a clean staging directory
mkdir -p ./photo_offloader_dist

# 2. Copy files into the staging directory using your exact folder structure
cp photo_offloader.py ./photo_offloader_dist/
cp install.sh ./photo_offloader_dist/
cp README.md ./photo_offloader_dist/
cp running_bash.sh ./photo_offloader_dist/

# 3. Ensure the bash scripts retain their executable permissions
chmod +x ./photo_offloader_dist/install.sh
chmod +x ./photo_offloader_dist/running_bash.sh

# 4. Compress the folder into a shareable ZIP archive
zip -r photo_offloader_v1.0.zip ./photo_offloader_dist

# 5. Clean up the staging directory
rm -rf ./photo_offloader_dist

echo "==========================================="
echo "📦 Archive Created: photo_offloader_v1.0.zip"
echo "==========================================="
