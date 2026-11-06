# Changelog

All notable changes to Photo Offloader are documented here.

## [Unreleased]

### Added
- **`photo-offloader-verify` rechecks the saved copies.** Until now a copy was verified once, when it was made, so later damage or a deleted file went unnoticed. This rereads every copy on the Mac and the USB drive from the disk, not from memory, and compares it with the hash saved at the time and with the other copy. It reports damaged, changed, missing and unreadable files, copies that differ, and files left only on the drive; says for each whether the other copy is good; and changes nothing. A card inserted during a check still imports. See [docs/using.md](docs/using.md#checking-the-saved-copies).
- **A failed import now says what went wrong.** The run opens by itself in Recent activity with how many files are safe, one row per file type with what wasn't saved, and why. A line separates photos that lost both RAW and JPG from those with one copy left, and says when an unreadable folder isn't counted. If at least the last 20 files all failed to read, it says that looks like the card or reader. Until a retry succeeds, the Camera card box says not to format and **Eject** asks first. See [docs/using.md](docs/using.md#when-an-import-fails).
- **Files a card fails to read get a second try.** One pass at the end of the copy, after 5 seconds, for read errors only, stopping after 10 failures in a row. The run's full list marks any file saved this way.
- **A USB backup now says what went wrong too,** alone or after an import: which files didn't reach the drive and why, such as a different file already there, a full drive or the drive being unplugged. It says if the Mac's own copy couldn't be read.
- **Notifications only go out when you'd otherwise miss the news:** while the service is running and no dashboard is showing.
- **Debug package.** **Save debug package…** on a run saves a zip to Downloads with a summary, every card file's result, the run's log and the card's details, never photos, your Google sign-in or the dashboard's token. Each import keeps these files in `run-reports` in the data folder; a package is only built when you ask.
- **`tools/test.sh fast|full`** runs the tests at two levels: `fast` skips the slowest modules and takes about 35 s, and `full` runs everything. CI still runs everything.
- **`photo-offloader-google-sync --check`** reports each Google Photos setup step in order (rclone installed, remote present, signed in, album) and prints the exact next step for the first one missing. It only reads: it creates no album and leaves the dashboard's status alone. A sync that fails because the sign-in expired, or because of another known Google setup problem, now says how to fix it instead of showing rclone's error.
- **Browser tests for the dashboard page** (`tests/browser/`, Playwright, a development-only dependency that is never installed with the app). They run the real server and core against a temp sandbox and check every dashboard state: the fixed panel, the five slots, the step strip, truncation, the menu, the theme, the step buttons and that text from a card is never treated as HTML. CI runs them in a separate job and keeps screenshots in light and dark as artifacts.
- **Test coverage in CI.** A new Coverage workflow measures line and branch coverage, shows the results in the run's summary, keeps the HTML report as an artifact and fails below 87%. The baseline is 89%, after new tests for what happens when a USB backup goes wrong: a drive unplugged or swapped mid-backup, a corrupt copy, a file appearing at the destination, and a USB crash after a verified import. See [docs/development.md](docs/development.md#coverage).

### Changed
- **The README and project website are clearer.** The README says the app is macOS only for now, that the USB drive name and archive folders can be changed, and where the website is. The website's landing page now opens the documentation on the site instead of on GitHub, and the screenshot shows dark mode. A new [CONTRIBUTING.md](CONTRIBUTING.md) explains how to report a bug and propose a change.
- **Every page of the project website now follows light or dark mode**, with the same look as the landing page. *Get started* now opens a page on the site instead of the GitHub README.
- **A finished import now shows a report of each step:** what it did, how much it moved, how long it took and how fast. The ⋯ menu's *Import report* setting offers *Basic* (the default: a bar showing where the time went, plus what reached the Mac, USB drive and Google Photos) and *Advanced* (a table of the steps). It replaces the panel's old summary, whose *Average speed* included the time spent scanning the card.
- **Click a run in Recent activity to unfold its report under it.** Uploads are now saved with the import they followed, so past runs keep those figures.
- **The dashboard's speed graph is easier to read.** It starts at the left edge and fills the width after 10 seconds instead of sitting at the right, shows up to the latest 90 seconds instead of one minute, and moves smoothly between polls. The line is smoothed with a moving average and a monotone curve, so what's already drawn keeps its shape. With *Reduce motion* on in macOS, it updates once a second without animating.
- **An import now measures progress against its new files.** Files reads *737 of 1,278 new*, Progress and Remaining count only those files, and the Camera card box shows *1,278 new files of 2,478*. This also fixes a time estimate that started too low when already-saved files came first. A card with nothing to estimate from, on its first import or read-only, shows *737 new* and measures the whole card.
- **The Mac archive, USB backup and Google Photos boxes no longer repeat the top panel's progress bar.** Google Photos keeps its bar when an import has taken over the top panel.
- **The documentation is rewritten.** `README.md` is now one screen, with the detail in `docs/` (`using.md`, `google-photos.md`, `how-it-works.md`, `configuration.md`, `development.md`). `docs/google-photos.md` is a step-by-step setup guide, including the *In production* step that stops Google sign-ins expiring after 7 days. `index.html` (GitHub Pages) is now a short landing page instead of a second copy of the text. The docs also correct some outdated claims: the archive is filed by camera model, auto-eject is off by default, and uploads go to one album per archive folder, not a single album.
- The dashboard's *Not set up* line on the Google Photos card now names the first missing step (install rclone, or add the remote) and honors a custom remote name.
- **Internal cleanup to meet the new [code quality guide](CODE_QUALITY.md), with no change in what the app does.** Long functions are split, the JSON state files share one module, and the dashboard's display logic and paths have moved out of `dashboard/core.py`. The Python code is now formatted with `ruff format`, and CI checks it. New tests cover the SHA-256 checks on a copy, which nothing was testing before.

### Fixed
- **Photo Offloader no longer writes to camera cards.** Each card's list of verified hashes used to be saved on the card, before anything was copied. It's now on the Mac, in `PhotoOffloaderData/card-manifests/`, one file per card, named by its volume ID. Lists left on cards are ignored and stay there. The first insert of a card after this update has no list: unless the catalog still knows its files they're hashed once more, there's no *new files* estimate, and the space check counts the whole card, so a nearly full Mac can refuse it.
- **A new photo can't be skipped because of an old photo's cached hash.** The catalog keeps the hashes of card files after a format, and now the card lists do too, so a new photo with an old one's path, size and time could look saved. Before skipping a file on a cached hash, the first and last 64 KB are now compared with the saved copy; if they differ, both files are hashed and the photo is saved as a conflict copy. This costs about 2.3 ms a file on an SD card, about 6 s on a full 2,700-file card. It also catches a saved copy that has changed.
- **A stuck `diskutil` can't hang an import while reading a volume's ID.** The call now gives up after 30 seconds, and the card's files are hashed instead.
- **The free space check is now right in both directions.** It checked the two archive folders separately, so 60 GB of photos and 60 GB of RAW passed with 100 GB free and then ran out of space partway through. It also counted files already imported, so a nearly full Mac could refuse a card with only a few new photos. It now adds up folders on the same disk and counts only files that look new.
- **Importing a card again no longer adds another conflict copy.** Two different photos with one name are kept as `DSC_0001.JPG` and `DSC_0001__conflict-1.JPG`, but each re-insert only compared against the first, so it added `conflict-2`, `conflict-3` and so on, and sent each one to USB and Google Photos. A file matching an existing conflict copy is now skipped.
- **A second card now imports while the first stays mounted.** The trigger only looked at the first card it found, and auto-eject is off by default, so a second card was ignored until the first was removed. Cards now import one after another, each once. The dashboard's Camera card box still shows only the first card.
- **A Homebrew cleanup can no longer stop imports silently.** The install could be tied to one Homebrew Python version, and when that was removed the trigger only wrote a line to its log. `install.sh` now builds from a path that survives upgrades, and rebuilds an install tied to anything else. If the Python still goes missing, a notification says to run `install.sh` again.
- **A power cut can no longer stop imports for good.** If the Mac lost power or the trigger was killed during its few seconds of work, its lock stayed behind and every later run gave up, so no card ever imported again. A lock whose owner is gone, or more than 10 minutes old, is now replaced.
- **A USB or eject problem no longer stops the upload.** The upload only started if the USB backup and the eject both worked, so one bad USB file could keep every new photo from Google Photos until someone noticed. It now starts whenever the photos are verified on the Mac, and the report shows it with the import it followed.
- **A card folder that can't be read now fails the import.** It used to be logged and skipped, so the import read *complete*, the card could be ejected and the upload started, with that folder's photos left behind. This is how a failing card behaves, and someone might then format it. What can be read is still copied. The card stays mounted and the message says to keep it.
- **Verifying a copy now reads the disk, not memory.** The check bypassed the macOS cache only for its read, which still returned the pages the copy had just written, so a bad write could pass. Copies now skip the cache as well. On 2 GB of real RAW files the pages held in memory before verifying went from 125,264 to 0, and the copy, check and install together went from 2.8 s to 4.3 s on the Mac's SSD. A USB backup slows by more on a slower drive, because each file is now really read back.
- **A verified copy is now flushed out of the drive's own cache,** along with its folder. On macOS `fsync` stops at the drive's cache, so a power cut or an unplug could lose a file already reported as verified. Drives without the full flush use `fsync`.
- **The speed graph starts when data starts moving.** It rises smoothly from zero instead of jumping to full height, and no flat line runs along the bottom while files are checked or rclone gets ready.
- **While an upload got ready, the dashboard showed the previous upload's last line from the log.** Progress read 100% and *Finishing upload* for about 15 seconds, and the graph was thrown away when real figures arrived. It now shows *Preparing upload* until this upload reports.
- **The progress bar no longer flashes while there's nothing to measure.** The sliding bar during scanning, catalog updates and the wait before an upload restarted every second. The whole bar now glows slowly until there is progress to show, or stays still with *Reduce motion* on.
- **Uploads start sooner.** An album that files have already been uploaded to is no longer checked with Google Photos first. If an upload fails, the check runs then, so the error still says why. The sync log now times each step before an upload.
- **The upload graph follows the real speed instead of moving in 5-second steps.** rclone now reports every second instead of every 5, so the figures and time left update as often as an import's. Only a report with no new bytes counts as a stall, and a retry no longer wipes the graph.
- **A long stall no longer distorts the graph.** The speed left over from before the stall can't fill the height once the fast part has scrolled off.
- The website's *Contributing* link no longer returns a 404.
- On a drive without hard links, such as an exFAT USB drive, a matching file that appeared at the destination during a copy was reported as a failure. It now counts as already copied, the same as on other drives. The existing file was never overwritten.
- **Upgrading no longer fails now and then with `Bootstrap failed: 5: Input/output error`.** `launchctl bootout` returns before launchd has removed a running job, so if the trigger was mid-poll, `install.sh` tried to load it again too soon and stopped, leaving the service and menu bar off. `install.sh` now reloads the agents through `service.sh restart`, which waits until each one is gone. That also fixes `service.sh restart` itself, which could say *started* with nothing running, and lets `install.sh` turn the service back on after `service.sh stop --disable`.
- **Step labels and values are no longer cut off in a narrow window.** Below 560 px wide, step labels wrap onto two lines and Progress and Files share the first row of the transfer panel, which is 10 px taller there to fit.
- A failed or canceled import no longer says *Remaining: Done* in the transfer panel; it shows a dash.
- The dashboard closes its read-only catalog connection after counting files, instead of leaving it for the garbage collector on every refresh.
- **Failures that left no trace are now logged.** A file the Google Photos sync can't read is named in the sync log (it was skipped silently, so a sync could report *up to date* without it). The log also records a dashboard setting that can't be saved, a partial copy that can't be removed, and a menu-bar failure to reset the last-card record, which can stop a mounted card importing after a resume.
- The Google Photos sync no longer stops if its status file holds valid JSON that isn't an object.
- **The Mac archive box now says *Checking* while an import is checking for new files.** It said *Copying* for that whole step, even when the panel above said *Checking for new files* and nothing was being copied.
- **Camera index files are no longer imported, so a card with no new photos now reports no copies.** `NC_FLLST.DAT` and `MEDIAPRO.XML` were copied on every insert and reported as *Local copies: 2*. A card has one per folder, all filed under one archive path, so each overwrote the last and the archive only ever held one: it was never a backup. Copies already in the archive and on USB are left alone.
- **Checking a card for new files is faster.** The catalog was reopened for every lookup, three times per file; each file type now keeps one connection open. Each save is still committed straight away.
- **Checking a card no longer rewrites the status file for every file.** It wrote about 4 times per file, which took 8.8 s of a 25.7 s check on a 2,722-file card. It now writes at most 4 times a second, and the dashboard reads about once a second.
- **Re-inserting an unchanged card no longer reads its photos.** Cameras only add files, so if every file from the last import is still on the card with the same path, size and time, the card can't have been formatted and the 64 KB comparison is skipped. If anything is missing or changed, that import does the comparison. New files are copied and verified as always.
- **The panel no longer flips to *Copying new files* on a card with nothing to copy.** Each file found already saved reported its state as copying.
- **A report no longer says *Copy 20s* when nothing was copied.** With no new files, that time is shown as *Check*, and the advanced table says *Check for new files*.
- **The import log now shows where the time went**, with one line per file type, such as `Time for JPG, 1361 file(s): 7.5 s: end checks 3.9 s, status 1.3 s, catalog 1.0 s, other 1.3 s`.

### Changed (2.5)
- **Repository and install layout.** Code is now a `photo_offloader/` package (importer, Google Photos, USB backup, `dashboard/`, `menubar/`), with entry points in `bin/` (`photo-offloader-import`, `-dashboard`, `-menubar`, `-google-sync`, `-usb-backup`), shell scripts in `scripts/` (`trigger.sh`, `import_supervisor.sh`, `service.sh`), helpers in `tools/` and tests in `tests/`. The installed app mirrors this layout. Re-running `./install.sh` upgrades an older install in place: it stops old and new processes, removes the old flat files and rewrites the LaunchAgents. `uninstall.sh` handles both layouts. The service script is now `~/Library/Application Support/PhotoOffloader/scripts/service.sh`.
- **The 2,300-line importer is split into focused modules** (`config`, `logs`, `progress`, `fileops`, `catalog`, `card`, `media_info`, `volumes`, `transfer`, `catalog_refresh`; `importer` is now only the command line). Every path and tunable lives in `photo_offloader/config.py`, and `tests/support.py` redirects all of them for tests.
- CI lints the whole repository (`ruff check .`, configured in `ruff.toml`) instead of a hand-kept file list.

### Removed
- `bundle_script.sh` and `running_bash.sh` (v2.2 leftovers), the Tk-era tests and helper re-exports, and unused functions.

### Changed
- **The dashboard is now a local web app** (`dashboard_web/index.html`, served on `127.0.0.1` by `dashboard_server.py`, state from `dashboard_core.py`) instead of a Tk window. It follows macOS light/dark mode, shows a headline status, a four-step pipeline (card, Mac archive, USB, Google Photos), live progress with a speed graph, and recent activity. Controls are hidden behind a **⋯** menu; Back up/Sync actions appear on a card only when they apply. It opens in a native window when `pywebview` is installed and in the browser otherwise (`PHOTO_OFFLOADER_DASHBOARD_MODE=browser` forces the browser). `photo_offloader_ui.py` is now the single-instance launcher. **tkinter is no longer required** and the installer no longer checks for it.
- The menu bar is slimmer (Open Dashboard, Copy to USB when a drive is connected, Pause/Resume, Stop, Quit) and refreshes itself every few seconds.

### Added
- **Persistent import history.** Every import, USB backup and catalog update is recorded in the catalog (`import_history`, latest 500 runs), so history survives restarts and reinstalls. The result of the last transfer fades after 10 minutes; failures stay until dismissed; **Clear** deletes the history.
- Dashboard server hardening: per-session token in a custom header, `Host` and `Origin` validation, JSON-only writes, single-file static serving with a Content-Security-Policy, no CORS, and idle shutdown. The launcher is single-instance and serialised by a lock.
- `photo_offloader_service.sh stop --keep-ui` stops only the trigger and leaves the menu bar and dashboards running.

### Changed
- **One transfer panel from start to finish.** The dashboard no longer drops back to the import result between copying and uploading: the sync reports itself as soon as it has files to send (its elapsed time counts from then), and the moment before it starts shows *Starting upload*. When everything is done, the same panel shows a summary (all steps, new files, size, average speed, total time) until it fades after 10 minutes or is dismissed.
- The importer's last second no longer drops the panel to the card headline ("already imported"): an import status that has just turned *complete* before the run reaches the history shows as *Finishing import*.
- **Dashboard log** (`dashboard_events.log`, **View logs › Dashboard**): one line each time what the dashboard shows changes, with the importer, upload and card state behind it.
- **Eject** buttons on the Camera card and USB backup cards (hidden while a transfer is using that volume).
- **The Google Photos upload is the last step of an import**, not a separate view: the step strip reads Scan card › Read photo info › Copy new files › Back up to USB › Upload to Google Photos, and an upload that starts right after an import keeps the import's steps and card name. During the upload, Files (from rclone's transfer count) and Elapsed are filled in and the speed graph runs, instead of showing dashes; the subtitle names the card and files rather than repeating the byte count.
- **Update file catalog is faster**: each folder is walked once, read from the catalog in one query and written back in one transaction (2,781 files: 1.8 s instead of 4.9 s). Videos are now dated by file time there too, the same rule an import uses.
- **USB backup compares the archive with the drive using one catalog read and one write** instead of three catalog round trips per file: 2,784 files in 0.08 s instead of about 5 s, and it no longer grows with the archive. A missing hash is computed only for the side that lacks one.
- **The dashboard says what an import is actually doing and no longer jumps around.** The headline names the step (*Scanning card*, *Reading photo info*, *Checking for new files*, *Copying new files*, *Backing up to USB*, *Updating file index*) above a step strip. The progress panel has one fixed size; progress, speed, files (checked, total, new), elapsed and remaining time have fixed slots that show a dash when unknown; progress uses one overall measure; and the speed graph is a steady one-minute window. Previously every file flipped the status between checking and copying, so the title, bar, timers and graph flashed and the panel resized.
- **Recent activity shows what each session backed up**: new files by type and size, then the card total, and USB copies.
- **View logs** in the dashboard **⋯** menu (import, card detection, Google Photos) and **View Import Log** in the menu bar open the log in Console, which follows it live. Only those three logs can be opened.
- The dashboard window has a fixed size (960×860); Recent activity fills the rest of it and scrolls, showing up to 100 runs instead of a *Show more* link.
- **Appearance setting** (Auto/Light/Dark) in the **⋯** menu, remembered across restarts. Switches and Appearance leave the menu open.
- **Fresh cards start copying minutes sooner.** Capture date and camera model for new RAW and video files are read in batched ExifTool runs right after the card scan, instead of starting ExifTool twice per file. On a 1,188-RAW Nikon Z 8 card this took 20 s instead of about 340 s. Results go to the catalog in one transaction; the archive layout is unchanged.

### Fixed
- **After a reinstall the dashboard asked to set up Google Photos again although rclone was still configured.** It only trusted its own status file (replaced on every install) and a preference written after a sync that uploaded something. It now also checks rclone's config (`$RCLONE_CONFIG` or `~/.config/rclone/rclone.conf`) for the `gphotos` remote, reading section names only.
- **Re-inserting the same card after shooting more never imported the new photos.** The dashboard said "waiting to start import" but the trigger kept the card's successful handled-state across the unmount indefinitely. The trigger now records when the card disappeared; if the same card returns after the grace period (20 s, `PHOTO_OFFLOADER_REMOUNT_GRACE_SECONDS`) it starts a new session, which copies only new files. A card that is already imported is shown as such, with an **Import now** action, instead of "waiting".
- **After a daylight-saving change the whole card was re-hashed and re-read for metadata.** Cameras store FAT/exFAT timestamps in local time, so after DST ended every file on the card read exactly one hour later and counted as changed (2,346 of 2,364 files in the reported log; about 6 minutes for 18 new photos). Card-manifest and catalog lookups now treat an exact whole-hour mtime shift (up to 24 h) with the same size as unchanged.
- **Every import left another `NC_FLLST__conflict-N.DAT` in `Other`, and USB backup then failed on it.** Nikon rewrites `NC_FLLST.DAT` on every shoot. Camera index files (`NC_FLLST.DAT`, `MEDIAPRO.XML`) now replace their previous copy locally and on USB; other changed files still get conflict copies. Earlier `NC_FLLST__conflict-*.DAT` files can be deleted.
- Log lines written during an import no longer start with `on <n>:`. The progress bar is only drawn on a terminal and no longer rewrites log output.
- **Stopping Photo Offloader from the menu bar left no way to start it again.** The Stop action unloaded the menu-bar agent itself, so the menu was torn down while on screen (greyed out, including Quit) and nothing offered Start. The menu bar and dashboard now stay alive in a *stopped* state with a **Start** item/button.
- The dashboard no longer shows "copying" forever when the importer dies: an active status that nobody is updating is reported as *interrupted*. A Google Photos sync that died mid-upload is likewise reported as interrupted instead of "uploading" indefinitely.

### Fixed
- **A second card with the same volume name was never imported.** The trigger kept successful handled-state across unmount keyed only on the `/Volumes/<name>/DCIM` path, so every later card from the same camera body (same volume name) was suppressed as "already handled". Handled-state now records the card's volume UUID, and a different card at the same path starts a new import.
- **Files outside the recognised extension list were silently left on the card while the import reported success.** The scan now covers `PRIVATE` and `MP_ROOT` (Sony/Canon/Panasonic video) as well as `DCIM`, recognises more formats (`.hif`, `.x3f`, `.crw`, `.mxf`, ...; `RAW_EXTENSIONS` and the import filter had drifted apart and are now a single definition), and copies every other file to an `Other` folder, logging the unrecognised extensions. `Other` files are included in USB backup.
- **A crashed Google Photos sync could disable uploads forever.** The `O_EXCL` lock file was never cleaned up after a crash, SIGKILL or power loss, and its PID was never checked. The sync now uses `flock`, which the OS releases when the holder dies. A duplicate launch no longer overwrites the live run's progress status.
- **Files imported while an upload was running were recorded as uploaded without being sent.** The sync now snapshots the files to upload, passes exactly that list to rclone with `--files-from`, and records only those files. Nothing new to upload no longer starts rclone at all.
- The importer exits cleanly on `SIGTERM` (service stop, launchd): the lock is released and the dashboard shows "canceled" instead of a frozen "copying" state.
- The trigger could fail under macOS's stock `/bin/bash` 3.2 when auto-eject was enabled, because `"${array[@]}"` on an empty array is an error under `set -u` there.
- `datetime.utcnow()` (deprecated since Python 3.12) replaced.

### Added
- `photo_offloader_service.sh` (`status | stop | start | restart`) and a **Stop Photo Offloader…** menu-bar item. Quitting the menu bar previously left the trigger LaunchAgent polling; there was no supported way to stop the service short of uninstalling.
- Log size caps: the importer log uses a rotating handler (5 MiB x 3), the trigger log rotates at 1 MiB, the rclone and launcher logs rotate before each sync, and the installer trims launchd-captured logs. The trigger now logs only when its output changed since the previous 30-second poll; idle polls were writing about 400 KB per day.

### Performance
- Card files are read once instead of twice: the SHA-256 is computed while copying. The same applies to the Mac SSD to USB backup, and to hard-linked installs, which no longer re-read the destination a second time.
- Destination verification reads with `F_NOCACHE` (macOS), so it checks the disk rather than the page cache.
- `status.json` is rewritten at most four times per second instead of once per 1 MiB chunk; byte counters remain exact.
- The catalog uses SQLite WAL mode with `synchronous=NORMAL`, so the dashboard no longer blocks behind importer writes; Google Photos upload records are written in one transaction instead of one connection per file.
- Card scanning uses one `scandir` pass instead of `rglob` with per-entry `stat` calls; the trigger no longer starts a Python interpreter on every poll.

### Changed
- README: card detection (any `DCIM` volume), what is scanned, verification behaviour, log caps, and stopping the service now describe the current behaviour.
- `*.heic` / `*.heif` are included in Google Photos uploads.

### Changed
- Automatic import detection now accepts any mounted volume containing a top-level `DCIM` directory, regardless of volume name or camera manufacturer.
- Added regression coverage for manufacturer-independent DCIM detection.


### Fixed
- Import supervisor now removes its named `launchctl submit` job after writing terminal status, preventing a completed import worker from being relaunched under the stale label.
- Added regression assertions that worker-job cleanup follows success/failure status persistence and disables traps before launchd cleanup.
- Installer now waits for the submitted import job and app-owned import processes to stop before replacing installed scripts; it aborts safely if shutdown cannot be verified.
- Uninstaller now verifies both launchd registration and installed-script processes have disappeared before removing application files or LaunchAgent plists, retaining files when cleanup is incomplete.
- Added regression tests for installer shutdown ordering and uninstaller process verification.

### Fixed
- Fixed a duplicate `fi` in the camera trigger that caused a shell syntax error and stopped trigger processing immediately after build-provenance logging; this also caused the trigger regression tests to fail.


### Fixed
- Stop automatic relaunch loops by preserving handled-card state when a background worker exits without a visible success record; retry is deferred until the card is unmounted and reinserted.
- Added regression coverage ensuring a handled mount without completion status is not relaunched on every trigger poll.


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
