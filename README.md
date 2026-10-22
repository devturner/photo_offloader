# 📸 Photo Offloader Automation Suite

A macOS-focused camera media offloader that safely copies photos and video from camera cards to local storage and, when available, an external backup drive.

The importer follows:

> **Discover → Lock → Snapshot → Scan → Capacity Check → Copy → Verify → Report → Eject**

Files are never deleted from the camera card and existing destination files are never overwritten. A local SQLite catalog tracks known file locations, filesystem fingerprints, cached camera/date metadata, and verified hashes. It avoids repeatedly decoding unchanged RAW/video metadata and re-hashing unchanged local/USB file pairs. The filesystem is still checked for newly added or changed files; the catalog is an optimization, not the source of truth.

## Build provenance in logs

Every importer session writes a `Run provenance` entry to `~/.camera_transfer.log` with a unique run ID, app version, full Git commit, branch, tag, dirty-checkout flag, build timestamp, Python version/executable, script path, and command-line arguments. The installer writes `build_info.json` next to the installed scripts so logs still identify the source revision after the code has been copied out of the repository. Direct source-checkout runs fall back to Git metadata. The volume trigger logs version/commit/branch to `~/.camera_transfer_trigger.log`, and the menu-bar app and dashboard log startup provenance to their respective stdout logs.

The installed `build_info.json` records the revision and checkout state at install time. Re-run `./install.sh` after updating the checkout to refresh this metadata; `git_dirty=true` means the installation source contained uncommitted changes.

## ✨ Features

### Camera card detection

Configured camera volume patterns currently include:

- `NIKON*`
- `SONY*`
- `EOS*`

The automatic LaunchAgent watches `/Volumes`, but the trigger only launches when one of those camera volumes contains a `DCIM` directory. A volume such as `/Volumes/Photos` is ignored as a source.

The importer also accepts a volume or `DCIM` path through `--source`.

### 🖥️ Menu-bar dashboard and pause control

The installer launches a native macOS menu-bar item and opens the dashboard window at the end of installation. When a camera import starts, the dashboard is opened automatically (or reused if already running). Select **Open Dashboard** from the menu-bar item at any time to view the current source, file being processed, progress, live transfer speed, a rolling speed graph, and estimated time remaining. Use **Copy to USB Drive** in the menu bar or dashboard to run a USB-only sync without a camera card. Both controls use a shared launch lock, so repeated clicks cannot start overlapping USB backup requests. A 15-second debounce also suppresses duplicate requests if a very fast backup finishes before a second click. The importer acquires its session lock before changing dashboard status; duplicate launches are ignored cleanly and do not overwrite the active session's progress.

Use **Pause Automatic Imports** before inserting a card when you do not want automatic copying to start. Pausing prevents new automatic import sessions; it does not interrupt a copy already in progress. Choose **Resume Automatic Imports** to re-enable the service and immediately re-check any camera card that is already mounted.

The menu-bar controller uses `rumps`; the dashboard uses Python's `tkinter` UI. The menu-bar dependency is declared in `requirements-ui.txt` and installed by `install.sh`.

Some Homebrew Python installations package Tk support separately. If the installer reports that `tkinter` is missing, install the matching formula, for example `brew install python-tk@3.14` for Python 3.14, then rerun `./install.sh`.

### iTerm2 output

The automatic camera trigger opens the importer in **iTerm2** by default. To use another terminal application, set `PHOTO_OFFLOADER_TERMINAL_APP` in the LaunchAgent environment before installing/loading the trigger.

For example, to use the macOS Terminal app instead:

```text
PHOTO_OFFLOADER_TERMINAL_APP=Terminal
```

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

Local storage uses two top-level roots:

```text
~/Pictures/CameraImports/       # JPG/HEIC, videos, and other non-RAW media
~/Pictures/CameraImportsRaw/    # RAW media (NEF, ARW, CR2/CR3, DNG, RAF, ORF, RW2)
```

A camera import now runs in two stages:

1. **Card → Mac SSD:** non-RAW media is copied to `~/Pictures/CameraImports/`; RAW media is copied to `~/Pictures/CameraImportsRaw/`. Each file is verified using the existing safe-copy rules.
2. **Mac SSD → USB:** if `/Volumes/Photos` is mounted, Photo Offloader mirrors both roots to `/Volumes/Photos/CameraImports/` and `/Volumes/Photos/CameraImportsRaw/`. Existing files are SHA-256 checked; matching files are skipped, and mismatching files are reported as conflicts rather than overwritten.

Once the local SSD stage has succeeded, the camera card can be ejected while the USB backup continues. A USB backup failure therefore does not invalidate the verified local import; rerun the USB-only sync after reconnecting the drive.

Use `--no-usb` to skip USB backup during a camera import. USB backup can also be run independently at any time with the drive mounted, even when no camera card is connected:

```bash
python3 camera_offloader_v2.py --backup-usb
```

The USB-only mode scans the local archive, verifies existing same-name files by SHA-256, copies missing files, and never overwrites a differing existing file.

### 🛡️ Collision safety

Existing files are never blindly overwritten.

For an existing same-name file:

1. Size is compared.
2. Contents are SHA-256 compared.
3. If it matches, the file is skipped.
4. If it differs, a conflict name such as `DSC_1234__conflict-1.NEF` is used.

### ⚡ Fast repeat imports

Photo Offloader keeps verified SHA-256 hashes in the Mac SQLite catalog and, when the card is writable, an optional hidden sidecar manifest at `.photo-offloader/manifest-v1.json` on the card. On later insertions, the manifest is matched by card-relative path, size, and modification time; the destination hash is reused only when its catalog fingerprint is still valid. When both hashes are available, the decision can be made without reading the large RAW file contents again.

