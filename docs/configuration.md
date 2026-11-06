# Configuration

Most people won't need anything here. Eject and Terminal behaviour are switches in the dashboard's
**⋯** menu.

## Settings in the code

Paths and import settings are in `photo_offloader/config.py`. Installing replaces the installed copy,
so change the file in your clone and run `./install.sh` again.

| Setting | Default | Meaning |
| --- | --- | --- |
| `LOCAL_DEST_ROOT` | `~/Pictures/CameraImports` | Archive for photos, video and other files |
| `LOCAL_RAW_DEST_ROOT` | `~/Pictures/CameraImportsRaw` | Archive for RAW files |
| `USB_DEST_VOLUME` | `/Volumes/Photos` | The backup drive |
| `USB_DEST_ROOT` | `/Volumes/Photos/CameraImports` | Where the backup goes on that drive |
| `EJECT_CARD_AFTER_SUCCESS` | `True` | Allows ejecting. The dashboard's switch must also be on |
| `VERIFY_WITH_SHA256` | `False` | Forces hashing for size-based comparisons too (same as `--sha256`). Copies are always verified |
| `USE_FILE_BIRTH_TIME` | `True` | Use the creation time for files with no capture date |
| `MIN_FREE_SPACE_BUFFER_BYTES` | 100 MiB | Free space to keep on each destination |

The easiest way to use a different USB drive is to name it `Photos`. If you change `USB_DEST_VOLUME`,
also change `USB_VOLUME` in `photo_offloader/menubar/app.py`, which keeps its own copy.

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
| The app, replaced on each install | `~/Library/Application Support/PhotoOffloader` |
| Catalog, history and preferences, kept on uninstall | `~/Library/Application Support/PhotoOffloaderData` |
| Background agents | `~/Library/LaunchAgents/com.user.photooffloader.plist` and `com.user.photooffloader.menubar.plist` |

Uninstalling removes the app, its agents and its logs. Your photos and
`PhotoOffloaderData` stay. To see exactly what it would do first, run `./uninstall.sh --dry-run --verbose`.
