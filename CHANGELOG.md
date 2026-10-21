# Changelog

All notable changes to Photo Offloader are documented here.

## [Unreleased]

### Changed
- Camera import output now opens in iTerm2 by default, with `PHOTO_OFFLOADER_TERMINAL_APP` available to override the terminal application.
- Imported media is now organized as `YYYY-MM/Camera-Model/Media-Type/filename`, using `Photos`, `Raws`, and `Movies` folders; camera identity comes from EXIF make/model when available, with `Unknown-Camera` as a fallback.
- Video camera identity now uses embedded Make/Model metadata through optional `exiftool` support when available, so compatible MOV/MP4 files are grouped with the correct camera.

### Fixed
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
