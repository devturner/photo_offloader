# How it works

## The safety model

- **Nothing is deleted from a card**, and existing files in the archive are never overwritten.
- **Every copy is verified.** The SHA-256 of the source is computed while copying, so the card is
  read once. The copy is written past the macOS file cache and flushed out of the drive's own cache,
  then read back from the destination and compared. It only gets its final name if the hashes
  match. The Mac-to-USB copy works the same way.
- **The source is watched during the copy.** If a file changes or disappears mid-copy, that copy
  counts as failed.
- **Free space is checked first.** Every destination needs room for the files plus a 100 MiB buffer.
- **Each stage stands alone.** A USB or Google Photos failure never invalidates a verified copy on
  the Mac.
- **Only one import runs at a time.** Mounting a card can fire several events, so the trigger
  and the importer each have their own locks.

Still, treat this as backup infrastructure rather than your only copy. Check your files on both
destinations after installing or upgrading, before you format a card.

### Same name, different file

When a file with the same name already exists, the size and SHA-256 are compared:

- **Identical:** the file is skipped.
- **Different:** the new file is kept next to the old one, as `DSC_1234__conflict-1.NEF`.

The one exception is the index files that cameras rewrite on every shoot (`NC_FLLST.DAT`,
`MEDIAPRO.XML`). The newest version replaces the old one, on the Mac and on USB. They aren't photos,
and keeping every version would create a new conflict copy on every import.

## Archive layout

Files are filed by month and camera, then by type:

```text
~/Pictures/CameraImports/            photos, video, other files
└── 2026-11/
    └── NIKON-Z-8/
        ├── Photos/DSC_0002.JPG
        ├── Movies/DSC_0004.MOV
        └── Other/CLIP0001.XML

~/Pictures/CameraImportsRaw/         RAW files
└── 2026-11/
    └── NIKON-Z-8/
        └── Raws/DSC_0001.NEF

/Volumes/Photos/CameraImports/       USB: both of the above, in one tree
```

- **The month** comes from the photo's capture date. A file without one uses its file creation or
  modification time, and that is also how video is dated.
- **The camera** comes from the file's metadata. Reading it from video needs
  [ExifTool](https://exiftool.org); without ExifTool, or without usable metadata, the folder is
  `Unknown-Camera`.
- **Everything on the card is copied.** As well as `DCIM`, the importer scans `PRIVATE` and
  `MP_ROOT`, where Sony, Canon and Panasonic cameras keep video. Unknown file types go to `Other`
  and are listed in the log, so nothing is left behind on a card you then format. Hidden files,
  including macOS `._*` files, are skipped.

Archives from an older version may have RAW files in `CameraImports`.
`tools/move_raw_to_separate_folder.sh` moves them, and previews the moves unless you pass `--apply`.

## The catalog and the card manifest

These make repeat imports fast. Both are caches: the files on disk are the source of truth, and a
missing or out-of-date cache just means hashing the file again.

- **The catalog** (`~/Library/Application Support/PhotoOffloaderData/catalog.sqlite3`) records known
  files, their fingerprints (size, modification time, inode), verified hashes, capture dates,
  camera models, Google Photos uploads and the run history. It lives outside the install folder, so
  it survives upgrades and uninstalling.
- **The card manifest** (`.photo-offloader/manifest-v1.json` on the card) records the hashes of files
  already verified, so a re-inserted card doesn't have to be read again. It holds no media. Read-only
  cards simply go without one.

The card's folders are still listed on every insert, so new files are always found.

**Daylight saving and time zones.** Cameras store file times in local time, so after a clock change
every file on a card can appear to have moved by exactly an hour. A shift of a whole number of hours,
up to 24, with the same size counts as unchanged. Otherwise a single clock change would mean hashing
the whole card again.

## How the parts run

These are separate programs that share files:

1. **launchd** runs the trigger (`scripts/trigger.sh`) every 30 seconds and whenever `/Volumes` changes.
   It looks for a volume with `DCIM`.
2. The trigger starts the **importer** as its own background job. The importer writes its progress to
   `status.json`.
3. After a successful import, the importer starts the **Google sync** in the background.
4. The **dashboard** and **menu bar** never copy files. They show that state and start the same
   programs when you press a button.

The dashboard's local server listens only on `127.0.0.1`. It requires a random per-session token and
checks the `Host` and `Origin` of each request, so other websites can't drive it.

## Logs

| Log | Location | Size limit |
| --- | --- | --- |
| Imports | `~/.camera_transfer.log` | Rotates at 5 MiB, keeps 3 older files |
| Card detection | `~/.camera_transfer_trigger.log` | Rotates at 1 MiB, keeps 2 older files. Only writes when something changes |
| Google Photos uploads | `~/Library/Application Support/PhotoOffloader/google_photos_sync.log` | Rotates at 5 MiB, keeps 2 older files |
| Dashboard and menu bar | `dashboard.log`, `menubar.log`, `menubar-error.log` in the same folder | Trimmed when you run `install.sh` |

Each import logs a `Run provenance` entry with the app version and Git commit, so a log can always
be matched to the code that wrote it. The Google Photos log times each step before an upload
(`Upload step: …`), and rclone adds a progress line every second during one.

The **⋯ › View logs** menu in the dashboard opens these in Console.
