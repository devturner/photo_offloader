# 📸 Photo Offloader Automation Suite

A macOS-focused camera media offloader that safely copies photos and video from camera cards to local storage and, when available, an external backup drive.

The importer follows:

> **Discover → Lock → Snapshot → Scan → Capacity Check → Copy → Verify → Report → Eject**

Files are never deleted from the camera card and existing destination files are never overwritten.

## ✨ Features

### Camera card detection

Configured camera volume patterns currently include:

- `NIKON*`
- `SONY*`
- `EOS*`

The automatic LaunchAgent watches `/Volumes`, but the trigger only launches when one of those camera volumes contains a `DCIM` directory. A volume such as `/Volumes/Photos` is ignored as a source.

The importer also accepts a volume or `DCIM` path through `--source`.

### Safe handling of camera names with spaces

Volume names are passed as real command arguments rather than being reconstructed through unsafe nested shell quoting. Names such as:

```text
/Volumes/NIKON Z 8
```

are supported.

### Duplicate-trigger protection

macOS can generate multiple `/Volumes` events while removable media is mounting. The trigger therefore uses several layers of protection:

1. LaunchAgent `ThrottleInterval` reduces event bursts.
2. An atomic trigger lock prevents simultaneous trigger evaluations.
3. Trigger state prevents the same mounted camera from launching repeatedly.
4. The importer has its own process lock as the final concurrency safeguard.

The state is cleared when no recognized camera volume is mounted, allowing the same card to be processed again after it is unmounted and remounted.

### 💾 Local + external backup

Local storage defaults to:

```text
~/Pictures/CameraImports/
```

When `/Volumes/Photos` is mounted when an import begins, a second copy is written to:

```text
/Volumes/Photos/CameraImports/
```

The USB destination is snapshotted at the beginning of the session. If the expected USB volume disappears or is replaced during the import, the session fails and the camera card is not automatically ejected.

Use `--no-usb` when a second backup is intentionally not required.

### 🛡️ Collision safety

Existing files are never blindly overwritten.

For an existing same-name file:

1. Size is compared.
2. Contents are SHA-256 compared.
3. If it matches, the file is skipped.
4. If it differs, a conflict name such as `DSC_1234__conflict-1.NEF` is used.

### 🔐 Copy verification

Every copied file is verified after the copy completes.

By default, verification compares file sizes. For stronger verification of every new copy:

```bash
python3 camera_offloader_v2.py --sha256
```

Existing same-name files are always content-checked before being skipped.

### Source stability

The importer fingerprints each source file before copying and checks it again afterward. If the source changes or disappears during the copy, that destination is treated as failed.

### Free-space protection

Before copying, the importer calculates the total source bytes and checks every required destination for enough free space plus a safety buffer.

The default safety buffer is 100 MiB and can be changed with `MIN_FREE_SPACE_BUFFER_BYTES` in the Python configuration.

### 📁 Archive layout

Files are organized into `YYYY-MM` folders while preserving native camera subfolders:

```text
CameraImports/
└── YYYY-MM/
    ├── 100NIKON/
    │   ├── DSC_0001.NEF
    │   └── DSC_0002.JPG
    └── 101NIKON/
        └── DSC_0003.JPG
```

EXIF capture date is preferred for the month folder, followed by filesystem birth time and modification time.

### 🎥 Video camera metadata

For video files such as MOV and MP4, the importer can read embedded camera Make/Model metadata using the optional `exiftool` command. When available, this allows videos to be grouped with photos from the same camera.

If `exiftool` is not installed or a video does not contain usable camera metadata, the importer falls back to `Unknown-Camera`.

`exiftool` is not installed automatically by Photo Offloader. On a Homebrew-based Mac, install it with:

```bash
brew install exiftool
```

You can verify that it is available with:

```bash
exiftool -ver
```

### 📊 Session reporting

The importer reports:

- files discovered
- source bytes
- destination copies
- destination skips
- verified copies
- failures
- destination paths
- eject status

