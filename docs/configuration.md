# Configuration

Most people won't need anything here. Eject and Terminal behaviour are switches in the dashboard's
**⋯** menu.

## The settings file

The archive folders and the backup drive are in a settings file. To change them, choose
**Edit settings…** in the dashboard's **⋯** menu or the menu bar. It opens the file in your text
editor, creating it with the values in use the first time. Change a value after the `=` and save:
the next import uses it, and the dashboard and menu bar follow within a few seconds. There's nothing
to restart or reinstall.

```text
archive_folder = ~/Pictures/CameraImports
raw_archive_folder = ~/Pictures/CameraImportsRaw
backup_drive = Photos
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `archive_folder` | `~/Pictures/CameraImports` | Where photos, video and other files go |
| `raw_archive_folder` | `~/Pictures/CameraImportsRaw` | Where camera RAW files go. It must be a different folder, and not inside the other one |
| `backup_drive` | `Photos` | The USB drive's name as Finder shows it. Backups go in its `CameraImports` folder |

The file is `~/Library/Application Support/PhotoOffloaderData/settings.txt`, so it survives
reinstalling, updating and uninstalling. Lines starting with `#` are notes. Without the file, the
defaults apply.

**Changing a folder doesn't move what's already in the old one.** The next import copies whatever is
still on the card into the new folder. To keep one archive, stop Photo Offloader, move the folder in
Finder, change the setting, start it again, and choose **Update file catalog** in the ⋯ menu so the
catalog knows the files' new place.

**With a new backup drive,** the first backup copies the whole archive to it. *Back up when the drive
connects* only starts on a drive that already holds a backup, so start the first one yourself with
**Back up to USB now**.

**If the file has a mistake,** such as a setting given twice or a folder that isn't a full path, the
dashboard says which line and why, and nothing is imported, backed up or uploaded until it's fixed.
It never falls back to the defaults on its own, so photos can't end up somewhere you didn't choose.

## Settings in the code

Other import settings are in `photo_offloader/config.py`. Installing replaces the installed copy,
so change the file in your clone and run `./install.sh` again.

| Setting | Default | Meaning |
| --- | --- | --- |
| `EJECT_CARD_AFTER_SUCCESS` | `True` | Allows ejecting. The dashboard's switch must also be on |
| `VERIFY_WITH_SHA256` | `False` | Forces hashing for size-based comparisons too (same as `--sha256`). Copies are always verified |
| `USE_FILE_BIRTH_TIME` | `True` | Use the creation time for files with no capture date |
| `MIN_FREE_SPACE_BUFFER_BYTES` | 100 MiB | Free space to keep on each destination |

File types and card locations are in `photo_offloader/card.py`. To change what's uploaded to Google
Photos, edit `INCLUDE_EXTENSIONS` in `photo_offloader/google_photos.py`.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `PHOTO_OFFLOADER_GPHOTOS_REMOTE` | `gphotos` | The rclone remote to upload to |
| `PHOTO_OFFLOADER_GPHOTOS_ALBUM` | `Photo Offloader` | The album to upload to |
| `RCLONE_CONFIG` | `~/.config/rclone/rclone.conf` | rclone's own setting: where its config is |
| `PHOTO_OFFLOADER_TERMINAL_APP` | `iTerm2` | Terminal to use when **Show Terminal during imports** is on. Also accepts `Terminal` |
| `PHOTO_OFFLOADER_REMOUNT_GRACE_SECONDS` | `20` | How long a card must be out before re-inserting it starts a new import |
| `PHOTO_OFFLOADER_OPEN_DASHBOARD` | `1` | Set to `0` so a new import doesn't open the dashboard |
| `PHOTO_OFFLOADER_DASHBOARD_MODE` | unset | Set to `browser` to use a browser tab instead of the native window |

The scripts and tests use other `PHOTO_OFFLOADER_*` variables to redirect paths and commands. They
are not settings, so leave them alone.

### Setting environment variables

For a single command you run yourself, put the variable in front of it:

```bash
PHOTO_OFFLOADER_GPHOTOS_REMOTE=my-photos \
  ~/Library/Application\ Support/PhotoOffloader/bin/photo-offloader-google-sync --check
```

The background programs need it set for all of launchd, because the importer runs as its own launchd
job and doesn't inherit the environment of the trigger that started it. A variable added to a LaunchAgent
plist would not reach the importer, or the upload that the importer starts. Use `launchctl setenv`, then
restart the service so the running programs pick it up:

```bash
launchctl setenv PHOTO_OFFLOADER_GPHOTOS_REMOTE my-photos

~/Library/Application\ Support/PhotoOffloader/scripts/service.sh stop
~/Library/Application\ Support/PhotoOffloader/scripts/service.sh start
```

`launchctl setenv` doesn't survive a restart of your Mac, so you need to run the first command again
after each one. To undo it, run `launchctl unsetenv NAME` and restart the service.

## Installed files

| What | Where |
| --- | --- |
| The app, replaced on each install | `~/Library/Application Support/PhotoOffloader`, including `Photo Offloader Menu Bar.app` |
| The app you open, and its link | `~/Applications/Photo Offloader.app`, and `Photo Offloader` on the Desktop |
| Settings, catalog, history, run reports and preferences, kept on uninstall | `~/Library/Application Support/PhotoOffloaderData` |
| Background agents | `~/Library/LaunchAgents/com.user.photooffloader.plist` and `com.user.photooffloader.menubar.plist` |

Uninstalling removes the app, its agents and its logs. Your photos and
`PhotoOffloaderData` stay. To see exactly what it would do first, run `./uninstall.sh --dry-run --verbose`.
