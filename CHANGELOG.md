# Changelog

All notable changes to Photo Offloader are documented here.

## [Unreleased]

### Fixed
- Preserve successful handled-card state when the camera DCIM directory is briefly absent, preventing repeated successful imports during mount visibility churn.
- Added a trigger regression test for a successful import followed by a transient missing-DCIM event.


### Fixed
- Prevented registered launchd import workers from causing the trigger to delete handled-card state and relaunch the same failing source on every poll.
- Revalidated camera DCIM paths immediately before submission/launch and skip stale paths that disappeared during mount churn.
- Enforced auto-eject-off at the importer CLI boundary by passing `--no-eject` to automatic imports unless the explicit auto-eject marker is enabled; the supervisor defaults to no-eject when its flag is omitted.
- Added regression coverage for registered-worker retry suppression and for enforcing `--no-eject` on automatic background imports.

### Fixed
- Prevented failed camera imports from being relaunched on every trigger poll. The failed status and handled-card state are preserved until the DCIM folder disappears on unmount, allowing a fresh attempt after remount.
- Added regression tests for failure-loop suppression and retry eligibility after unmount/remount.

### Fixed
- Run terminal-free camera imports as a separately submitted user-domain launchd job instead of relying on a `nohup` child of the short-lived volume trigger. Submission failures are logged and clear the handled-card state for retry.
- Hardened uninstall to unload known LaunchAgents, stop installed-script processes, verify known jobs are unloaded before reporting success, and preserve imported media and the persistent catalog.

### Added
- Added `./uninstall.sh --verbose` diagnostics and a `--dry-run` preview, plus usage help.

### Fixed
- Replaced the embedded `bash -c` background-import supervisor with an installed standalone supervisor script. It records lifecycle phases, atomically persists status, logs unexpected exits, and clears stale mount state on failure.
- Corrected the stale-state regression fixture to write an actual newline, so the test exercises the state-file format produced by the trigger.

### Added
- Added supervisor lifecycle regression coverage through the installed-script path.

### Fixed
- Background imports now persist a source-specific running/success/failure status. The trigger retries a stale background launch when its state exists but no live importer or successful completion is recorded, and auto-eject continues to require an explicit successful import status.

### Added
- Added regression coverage for persisted supervisor completion status and retrying stale handled-state after a background launch disappears.

### Fixed
- Added explicit background-supervisor start, working-directory failure, and importer exit diagnostics to both the trigger log and importer log, making terminal-free launch failures distinguishable from successful imports.

### Added
- Added regression coverage requiring the detached supervisor to record its startup in both logs before importer processing.

### Added
- Added live card-inventory reconciliation against the existing manifest, reporting new, changed, unchanged, and no-longer-present files. Changed-file hashes are invalidated so they must be re-established; inventory observations do not advance successful import timestamps.

### Added
- Extended the existing card-side manifest with last-seen, scan status, discovered/verified/pending counts, and successful scan/import timestamps. Failed or incomplete runs do not advance successful timestamps; manifest write failures remain non-fatal for read-only cards.

### Added
- Added an Auto-Eject On/Off dashboard control; auto-eject is opt-in and defaults off so already-handled cards remain mounted for Finder access unless explicitly enabled.
- Replaced the Light/Dark dropdown with a compact sun/moon appearance toggle with a sliding active-state thumb and mouse/keyboard support. The selected theme is saved under `ui.theme` in `PhotoOffloaderData/preferences.json` and restored on next launch without overwriting other preferences.


### Changed
- Refreshed the dashboard with a light neutral palette, cleaner label backgrounds, styled primary/secondary buttons, and individually collapsible archive/database, automation/cloud, and activity/history sections. Diagnostic detail is hidden until requested.
- Reset copy and Google Photos speed timelines and rolling throughput values when a transfer is no longer active, so the next run starts with a clean chart.

### Fixed
- Dashboard status now recognizes a mounted supported camera card when no transfer is active, rather than always showing “Waiting for camera”. With auto-eject disabled, the trigger no longer requires a success marker because it is not attempting ejection.
- Already-handled camera mounts now honor the Auto-Eject setting; with the default-off setting they remain mounted and repeated polling logs are suppressed. When enabled, the trigger waits until the importer is no longer running, then request card ejection once; ejection success/failure is logged, successful ejections suppress repeated poll messages, and failed ejections are retried. Added trigger regression tests for successful/idempotent and failed/retried ejection.
- Made terminal-free automatic imports run under a detached supervisor, set a predictable launchd-safe PATH for Homebrew media tools, set the importer's working directory, and ensure failed imports clear their handled-card state so they can be retried.
- Added trigger regression coverage for detached background imports and the launchd-safe PATH.