Persistent logs are written to:

```text
~/.camera_transfer.log
```

Trigger activity is written to:

```text
~/.camera_transfer_trigger.log
```

### 🔔 macOS notifications

The importer uses native `osascript` notifications for successful, incomplete, canceled, and eject-failure sessions. Notification failure does not itself make an import fail.

### ⏏️ Safe ejection

The card is automatically ejected only when:

- all required destinations succeeded;
- the expected USB backup volume is still present and matches its recorded identity; and
- `--no-eject` was not specified.

If an import fails, the card remains mounted for investigation or retry.

## 📋 Requirements

- macOS
- Python 3.9+
- `diskutil` and `osascript` (provided by macOS)
- `alive-progress`
- `Pillow`
- Optional: `exiftool` for camera Make/Model metadata in video files

Development/testing also uses Ruff.

## 🚀 Installation

From the repository directory:

```bash
chmod +x install.sh uninstall.sh
./install.sh
```

The installer:

1. installs the importer and trigger under `~/Library/Application Support/PhotoOffloader`;
2. creates the runtime virtual environment;
3. installs runtime dependencies;
4. creates and validates `com.user.photooffloader.plist`;
5. loads the LaunchAgent with `launchctl bootstrap`; and
6. verifies that the LaunchAgent is active.

## 🧪 Testing

Install development dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Run the complete test suite:

```bash
python -m unittest discover -v
```

Run Ruff:

```bash
python -m ruff check camera_offloader_v2.py test_camera_offloader.py test_hardening_regressions.py test_camera_offloader_trigger.py
```

The test suite includes filesystem-level copy tests, hardening regressions, lock behavior, USB identity checks, video camera metadata detection, and trigger tests covering volume names with spaces and repeated `/Volumes` events.

## ⚙️ Manual operation

Import a specific camera volume:

```bash
python3 camera_offloader_v2.py --source "/Volumes/NIKON Z 8"
```

Import without the USB destination:

```bash
python3 camera_offloader_v2.py --source "/Volumes/NIKON Z 8" --no-usb
```

Do not eject after a successful import:

```bash
python3 camera_offloader_v2.py --source "/Volumes/NIKON Z 8" --no-eject
```

Enable SHA-256 verification:

```bash
python3 camera_offloader_v2.py --source "/Volumes/NIKON Z 8" --sha256
```

## 🔧 Configuration

The main configuration constants are at the top of `camera_offloader_v2.py`:

- `CARD_VOLUME_GLOBS`
- `LOCAL_DEST_ROOT`
- `USB_DEST_VOLUME`
- `USB_DEST_ROOT`
- `VERIFY_WITH_SHA256`
- `PRESERVE_CAMERA_SUBFOLDERS`
- `USE_FILE_BIRTH_TIME`
- `MIN_FREE_SPACE_BUFFER_BYTES`
- `EJECT_CARD_AFTER_SUCCESS`

The installed copy lives at:

```text
~/Library/Application Support/PhotoOffloader/camera_offloader_v2.py
```

### Trigger configuration

The LaunchAgent is:

```text
~/Library/LaunchAgents/com.user.photooffloader.plist
```

It watches `/Volumes`. Do not remove the trigger-level and importer-level locks; both are intentional safety layers.

## 🧹 Uninstallation

```bash
./uninstall.sh
```

This removes the application files, runtime environment, trigger state, LaunchAgent, and logs. Imported media under `~/Pictures/CameraImports` is intentionally left untouched.

## 📝 Change tracking

See [`CHANGELOG.md`](CHANGELOG.md) for the project history. The current hardening release is tracked as **2.3.0**.

## ⚠️ Operational guidance

This application is designed to protect against accidental overwrites and incomplete backups, but it should still be treated as backup infrastructure rather than the only copy of irreplaceable media.

For a first run after installation or an upgrade, verify the resulting files on both destinations before formatting or reusing the camera card.
