# Changelog

All notable changes to Photo Offloader are documented here.

## [Unreleased]

### Planned
- Additional macOS hardware integration tests.
- Review and tuning of camera-volume trigger behavior on multiple macOS versions.

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

### Exit Codes
- `0` — import completed successfully.
- `1` — import failed or was incomplete.
- `2` — import completed successfully but automatic ejection failed.
- `130` — import canceled with `Ctrl-C`.