The card manifest is a cache, not a source of truth. It is written atomically, never contains the media itself, and is optional: read-only cards, unsupported filesystems, missing manifests, and stale entries fall back to content hashing. Directory enumeration still runs on insertion so newly added files are discovered. During the decision phase, the dashboard reports files checked, checks per second, and an estimated time remaining.

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

### Migrating existing RAW files

To move RAW files already stored under `CameraImports` into the sibling `CameraImportsRaw` tree, use `./move_raw_to_separate_folder.sh` for a dry run. The script preserves relative paths and never overwrites an existing destination. Review the preview first, then run `./move_raw_to_separate_folder.sh --apply` to perform the move. It does not delete or modify JPGs or other non-RAW files.

### 📁 Archive layout

Files are organized into `YYYY-MM` folders while preserving native camera subfolders:

```text
CameraImports/
└── YYYY-MM/
    ├── 100NIKON/
    │   ├── Photos/
    │   │   └── DSC_0002.JPG
    │   └── Movies/
    │       └── DSC_0004.MOV
    └── 101NIKON/
        └── Photos/
            └── DSC_0003.JPG

CameraImportsRaw/
└── YYYY-MM/
    └── 100NIKON/
        └── Raws/
            └── DSC_0001.NEF
```

EXIF capture date is preferred for the month folder, followed by filesystem birth time and modification time.

### 🎥 Video camera metadata

For video files such as MOV and MP4, the importer can read embedded camera Make/Model metadata using the optional `exiftool` command. When available, this allows videos to be grouped with photos from the same camera.

If `exiftool` is not installed or a video does not contain usable camera metadata, the importer falls back to `Unknown-Camera`.

Because macOS LaunchAgents may start with a restricted `PATH`, the importer also checks common executable locations including `/opt/homebrew/bin/exiftool`, `/usr/local/bin/exiftool`, and `/opt/local/bin/exiftool`.

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

The SQLite file catalog is stored at:

```text
~/Library/Application Support/PhotoOffloaderData/catalog.sqlite3
```

The catalog is updated automatically as files are scanned and copied. Use **Update File Catalog** in the menu bar or dashboard, or run `python3 camera_offloader_v2.py --update-catalog`, to explicitly initialize or refresh it. The dashboard's **Database Status** section shows separate internal-SSD and backup-USB catalog counts, last successful refresh times, and whether a refresh is currently running. A USB drive that is not mounted is clearly marked and its previous catalog records are preserved. This scans the Mac archive and the USB archive if the USB volume is mounted, updates file locations and cached capture-month/camera metadata, and removes stale entries for roots it successfully scans. It does not hash every file; verified hashes are preserved for unchanged files and generated by copy/verification workflows as needed. If the USB volume is not mounted, its existing catalog entries are preserved.

The database is stored separately from the install directory, so it survives normal application upgrades and `./uninstall.sh`. The first run after this update automatically migrates an existing database from the former `~/Library/Application Support/PhotoOffloader/catalog.sqlite3` location. If the database is missing or unreadable, the importer falls back to normal filesystem and metadata checks; it never relies on catalog data to overwrite files.

Trigger activity is written to:

```text
~/.camera_transfer_trigger.log
```

Dashboard startup errors are written to `~/Library/Application Support/PhotoOffloader/dashboard.log`; menu-bar app output is written to `~/Library/Application Support/PhotoOffloader/menubar.log` and errors to `~/Library/Application Support/PhotoOffloader/menubar-error.log`.

### 🔔 macOS notifications

The importer uses native `osascript` notifications for successful, incomplete, canceled, and eject-failure sessions. Notification failure does not itself make an import fail.

### ⏏️ Safe ejection

The card is automatically ejected after the complete local SSD stage succeeds, provided `--no-eject` was not specified. If the local stage fails, the card remains mounted for investigation or retry. The USB stage checks the expected volume identity before copying; USB failures are reported separately and can be retried with `--backup-usb`.

## 📋 Requirements

- macOS
- Python 3.9+
- `diskutil` and `osascript` (provided by macOS)
- `alive-progress`
- `Pillow`
- `rumps` for the native menu-bar controller (installed by the app installer)
- Optional: `exiftool` for camera Make/Model metadata in video files

Development/testing also uses Ruff.

## 🚀 Installation

From the repository directory:

```bash
chmod +x install.sh uninstall.sh
./install.sh
```

The installer:

1. installs the importer, trigger, dashboard, and menu-bar controller under `~/Library/Application Support/PhotoOffloader`;
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

The test suite includes filesystem-level copy tests, catalog caching/invalidation, hardening regressions, lock behavior, USB identity checks, video camera metadata detection, and trigger tests covering volume names with spaces and repeated `/Volumes` events.

## ⚙️ Manual operation

Import a specific camera volume (copy to the Mac SSD, then sync missing files to USB if mounted):

```bash
python3 camera_offloader_v2.py --source "/Volumes/NIKON Z 8"
```

Sync the Mac archive to USB without a camera card:

```bash
python3 camera_offloader_v2.py --backup-usb
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

This removes the application files, runtime environment, trigger state, LaunchAgent, and logs. Imported media under `~/Pictures/CameraImports` and `~/Pictures/CameraImportsRaw` is intentionally left untouched.

## 📝 Change tracking

See [`CHANGELOG.md`](CHANGELOG.md) for the project history. The current hardening release is tracked as **2.3.0**.

## ⚠️ Operational guidance

This application is designed to protect against accidental overwrites and incomplete backups, but it should still be treated as backup infrastructure rather than the only copy of irreplaceable media.

For a first run after installation or an upgrade, verify the resulting files on both destinations before formatting or reusing the camera card.
