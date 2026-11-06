# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A macOS tool that imports camera cards to the Mac (SHA-256 verified), mirrors them to a USB
drive and uploads JPG/video to Google Photos via rclone. A local web dashboard and a menu-bar
app sit on top. Open work and its reasons are in `scratch/todo.md` (gitignored); read it
first when asked to "continue".

## Commands
- Tests: `.venv/bin/python -m unittest discover` (about 375 tests, about 80 s). Use the
  project `.venv`, not the system `python3`, which has no Pillow.
- One module or test: `.venv/bin/python -m unittest tests.test_usb_backup`, or add
  `.TestBackupLauncher.test_request_runs_importer_when_lock_is_free`. Filter by name with `-k`.
- Browser tests: `.venv/bin/python -m unittest tests.browser.test_dashboard_page` (about 20 s). They
  are part of `discover` and skip without Playwright's Chromium (`.venv/bin/python -m playwright
  install chromium`). See "How the user likes to work" for screenshots.
- Lint: `.venv/bin/python -m ruff check .`. ruff enforces max complexity 15 (C901), so
  split functions instead of raising the limit.
- Shell syntax: `bash -n install.sh uninstall.sh scripts/*.sh tools/*.sh`. macOS ships
  bash 3.2, so use `${arr[@]+"${arr[@]}"}` for arrays that may be empty under `set -u`.
- Python target is 3.9 (CI runs 3.9, 3.11, 3.13 and 3.14 on macOS).

## How the pieces run
Separate processes that share files, not one app:
- launchd runs `scripts/trigger.sh` (30 s poll plus `WatchPaths` on `/Volumes`). It finds a
  volume with `DCIM` and uses `launchctl submit` to start `scripts/import_supervisor.sh`, which
  runs `bin/photo-offloader-import`.
- The importer writes `status.json` (`progress.py`) and starts the Google sync in the
  background after a successful import. The sync writes `google_photos_status.json`.
- The dashboard and menu bar never copy files themselves. They show that state and the catalog,
  and act by launching the `bin/` and `scripts/` programs or touching marker files such as
  `.paused`.

Two consequences:
- **The shell scripts can't see `config.py`.** They repeat its paths and take
  `PHOTO_OFFLOADER_*` environment overrides, so `isolate_config` doesn't protect them. Tests
  of the scripts pass those variables instead (see `tests/test_trigger.py`). If a path
  changes, update both sides.
- **A source checkout uses the real data.** Only code paths follow `APP_ROOT`; the archives,
  catalog and `status.json` always resolve to the real locations. Running `bin/` programs from
  the repo acts on the live install, and the dashboard also launches the installed programs
  (`PHOTO_OFFLOADER_INSTALL_DIR` redirects it). `tests/browser/harness.py` runs the page
  against a sandbox.

## Safety invariants
- Never delete from a card and never overwrite a destination file. A differing file with the
  same name becomes `name__conflict-N`. The exceptions are index files the camera rewrites
  (`NC_FLLST.DAT`, `MEDIAPRO.XML`).
- The catalog and the card manifest are caches. The filesystem is the source of truth, and a
  missing or stale cache must fall back to hashing.
- A USB or Google Photos failure never invalidates a verified local import.
- The trigger locks and the importer's session lock overlap on purpose. Keep all of them.

## Layout
- `photo_offloader/config.py`: every path and setting. Code reads `config.NAME` at call
  time, never `from config import NAME`.
- The importer is split into `card`, `media_info`, `transfer`, `catalog`, `fileops`,
  `volumes`, `progress`, `logs` and `catalog_refresh`; `importer` is only the command line.
  Imports only point downward.
- Modules call each other qualified (`catalog.catalog_row(...)`). Tests patch a name in
  the module that owns it.
- `photo_offloader/dashboard/`: `core.py` holds all state and actions (unit-tested),
  `server.py` is the stdlib HTTP server (keep its token, Host/Origin and CSP checks),
  `web/index.html` is one static page that builds the DOM with `textContent` only and
  never puts card-derived text in `innerHTML`. `menubar/`: pure `model.py` plus a
  `rumps` app.
- `bin/` launchers, `scripts/` (trigger, import_supervisor, service), `tools/`, `tests/`.
- `docs/` holds the user documentation, one topic per page, linked from a short `README.md`.
- The root `index.html` is the GitHub Pages landing page, not the dashboard page. It links to
  `docs/` and doesn't repeat it.
- The installed app (`~/Library/Application Support/PhotoOffloader`) mirrors this layout.
  `install.sh` copies `bin/`, `scripts/` and `photo_offloader/`; new top-level files must
  be added there.

## Rules that matter
- **The app is installed and running on this Mac.** Tests must never touch the real
  install or data folders, start the real Google sync, or write the real `status.json`.
  Use `tests/support.isolate_config(self, tmp)`; it refuses to run if `config` gains a
  path it doesn't cover.
  - After changing fixtures, check: `touch m; run tests;
    find ~/Library/Application\ Support/PhotoOffloader* -newer m`.
    (The running dashboard touches the catalog's `-wal`/`-shm` files; that is expected.)
- **Never run `install.sh` or `uninstall.sh` yourself.** They re-register the user's real
  LaunchAgents. To check the installed layout, copy `bin scripts photo_offloader VERSION`
  to a temp dir and run the launchers from there.
- **Real data is read-only.** Benchmark against a copy of the catalog made with the
  sqlite3 backup API, kept in the scratchpad. Read camera cards only.
- No new required runtime dependencies without asking. The dashboard is deliberately
  stdlib plus one static page.

## How the user likes to work
- Write the failing test first, confirm it fails for the right reason, then fix. Mutation-check
  important fixes (break the code, make sure the test goes red).
- Base claims on measurements (logs, timings on real data), not guesses.
- Update `CHANGELOG.md` (`[Unreleased]`) and `docs/` (or `README.md`) with behavior changes.
- Commits in reviewable units: the message explains why, and behavior changes stay
  separate from moves and renames.
- Work on a branch from `main` and open a PR; merge with a merge commit (not squash).
- Follow `CODE_QUALITY.md` for clean code, reuse, errors, logging and ease of use.
- UI preferences: a modern look, controls hidden until needed, one fixed-size transfer
  panel for the whole run, nothing that flashes in or out. Check page changes with the
  browser tests, and look at the result: set `PHOTO_OFFLOADER_TEST_SCREENSHOTS=<folder>` and run
  `tests.browser.test_dashboard_page.TestScreenshots`. A new page state gets a scenario in
  `tests/browser/harness.py`.
