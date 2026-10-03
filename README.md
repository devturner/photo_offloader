# 📸 Photo Offloader Automation Suite

A macOS-focused camera media offloader that safely copies photos and video from camera cards to local storage and, when available, an external backup drive.

The importer follows:

> **Discover → Lock → Snapshot → Scan → Capacity Check → Copy → Verify → Report → Eject**

Files are never deleted from the camera card and existing destination files are never overwritten.

## What's new in v2.2

v2.2 is a reliability and safety hardening release.

- Automatic volume events are filtered through a camera-aware trigger.
- Only one import session can run at a time.
- Existing same-name files are SHA-256 checked before being skipped.
- Required destination space is checked before copying.
- The configured USB volume is identified at session start and checked again before ejection.
- Source files are checked for changes during copying.
- LaunchAgent installation validates the generated plist.
- Runtime and development dependencies are separated.
- The distribution bundle is updated for v2.
- Exit codes are documented and changes are tracked in `CHANGELOG.md`.

## Features

### Camera card detection

Configured camera volume patterns currently include:

- `NIKON*`
- `SONY*`
- `EOS*`

The importer also accepts a volume or `DCIM` path through `--source`.

### Local + external backup

Local storage defaults to:

```text
~/Pictures/CameraImports/
```

When `/Volumes/Photos` is mounted, a second copy is written to:

```text
/Volumes/Photos/CameraImports/
```

The USB destination is snapshotted at the beginning of the session. If it disappears or is replaced during the import, the session fails and the card is not automatically ejected.

Use `--no-usb` when a second backup is intentionally not required.

### Collision safety

Existing files are never overwritten.

For an existing same-name file:

1. Its size is compared.
2. Its contents are SHA-256 compared.
3. If it matches, the file is skipped.
4. If it differs, a conflict name such as `DSC_1234__conflict-1.NEF` is used.

### Verification

New copies are verified by size by default.

Use:

```bash
python3 camera_offloader_v2.py --sha256
```

to use SHA-256 for every copy verification.

Existing same-name files are always content-checked regardless of the `--sha256` option.

### Source stability

The source file's size, modification timestamp, and inode are captured before copying and checked again afterward. If the source changes during the copy, that destination is treated as failed.

### Free-space protection

Before copying, the importer calculates the total source bytes and checks every required destination for enough free space plus a safety buffer.

The default safety buffer is 100 MiB and can be changed with `MIN_FREE_SPACE_BUFFER_BYTES`.

### Single-instance protection

A process lock prevents two importer sessions from operating on the same workstation simultaneously.

## Archive layout

```text
CameraImports/
└── YYYY-MM/
    ├── 100NIKON/
    │   ├── DSC_0001.NEF
    │   └── DSC_0002.JPG
    └── 101NIKON/
        └── DSC_0003.NEF
```

EXIF capture date is preferred for the month folder, followed by filesystem birth time and modification time.

## Supported media

- JPEG / JPG
- HEIC / HEIF
- NEF
- ARW
- CR2
- CR3
- DNG
- RAF
- ORF
- RW2
- MOV
- MP4
- AVI
- MTS
- M2TS

## Requirements

- macOS
- Python 3.9+
- `diskutil`
- `osascript`
- `alive-progress`
- `Pillow`

Runtime dependencies are in `requirements.txt`.

Development dependencies are in `requirements-dev.txt`.

## Installation

```bash
chmod +x install.sh uninstall.sh running_bash.sh camera_offloader_trigger.sh
./install.sh
```

The installer creates:

```text
~/Library/Application Support/PhotoOffloader/
```

with an isolated Python environment and installs a LaunchAgent.

The LaunchAgent watches `/Volumes`, but it does **not** launch the importer for every volume event. The trigger script first checks for a configured camera DCIM directory and passes the detected source explicitly to the importer.

## Manual use

```bash
python3 camera_offloader_v2.py --source /Volumes/NIKON/DCIM
```

or:

```bash
./running_bash.sh --source /Volumes/NIKON/DCIM
```

If `--source` is omitted, the importer prompts for a recognized camera card.

## Command-line options

```text
--source PATH    Camera volume or DCIM path
--no-usb         Local destination only
--no-eject       Leave camera card mounted
--sha256         SHA-256 verification for all copies
--verbose        Verbose logging
```

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Import completed successfully |
| 1 | Import failed or was incomplete |
| 2 | Import succeeded but automatic eject failed |
| 130 | Import canceled with Ctrl-C |

## Safety model

The importer:

- does not delete source media
- never overwrites destination files
- uses temporary files before final installation
- verifies completed copies
- detects source mutation during copy
- validates destination capacity
- requires the originally detected USB backup volume to remain present
- prevents concurrent import sessions
- only ejects after all required destinations succeed

A failed import leaves the camera card mounted.

## Logs

Main importer log:

```text
~/.camera_transfer.log
```

Automatic trigger log:

```text
~/.camera_transfer_trigger.log
```

## Testing

Create a development environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Run tests:

```bash
python -m unittest discover -v
```

Run Ruff:

```bash
python -m ruff check camera_offloader_v2.py test_camera_offloader.py camera_offloader_trigger.sh
```

The shell script is not Python, so a typical Ruff command should target the Python files only:

```bash
python -m ruff check camera_offloader_v2.py test_camera_offloader.py
```

## Distribution bundle

```bash
./bundle_script.sh
```

This produces:

```text
photo_offloader_v2.2.zip
```

## Uninstallation

```bash
./uninstall.sh
```

The uninstaller removes the LaunchAgent, installed application files, and logs. It does **not** delete imported media under `~/Pictures/CameraImports`.

## Configuration

The main configuration is at the top of `camera_offloader_v2.py`.

Important values include:

```python
CARD_VOLUME_GLOBS
LOCAL_DEST_ROOT
USB_DEST_VOLUME
USB_DEST_ROOT
WAIT_SECONDS
EJECT_CARD_AFTER_SUCCESS
VERIFY_WITH_SHA256
PRESERVE_CAMERA_SUBFOLDERS
USE_FILE_BIRTH_TIME
MIN_FREE_SPACE_BUFFER_BYTES
```

## Changelog

See `CHANGELOG.md` for release and reliability-hardening history.

## License

MIT License.
