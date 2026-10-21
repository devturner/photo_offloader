# Changelog

All notable changes to Photo Offloader are documented here.

## [Unreleased]

### Added
- Per-run provenance logging with application version, full Git commit, branch, tag, dirty-source state, build timestamp, Python runtime, script path, and run ID; installer persists build metadata for installed copies.
- Native macOS menu-bar controller with a dashboard for live import speed, progress, and estimated completion time.
- Menu-bar pause/resume control that blocks new automatic imports without interrupting an active copy; resuming re-checks already-mounted camera cards.
- Atomic JSON progress status updates emitted during chunked file copies.
- Windows-style rolling transfer-speed graph in the dashboard, with recent throughput samples.
- Independent **Copy to USB Drive** actions in the menu bar and dashboard; USB sync can run without a camera card.
- SQLite file-location catalog at `~/Library/Application Support/PhotoOffloaderData/catalog.sqlite3`, tracking known paths, file fingerprints, cached capture month/camera metadata, and verified SHA-256 hashes.
- **Update File Catalog** action in the menu bar and dashboard plus `--update-catalog` CLI mode to initialize/refresh local and mounted USB inventories, prune stale paths for scanned roots, and preserve cached hashes without rehashing every file.
- Dashboard **Database Status** panel with separate internal-SSD and backup-USB inventory counts, refresh-in-progress indicators, and per-volume last-successful-update timestamps.
- Faster repeat catalog refreshes: unchanged files with valid cached metadata now require only a fingerprint check and catalog lookup, avoiding redundant metadata lookups and SQLite writes.
- Batch ExifTool reads for uncached files during catalog refresh, with the existing per-file/Pillow fallback retained for missing or failed batch metadata.
- Added `benchmark_catalog.py` to compare cold-build and repeat-refresh times using a temporary catalog without modifying production database, log, or status files.
- Stabilized dashboard catalog progress across roots and media folders: the progress bar is indeterminate during catalog scans, the overall file count no longer resets at drive boundaries, and metadata-discovery/batch-reading stages are shown explicitly.
- Fixed **Quit Menu Bar App** so it unloads the launchd agent to prevent automatic relaunch and closes dashboard windows started by this installation. Active photo transfers and catalog updates are intentionally left running.
- Clearer catalog progress text identifies the active drive and per-drive scan count, and removes the duplicate progress message.
- One-time migration from the legacy catalog path; the database now lives outside the uninstallable application directory and survives upgrades/uninstall.
- Cache reuse for unchanged media to avoid repeatedly invoking ExifTool/Pillow metadata decoding and hashing unchanged local/USB file pairs.
- Shared lock for USB backup requests from the menu bar and dashboard; repeated requests are ignored while a backup launch is active.
- Acquire the importer session lock before initializing progress state, so duplicate launches do not overwrite the active dashboard status; lock collisions exit cleanly without a traceback.
- Dashboard opens at the end of installation and automatically when a camera import starts; menu-bar and dashboard logs are retained for troubleshooting.

### Changed
- Camera imports now complete and verify the Mac SSD stage before starting a separate Mac-to-USB backup stage.
- The camera card can be ejected after the verified local SSD stage while the USB backup continues.
- USB backup sync copies missing files only, SHA-256 checks existing files, and reports conflicting files without overwriting them.
- Camera import output now opens in iTerm2 by default, with `PHOTO_OFFLOADER_TERMINAL_APP` available to override the terminal application.
- Imported media is now organized as `YYYY-MM/Camera-Model/Media-Type/filename`, using `Photos`, `Raws`, and `Movies` folders; camera identity comes from EXIF make/model when available, with `Unknown-Camera` as a fallback.
- Video camera identity now uses embedded Make/Model metadata through optional `exiftool` support when available, so compatible MOV/MP4 files are grouped with the correct camera.

### Fixed
- Increased the dashboard’s default window size and minimum dimensions so the bottom action buttons remain visible when opened from the menu bar.
- Nikon and other supported camera RAW files now use ExifTool for capture date and camera make/model when Pillow cannot decode the RAW format.
- Video metadata detection now checks standard Homebrew/MacPorts install paths when launchd's restricted PATH cannot locate `exiftool`.
- ExifTool executable discovery is cached for the importer session to avoid repeating the same lookup/debug message for every RAW file.
- Imported media no longer inherits executable or owner-only source mode bits such as 0700; new destination files are normalized to user read/write (0600).
- Trigger regression test now matches the shell-escaped path format produced by bash `%q`.
- CI regression test now matches the list-form argument vector used by `subprocess.run` for `exiftool`.

### Planned
- Additional macOS hardware integration tests with real removable media.
- Validation across supported macOS and Python versions.

## [2.3.0] - 2026-10-03

### Added
- Trigger-level duplicate suppression for repeated `/Volumes` events.
- Trigger state tracking so one mounted camera is only launched once until it is unmounted.
- Atomic trigger lock to suppress simultaneous launchd invocations.
- Safe regression coverage for camera volume names containing spaces, including `NIKON Z 8`.
- Importer source-file fingerprinting before and after every copy.
- Destination free-space checks with a configurable safety buffer.
- USB destination identity validation using `diskutil` metadata.
- Explicit source-path logging and deterministic import accounting.

### Changed
- Camera trigger path handling no longer performs unsafe shell/AppleScript interpolation.
- Terminal launch commands use shell-escaped arguments so spaces and shell metacharacters in volume names are preserved.
- LaunchAgent now throttles repeated `/Volumes` events for two seconds in addition to trigger/import locks.
- Installer verifies that the LaunchAgent successfully loaded.
- Uninstaller removes trigger state and lock artifacts.
- Importer is now v2.3 and treats a USB destination present at session start as required for the entire session.

### Fixed
- Repeated `/Volumes` events no longer open multiple importer Terminal windows for the same mounted camera.
- `/Volumes/Photos` mounting before the camera no longer causes an importer launch.
- Camera paths containing spaces are passed to the importer intact.
- Same-size, different-content destination files are treated as conflicts rather than duplicates.
- Source files that disappear or change during copying fail safely rather than producing misleading destination results.
- Failed imports do not automatically eject the source card.

### Exit Codes
- `0` — import completed successfully.
- `1` — import failed or was incomplete.
- `2` — import completed successfully but automatic ejection failed.
- `130` — import canceled with `Ctrl-C`.

## [2.2.0] - 2026-10-02

### Added
- Single-instance import locking.
- Camera-aware LaunchAgent trigger script.
- Destination free-space validation with a configurable safety buffer.
- USB volume identity tracking using macOS `diskutil` metadata.
- Source-file stability checking during copy.
- Content-based SHA-256 comparison for existing same-name destination files.
- Runtime/development dependency separation.
- Expanded CI-ready test coverage.
- Changelog tracking.

### Changed
- Destination selection is snapshotted once per import session.
- Existing destination files are only treated as duplicates after content verification.
- Automatic backup failure now includes USB disconnect/replacement detection.
- LaunchAgent installation validates the plist and uses modern `launchctl bootstrap`/`bootout`.
- Bundle generation now targets the v2 application files.
- Manual runner no longer blocks non-interactive invocations.

### Fixed
- Removed stale v1 references from the distribution bundle script.
- Prevented unrelated `/Volumes` activity from directly launching the Python importer.
- Prevented concurrent importer sessions from running simultaneously.
- Preserved the existing safety rule that failed imports do not automatically eject the card.