### Changed
- Restored the full dashboard redesign: compact workflow header and state indicator, prominent quick actions, separate transfer panels, a dedicated current-activity/progress surface, side-by-side archive/database and automation/cloud health cards, and a separate recent-log history panel.

### Fixed
- Fixed a syntax error in catalog ETA tracking that prevented the dashboard from launching.

### Added
- Copy and Google Photos cards now show rolling 60-second average and peak throughput alongside current speed, elapsed time, ETA, and the live speed graph.

### Changed
- Redesigned the Copy and Google Photos dashboard cards with independent progress percentages, elapsed timers, and separate rolling throughput graphs positioned beside each speed metric. Copy card title reflects import-to-HD versus USB backup activity.

### Added
- Dashboard now shows Google Photos upload percentage, current transfer speed, and estimated time remaining beside the existing copy-speed monitor. ETA labels sit in the upper-right corner of each speed card.
- Google Photos logs now include an explicit application-level success message after rclone exits successfully and per-file catalog updates finish.
- A non-secret Google Photos connection preference is stored outside the install directory, allowing the dashboard to remember a configured rclone remote across upgrades and reinstallations.

### Changed
- The Google Photos upload card now shows a distinct finalization state when rclone transfer stats reach 100% but album uploads are still being committed.
- Background card imports now run under a small supervisor; a nonzero importer exit clears the mount's handled-state and logs the failure so the card can be retried.

### Added
- Optional rclone-based Google Photos uploader for JPG/JPEG photos and supported videos, with automatic post-import uploads, a dashboard manual-sync button, separate upload status/logs, and per-file upload records in the SQLite catalog.
- RAW media is stored under a dedicated `CameraImportsRaw` top-level archive on the Mac; JPGs and other non-RAW media remain under `CameraImports`.
- Added a preview-first Bash migration script to move existing RAW files into the separate archive without overwriting conflicts.

### Changed
- Installer deploys the Google Photos sync helper; cloud upload failures remain independent of the verified local/USB import result.
- USB backup reads both local archive roots and keeps the USB archive consolidated under `CameraImports`; catalog refresh covers both local roots and the single USB root.
- Automatic imports run in the background by default; a dashboard toggle enables Terminal output for future imports.
- Moved the rolling transfer-speed graph into the Copy Speed card to consolidate the graph and numeric speed display.


### Fixed
- Automatic card detection now runs at launch and polls every 30 seconds as a fallback when macOS misses a removable-volume WatchPaths event.
- Catalog updates now show phase-aware remaining-time estimates when scan progress supports one, and a clear metadata-reading label during ExifTool batches.
- Uninstall now stops the separately launched Tkinter dashboard before removing application files, so its window does not remain open after uninstall.
- Close SQLite catalog connections after each lookup, record, and refresh transaction to prevent file-descriptor exhaustion during large catalog scans.

### Changed
- Catalog refresh now logs ExifTool batch start/completion heartbeats with batch counts and elapsed time, so long metadata phases no longer appear silent for minutes.
- Repeat card imports now reuse fingerprint-validated hashes from an optional card-side manifest and the Mac SQLite catalog, avoiding full RAW-file reads when both source and destination hashes are already known.
- Copy-decision progress is reported separately from transfer progress, including files checked, checks per second, and an estimated time remaining.

### Added
- Dashboard automatic-import status area shows the latest trigger activity and how recently it ran.
- Toggleable live debug window tails the trigger, importer, dashboard, and menu-bar error logs.
- Optional atomic card-side manifest at `.photo-offloader/manifest-v1.json`; it stores only relative media paths, size/mtime fingerprints, and verified SHA-256 hashes. If the card is read-only or the manifest is unavailable, imports continue using the Mac catalog and safe fallback verification.
- Verified SHA-256 hashes are recorded for successfully copied files and existing matching destination files, making subsequent repeat imports faster.

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
- Camera import output can be opened in iTerm2 or another configured terminal when Terminal output is enabled in the dashboard; background mode is the default.
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
