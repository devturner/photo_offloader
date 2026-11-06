# Development

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -r requirements-ui.txt
```

Use `.venv/bin/python` for everything below. The system `python3` has no Pillow. The code targets
Python 3.9, and CI runs 3.9, 3.11, 3.13 and 3.14 on macOS.

## Repository layout

```text
bin/              entry points: photo-offloader-import, -dashboard, -menubar, -google-sync, -usb-backup
photo_offloader/
    config        every path and setting
    importer      the import command line
    card          media types, card scanning, card manifest
    media_info    EXIF and ExifTool, camera model, archive folder
    transfer      copy decisions, copying a card, USB backup
    catalog       SQLite catalog, history, upload records
    catalog_refresh, fileops, jsonfile, volumes, progress, logs
    google_photos, usb_backup
    dashboard/    core (state and actions), display (wording and cards), paths, server, launcher, web/index.html
    menubar/      model, app
scripts/          trigger.sh (launchd), import_supervisor.sh, service.sh (start, stop, status)
tools/            benchmark and one-off migration scripts
tests/            the unittest suite; browser/ runs the dashboard page in Chromium
docs/             these pages
index.html        the GitHub Pages landing page, which is not the dashboard page
install.sh        installs the same layout into ~/Library/Application Support/PhotoOffloader
```

## Running the tests

```bash
.venv/bin/python -m unittest discover                 # everything
.venv/bin/python -m unittest tests.test_usb_backup    # one module
.venv/bin/python -m ruff format .                     # format; CI fails on unformatted code
.venv/bin/python -m ruff check .                      # lint; maximum complexity is 15
bash -n install.sh uninstall.sh scripts/*.sh tools/*.sh
```

macOS ships bash 3.2, so write `${arr[@]+"${arr[@]}"}` for an array that may be empty under `set -u`.

### Coverage

```bash
.venv/bin/python -m coverage run -m unittest discover
.venv/bin/python -m coverage combine                  # merges the data from test subprocesses
.venv/bin/python -m coverage report                   # add `html` for a browsable report in htmlcov/
```

The Coverage workflow runs the same steps, keeps the HTML report as an artifact, and fails below
`fail_under` in `.coveragerc`. Raise that number when you add tests. The number measures Python
only: the shell scripts and the JavaScript in `web/index.html` are tested but not counted, and
`menubar/app.py` is excluded because no test imports it.

### The dashboard page

The browser tests need one extra download. Without it they skip, and the rest of the suite still runs.

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/python -m unittest tests.browser.test_dashboard_page
```

To look at the page in every state, in light, dark and narrow, save screenshots to a folder:

```bash
PHOTO_OFFLOADER_TEST_SCREENSHOTS=/tmp/dashboard-shots \
  .venv/bin/python -m unittest tests.browser.test_dashboard_page.TestScreenshots
```

A new page state gets a scenario in `tests/browser/harness.py`.

## Rules

**Tests never touch a real install.** This project is often developed on a Mac where the app is
installed and running. Tests must not change the real install or data folders, start the real Google
sync or write the real `status.json`. Use `tests/support.isolate_config(self, tmp)`, which refuses to
run if `config` gains a path it doesn't cover. After changing fixtures, check nothing real changed:

```bash
touch m; .venv/bin/python -m unittest discover; find ~/Library/Application\ Support/PhotoOffloader* -newer m
```

The running dashboard touches the catalog's `-wal` and `-shm` files, which is expected.

**A source checkout uses the real data.** Only code paths follow the checkout. Archives, the catalog and
`status.json` always resolve to the real locations, so running `bin/` programs from the repository acts
on the live install. `tests/browser/harness.py` runs the page against a sandbox.

**Shell scripts can't see `config.py`.** They repeat its paths and accept `PHOTO_OFFLOADER_*` overrides, so
`isolate_config` doesn't protect them. Tests pass those variables instead (see `tests/test_trigger.py`). If
a path changes, update both sides.

**Don't run `install.sh` or `uninstall.sh` to test a change.** They re-register your real LaunchAgents. To
check the installed layout, copy `bin scripts photo_offloader VERSION` to a temp folder and run the
launchers from there.

**Keep the safety invariants** in [How it works](how-it-works.md#the-safety-model). The catalog and card
manifest are caches, and the trigger and importer locks overlap on purpose.

**No new required runtime dependencies** without discussion. The dashboard is deliberately the standard
library plus one static page, and it builds the page with `textContent`, so text from a card never reaches
`innerHTML`.

## Code conventions

- Code reads `config.NAME` at call time, never `from config import NAME`.
- Modules call each other qualified (`catalog.catalog_row(...)`), imports only point downward, and tests
  patch a name in the module that owns it.
- `install.sh` copies `bin/`, `scripts/` and `photo_offloader/`. New top-level files must be added there.

## Contributing changes

- Follow the [code quality guide](../CODE_QUALITY.md).
- Write the failing test first and check it fails for the right reason. For important fixes, break the code
  again and confirm the test goes red.
- Base claims on measurements (logs, timings on real data). Benchmark against a copy of the catalog.
- Record behaviour changes in `CHANGELOG.md` under `[Unreleased]`, and in these docs.
- Keep commits reviewable, and explain why in the message. Keep behaviour changes separate from moves and
  renames.
- Work on a branch from `main`, open a pull request, and merge with a merge commit.
