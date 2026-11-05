> **HTML documentation:** The GitHub Pages version is available at [Photo Offloader Documentation](https://devturner.github.io/photo_offloader/). The hosted page is maintained in `index.html` on `main`.

# 📸 Photo Offloader Automation Suite

A macOS-focused camera media offloader that safely copies photos and video from camera cards to local storage and, when available, an external backup drive.

The importer follows:

> **Discover → Lock → Snapshot → Scan → Capacity Check → Copy → Verify → Report → Eject**

Files are never deleted from the camera card and existing destination files are never overwritten. A local SQLite catalog tracks known file locations, filesystem fingerprints, cached camera/date metadata, verified hashes, and per-file Google Photos upload records tied to the exact file fingerprint. It avoids repeatedly decoding unchanged RAW/video metadata and re-hashing unchanged local/USB file pairs. The filesystem is still checked for newly added or changed files; the catalog is an optimization, not the source of truth.

## Build provenance in logs

Every importer session writes a `Run provenance` entry to `~/.camera_transfer.log` with a unique run ID, app version, full Git commit, branch, tag, dirty-checkout flag, build timestamp, Python version/executable, script path, and command-line arguments. The installer writes `build_info.json` next to the installed scripts so logs still identify the source revision after the code has been copied out of the repository. Direct source-checkout runs fall back to Git metadata. The volume trigger logs version/commit/branch to `~/.camera_transfer_trigger.log`, and the menu-bar app and dashboard log startup provenance to their respective stdout logs.

The installed `build_info.json` records the revision and checkout state at install time. Re-run `./install.sh` after updating the checkout to refresh this metadata; `git_dirty=true` means the installation source contained uncommitted changes.

## ✨ Features

### Camera card detection

Any mounted volume with a top-level `DCIM` directory is treated as a camera card, whatever its name or manufacturer. A volume such as `/Volumes/Photos` is ignored as a source.

Cards are tracked by their **volume UUID**, not just their mount path. Every card from the same camera body mounts under the same name (`NIKON Z 8`, `EOS_DIGITAL`), so a second card inserted after the first one finished is recognised as a new card and imported. Re-inserting the *same* card after it has been out for more than 20 seconds (for example to shoot more) starts a new import that copies only the new files; shorter gaps are treated as mount churn and ignored (`PHOTO_OFFLOADER_REMOUNT_GRACE_SECONDS` changes the window). If a mounted card was already imported, the dashboard says so and offers **Import now**.

The importer also accepts a volume or `DCIM` path through `--source`.

### Everything on the card is copied

Besides `DCIM`, the importer scans `PRIVATE` and `MP_ROOT`, where Sony, Canon and Panasonic cameras store video (`PRIVATE/M4ROOT/CLIP`, `PRIVATE/AVCHD`, ...). Known photo, RAW and video formats go to `Photos`, `Raws` and `Movies`. **Any other file** (clip sidecar XML, thumbnails, an unfamiliar RAW format) is copied to an `Other` folder instead of being ignored, and the log lists the unrecognised extensions, so nothing is silently left on a card that you then format. Hidden files (including macOS `._*` files) are skipped. A file that differs from an earlier copy with the same name is kept alongside it as `name__conflict-N`, except for index files the camera rewrites on every shoot (`NC_FLLST.DAT`, `MEDIAPRO.XML`), where the newest version replaces the old one on the Mac and on USB.

### 🖥️ Menu-bar dashboard and pause control

The installer starts a menu-bar item and opens the dashboard. When a camera import starts, the dashboard is opened automatically (or brought forward if it is already open). Choose **Open Dashboard** from the menu-bar item at any time.

The dashboard is a small local web app, served only on `127.0.0.1`, shown in a native window when `pywebview` is installed (the installer tries to install it) and in your default browser otherwise. It follows macOS light/dark mode unless you choose **Light** or **Dark** under *Appearance* in the **⋯** menu (the choice is remembered), and shows:

- **A headline** describing what matters right now: *Ready*, the step an import is on (*Scanning card*, *Reading photo info*, *Checking for new files*, *Copying new files*, *Backing up to USB*, *Updating file index*), *Imported from NIKON Z 8*, *Automatic imports are paused*, *Photo Offloader is stopped*, or what needs attention.
- **Four cards**: Camera card, Mac archive, USB backup and Google Photos, each with its state and a one-line detail. Contextual actions such as **Back up now** or **Sync now** appear on a card only when they apply.
- **Live progress** while something is running: the import's steps, a progress bar, and fixed slots for progress, speed, files (checked of total, and how many are new), elapsed and remaining time, plus a one-minute speed graph. The panel keeps one size in every state, and a value that is not known yet shows a dash instead of disappearing. Google Photos uploads use the same panel when the importer is idle.
- **Recent activity**: the last imports (what that session copied, by type and size, then the card total), USB backups and catalog updates, stored in the persistent catalog so they survive restarts and reinstalls (the latest 500 runs are kept).

Everything else is hidden behind the **⋯** menu: pause/resume automatic imports, update the file catalog, back up to USB, sync Google Photos, *Eject card after import*, *Show Terminal during imports*, *Appearance*, *View logs* (import, card detection or Google Photos log, opened in Console so it updates live), and Stop/Start. Switches and *Appearance* leave the menu open so you can see the change. The menu bar also has **View Import Log**. The dashboard window has a fixed size; Recent activity scrolls inside it (the latest 100 runs). The result of the last transfer fades after 10 minutes; a failed transfer stays until you dismiss it with **×**. **Clear** in Recent activity deletes the stored history after a confirming second click. If an import or upload process dies mid-transfer, the dashboard reports it as *interrupted* instead of showing "copying" forever.

Use **Copy to USB Drive** in the menu bar or dashboard to run a USB-only sync without a camera card. Both controls use a shared launch lock, so repeated clicks cannot start overlapping USB backup requests. A 15-second debounce also suppresses duplicate requests if a very fast backup finishes before a second click. The importer acquires its session lock before changing dashboard status; duplicate launches are ignored cleanly and do not overwrite the active session's progress.

**Security.** Because the dashboard can start backups and stop the service, its local server accepts only requests that carry a random per-session token (passed in the URL fragment, so it is never sent to a server or logged), a `Host` header naming the server (DNS-rebinding protection), a same-origin `Origin` for writes, and `application/json` bodies. It never grants CORS access, serves a single fixed page with a strict Content-Security-Policy, and exits on its own a few minutes after its browser tab is closed.

Use **Pause Automatic Imports** before inserting a card when you do not want automatic copying to start. Pausing prevents new automatic import sessions; it does not interrupt a copy already in progress. Choose **Resume Automatic Imports** to re-enable the service and immediately re-check any camera card that is already mounted.

### Stopping and starting the service

Photo Offloader runs as two LaunchAgents: the **trigger** (watches for cards) and the **menu-bar app**. The menu bar stays alive whether or not the service is running. **Stop Photo Offloader…** (menu bar or dashboard **⋯** menu) stops the trigger, after which the menu bar shows *stopped* with a **Start Photo Offloader** item and the dashboard shows a **Start** button. **Quit Menu Bar (Service Keeps Running)** only closes the menu-bar app. From the command line:

```bash
~/Library/Application\ Support/PhotoOffloader/scripts/service.sh status
~/Library/Application\ Support/PhotoOffloader/scripts/service.sh stop            # refuses while a transfer is running
~/Library/Application\ Support/PhotoOffloader/scripts/service.sh stop --wait     # finish the current transfer, then stop
~/Library/Application\ Support/PhotoOffloader/scripts/service.sh stop --force    # cancel the transfer and stop now
~/Library/Application\ Support/PhotoOffloader/scripts/service.sh stop --disable  # also stay off at next login
~/Library/Application\ Support/PhotoOffloader/scripts/service.sh start
```

`stop` unloads the trigger first so no new import can begin, closes open dashboards, and unloads the menu-bar app last. Add `--keep-ui` to stop only the trigger and leave the menu bar and dashboards open (this is what their Stop buttons use). A transfer is never interrupted unless you pass `--force`; the importer treats the stop request like Ctrl-C, so the dashboard shows "canceled" rather than a stuck "copying" state. Without `--disable`, the service comes back at your next login.

The menu-bar controller uses `rumps` (declared in `requirements-ui.txt` and installed by `install.sh`). The dashboard needs nothing beyond the standard library; `pywebview` is an optional extra that gives it a native window instead of a browser tab. Set `PHOTO_OFFLOADER_DASHBOARD_MODE=browser` to force the browser even when `pywebview` is installed.

### ☁️ Google Photos upload via rclone
- Optional one-way upload of JPG/JPEG photos and supported video formats to a dedicated Google Photos album named `Photo Offloader`. Successful uploads are recorded per file in the SQLite catalog with timestamp, remote/album, and the uploaded file fingerprint. Changed files no longer show as uploaded until synced again.
- Upload starts automatically after a successful camera import and can be retried manually with **Sync now** on the Google Photos card (or **Sync Google Photos now** in the **⋯** menu). Live upload percentage, transfer speed, and ETA appear in the dashboard while rclone is running. When transfers reach 100% but rclone is still committing uploads, the card shows “Finishing upload”; the sync log records a clear success message after rclone exits successfully and per-file catalog updates finish.
- RAW files are excluded by extension and remain in `~/Pictures/CameraImportsRaw`; the USB archive remains consolidated at `/Volumes/Photos/CameraImports`.
- Requires rclone and a Google Photos remote named `gphotos`; see setup steps below. The OAuth token stays in rclone's own config. After a successful remote/album connection check, Photo Offloader saves only the remote name and last-check timestamp in `~/Library/Application Support/PhotoOffloaderData/preferences.json`, outside the replaceable install directory, so the dashboard can remember the setup after reinstalling. No credentials or tokens are copied into this preferences file. Cloud failures never invalidate a completed local/USB import.

### iTerm2 output

The automatic camera trigger opens the importer in **iTerm2** by default. Disabling Terminal output runs the same importer in the background and writes output to `~/.camera_transfer.log`; if that background importer exits unsuccessfully, trigger state is cleared so a later volume poll can retry the card. To use another terminal application, set `PHOTO_OFFLOADER_TERMINAL_APP` in the LaunchAgent environment before installing/loading the trigger.

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

Failed or incomplete state is cleared as soon as no camera volume is mounted. After a successful import the state survives brief mount gaps; if the same card comes back after more than `PHOTO_OFFLOADER_REMOUNT_GRACE_SECONDS` (default 20) it is treated as a new session.

### 💾 Local + external backup

Local storage uses two top-level roots:

```text
~/Pictures/CameraImports/       # JPG/HEIC, videos, and other non-RAW media
~/Pictures/CameraImportsRaw/    # RAW media (NEF, ARW, CR2/CR3, DNG, RAF, ORF, RW2)
```

A camera import now runs in two stages:

1. **Card → Mac SSD:** non-RAW media is copied to `~/Pictures/CameraImports/`; RAW media is copied to `~/Pictures/CameraImportsRaw/`. Each file is verified using the existing safe-copy rules.
2. **Mac SSD → USB:** if `/Volumes/Photos` is mounted, Photo Offloader backs up files from both local roots into the single `/Volumes/Photos/CameraImports/` tree, preserving the existing date/camera and `Photos`/`Raws`/`Movies` subfolders. Existing files are SHA-256 checked; matching files are skipped, and mismatching files are reported as conflicts rather than overwritten.

Once the local SSD stage has succeeded, the camera card can be ejected while the USB backup continues. A USB backup failure therefore does not invalidate the verified local import; rerun the USB-only sync after reconnecting the drive.

Use `--no-usb` to skip USB backup during a camera import. USB backup can also be run independently at any time with the drive mounted, even when no camera card is connected:

```bash
bin/photo-offloader-import --backup-usb
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

Every copied file is verified after the copy completes. The SHA-256 of the bytes read from the card is computed **while copying** (the card is read once), then the copy is re-read from the destination, bypassing the macOS file cache where supported, and compared. The copy is only installed under its final name if the hashes match. The same single-read verification is used for the Mac SSD to USB backup.

Existing same-name files are always content-checked before being skipped. The `--sha256` flag is kept for compatibility and only affects the remaining size-based comparisons.

### Source stability

The importer fingerprints each source file before copying and checks it again afterward. If the source changes or disappears during the copy, that destination is treated as failed.

### Free-space protection

Before copying, the importer calculates the total source bytes and checks every required destination for enough free space plus a safety buffer.

The default safety buffer is 100 MiB and can be changed with `MIN_FREE_SPACE_BUFFER_BYTES` in the Python configuration.

### Migrating existing RAW files

To move RAW files already stored under `CameraImports` into the sibling `CameraImportsRaw` tree, use `bash move_raw_to_separate_folder.sh` for a dry run. The script preserves relative paths and never overwrites an existing destination. Review the preview first, then run `bash move_raw_to_separate_folder.sh --apply` to perform the move. It does not delete or modify JPGs or other non-RAW files.

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

All logs are size-capped. The importer log rotates at 5 MiB (keeping three older files, `.1` to `.3`); the trigger log at 1 MiB (two older files); the Google Photos rclone log at 5 MiB (two older files); and the dashboard and menu-bar logs are trimmed when you run `./install.sh`. The trigger runs every 30 seconds, so it only writes a log entry when something changed since its previous run; an idle Mac with no card produces no log output.

The SQLite file catalog is stored at:

```text
~/Library/Application Support/PhotoOffloaderData/catalog.sqlite3
```

The catalog is updated automatically as files are scanned and copied. Use **Update file catalog** in the dashboard's **⋯** menu, or run `bin/photo-offloader-import --update-catalog`, to explicitly initialize or refresh it. The dashboard's Mac archive and USB backup cards show the indexed file counts, and the headline shows when a refresh is running. A USB drive that is not mounted is clearly marked and its previous catalog records are preserved. This scans the Mac archive and the USB archive if the USB volume is mounted, updates file locations and cached capture-month/camera metadata, and removes stale entries for roots it successfully scans. It does not hash every file; verified hashes are preserved for unchanged files and generated by copy/verification workflows as needed. If the USB volume is not mounted, its existing catalog entries are preserved.

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
python -m ruff check .
```

The test suite includes filesystem-level copy tests, catalog caching/invalidation, hardening regressions, lock behavior, USB identity checks, video camera metadata detection, and trigger tests covering volume names with spaces and repeated `/Volumes` events.

## 🗂️ Repository layout

```text
bin/              entry points (photo-offloader-import, -dashboard, -menubar, -google-sync, -usb-backup)
photo_offloader/  the Python package: importer, Google Photos sync, USB backup launcher,
                  dashboard/ (core, server, launcher, web/) and menubar/ (model, app)
scripts/          trigger.sh (launchd), import_supervisor.sh, service.sh (start/stop/status)
tools/            benchmark and one-off migration scripts
tests/            unittest suite: python -m unittest discover
install.sh        installs the same layout into ~/Library/Application Support/PhotoOffloader
```

Run the entry points from a checkout the same way they run installed, e.g. `bin/photo-offloader-import --help`.

## ⚙️ Manual operation

Import a specific camera volume (copy to the Mac SSD, then sync missing files to USB if mounted):

```bash
bin/photo-offloader-import --source "/Volumes/NIKON Z 8"
```

Sync the Mac archive to USB without a camera card:

```bash
bin/photo-offloader-import --backup-usb
```

Import without the USB destination:

```bash
bin/photo-offloader-import --source "/Volumes/NIKON Z 8" --no-usb
```

Do not eject after a successful import:

```bash
bin/photo-offloader-import --source "/Volumes/NIKON Z 8" --no-eject
```

Enable SHA-256 verification:

```bash
bin/photo-offloader-import --source "/Volumes/NIKON Z 8" --sha256
```

## ☁️ Google Photos setup (optional)
1. Install rclone (for example, `brew install rclone`).
2. Follow [rclone’s Google Photos setup guide](https://rclone.org/googlephotos/) and create a remote named `gphotos`. In 2026, configure your own Google OAuth client ID/secret; the shared rclone client ID is being retired. Enable the Photos Library API as described by rclone.
3. Run `rclone config` and complete browser authentication. Photo Offloader does not store Google credentials itself; rclone stores its token in its own config.
4. Use **Sync now** on the dashboard's Google Photos card to test the upload. The first run creates/uses the `Photo Offloader` album. After each successful camera import, the app launches the same upload in the background.

Uploads are original-quality and count against your Google account storage. This is a one-way upload only: it does not delete items from Google Photos or import your existing library, and RAW extensions are excluded. The Google Photos API has limitations; rclone can only download items it uploaded, and its existence checks are not a full content-hash comparison. See [rclone’s documented limitations](https://rclone.org/googlephotos/).

Set `PHOTO_OFFLOADER_GPHOTOS_REMOTE` to change the remote name or `PHOTO_OFFLOADER_GPHOTOS_ALBUM` to change the album name. Defaults are `gphotos` and `Photo Offloader`.

## 🔧 Configuration

The main configuration constants are at the top of `photo_offloader/importer.py`:

- `CARD_VOLUME_GLOBS`
- `LOCAL_DEST_ROOT`
- `USB_DEST_VOLUME`
- `USB_DEST_ROOT`
- `VERIFY_WITH_SHA256`
- `USE_FILE_BIRTH_TIME`
- `MIN_FREE_SPACE_BUFFER_BYTES`
- `EJECT_CARD_AFTER_SUCCESS`

The installed copy lives at:

```text
~/Library/Application Support/PhotoOffloader/photo_offloader/importer.py
```

### Trigger configuration

The LaunchAgent is:

```text
~/Library/LaunchAgents/com.user.photooffloader.plist
```

It watches `/Volumes`. Do not remove the trigger-level and importer-level locks; both are intentional safety layers.

## 🧹 Uninstallation

Preview the cleanup first, then run uninstall with detailed output:

```bash
./uninstall.sh --dry-run --verbose
./uninstall.sh --verbose
```

Use `./uninstall.sh --help` for options. Verbose mode reports which LaunchAgents are unloaded, which installed processes are stopped, and what is removed. The script pauses new automatic imports before cleanup, checks the known trigger, menu-bar, and import-worker jobs, and returns a nonzero status if a known job remains registered. It removes the application files, runtime environment, trigger state, known LaunchAgent plists, and application logs. Imported media under `~/Pictures/CameraImports` and `~/Pictures/CameraImportsRaw` and the persistent catalog under `~/Library/Application Support/PhotoOffloaderData` are intentionally preserved.

## 📝 Change tracking

See [`CHANGELOG.md`](CHANGELOG.md) for the project history. The current hardening release is tracked as **2.3.0**.

## ⚠️ Operational guidance

This application is designed to protect against accidental overwrites and incomplete backups, but it should still be treated as backup infrastructure rather than the only copy of irreplaceable media.

For a first run after installation or an upgrade, verify the resulting files on both destinations before formatting or reusing the camera card.
