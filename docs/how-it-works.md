# How it works

## The safety model

- **Nothing is deleted from a card**, and existing files in the archive are never overwritten.
- **Every copy is verified.** The SHA-256 of the source is computed while copying, so the card is
  read once. The copy is written past the macOS file cache and flushed out of the drive's own cache,
  then read back from the destination and compared. It only gets its final name if the hashes
  match. The Mac-to-USB copy works the same way.
- **The source is watched during the copy.** If a file changes or disappears mid-copy, that copy
  counts as failed.
- **A card that can't be fully read isn't called complete.** What can be read is copied, but an
  unreadable folder fails the import. The card stays mounted, and no USB backup or upload starts.
- **Free space is checked first.** Each disk needs room for the files that look new, plus a 100 MiB
  buffer. If that estimate is wrong and a disk fills up, the affected copies fail and are reported.
- **Each stage stands alone.** A USB or Google Photos failure never invalidates a verified copy on
  the Mac.
- **Only one import runs at a time.** Mounting a card can fire several events, so the trigger
  and the importer each have their own locks.

- **Copies can be rechecked later.** [`photo-offloader-verify`](using.md#checking-the-saved-copies)
  rereads them from the disk and reports what no longer matches. It runs only when you start it.

Still, treat this as backup infrastructure rather than your only copy. Check your files on both
destinations after installing or upgrading, before you format a card.

### Same name, different file

When a file with the same name already exists, the size and SHA-256 are compared:

- **Identical:** the file is skipped.
- **Different:** the new file is kept next to the old one, as `DSC_1234__conflict-1.NEF`. It's also
  compared with any conflict copies already there, so importing a card again doesn't add another.

Index files that cameras keep for themselves (`NC_FLLST.DAT`, `MEDIAPRO.XML`) aren't imported. A card
has one per folder and they aren't photos. Copies left in the archive by older versions stay there,
and if a USB copy differs from the Mac's, the Mac's replaces it: that's the one exception to the rule
above.

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
- **Three folders on the card are copied:** `DCIM`, plus `PRIVATE` and `MP_ROOT`, where Sony, Canon
  and Panasonic cameras keep video. Anything else at the top level of the card is not copied, so
  check for it before you format a card a camera has stored files on elsewhere. Unknown file types
  in those three folders go to `Other` and are listed in the log. Hidden files, including macOS
  `._*` files, and camera index files are skipped.

Archives from an older version may have RAW files in `CameraImports`.
`tools/move_raw_to_separate_folder.sh` moves them, and previews the moves unless you pass `--apply`.

## The catalog and the card manifest

These make repeat imports fast. Both are caches: the files on disk are the source of truth, and a
missing or out-of-date cache just means hashing the file again.

- **The catalog** (`~/Library/Application Support/PhotoOffloaderData/catalog.sqlite3`) records known
  files, their fingerprints (size, modification time, inode), verified hashes, capture dates,
  camera models, Google Photos uploads and the run history. It lives outside the install folder, so
  it survives upgrades and uninstalling.
- **The card manifests** (`~/Library/Application Support/PhotoOffloaderData/card-manifests/`, one file
  per card, named by its volume ID) record the hashes of files already verified, so a re-inserted card
  doesn't have to be read in full again. They hold no media, and the 50 most recently used are kept.
  A card with no volume ID goes without one, and its files are hashed.
  Each also keeps the card's name, size and kind, and for an SD card in the built-in slot its
  serial number, read from macOS. They are what the dashboard's
  [cards row](using.md#cards-and-when-to-format) compares with the catalog to say whether a card is
  safe to format. `card_safety.json` beside them records when each card became safe, and
  `cards.json` the names you gave cards and the ones you said you formatted.

Photo Offloader never writes to a card. Older versions kept the manifest on the card, in
`.photo-offloader`; that folder is now ignored and left where it is, so such a card is hashed once more
on its next insert unless the catalog still knows its files. macOS itself adds a `.fseventsd` folder
when it mounts a card.

The card's folders are still listed on every insert, so new files are always found.

**After a format.** Both caches outlive a format. A new photo could have the same path, size and time
as an old one, so before a file is skipped on a cached hash, its first and last 64 KB are compared with
the saved copy. If they differ, both files are hashed in full, and a new photo gets saved as a
`__conflict-N` copy. The Nikon Z 8 gave an SD card a new volume ID when it was formatted, which makes
this rarer still. The comparison isn't proof that the files are identical; it only rules out a wrong
cache entry. The full check happens when a file is copied.

**An unchanged card isn't read again.** Cameras only add files. If every file in a card's manifest is
still on the card with the same path, size and time, the card can't have been formatted, so that
comparison is skipped and a re-insert reads no photos. New files are copied and verified as usual. If
a file is missing or changed, that import does the comparison, and files that have left the card drop
out of the manifest only once an import completes. This also means a saved copy that changed on disk
without its size or time changing goes unnoticed here; [`photo-offloader-verify`](using.md#checking-the-saved-copies)
is what finds that.

**Daylight saving and time zones.** Cameras store file times in local time, so after a clock change
every file on a card can appear to have moved by exactly an hour. A shift of a whole number of hours,
up to 24, with the same size counts as unchanged. Otherwise a single clock change would mean hashing
the whole card again.

## How the parts run

These are separate programs that share files:

1. **launchd** runs the trigger (`scripts/trigger.sh`) every 30 seconds and whenever `/Volumes` changes.
   It looks for volumes with `DCIM`. With two cards mounted, they import one after the other.
2. The trigger starts the **importer** as its own background job. The importer writes its progress to
   `status.json`.
3. After a successful import, the importer starts the **Google sync** in the background. One upload
   runs at a time. A sync started during one waits for it, then sends what's new; a third gives up,
   since the waiting one lists every file. While an upload is unfinished the sync leaves a marker
   (`.google-photos-retry`), and the trigger uses it to try again later. Pausing writes
   `.google-photos-paused`, which stops any sync from starting.
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
