# Using Photo Offloader

Insert a card. Within about 30 seconds the dashboard opens and the import starts. Any volume
with a top-level `DCIM` folder counts as a camera card, whatever its name or make.

## The dashboard

Open it from the 📷 menu bar item with **Open Dashboard**. It follows macOS light or dark mode
unless you choose otherwise under **⋯ › Appearance**. It runs only on your Mac, in its own window
or in a browser tab if the native window isn't available.

### Import steps

While an import runs, the top panel shows these steps with progress, speed, file count, elapsed
and remaining time. A value not known yet shows a dash. The graph shows the speed over the run, up to the
latest 90 seconds. It starts when data first moves, and a stall shows as a dip to zero.
While there's nothing to measure yet, such as during a scan or while the upload gets ready, the progress bar
glows slowly.

When a run ends, the panel shows its report: what each step did and how long it took. Choose how it looks
under **Import report** in the ⋯ menu. **Basic**, the default, shows where the time went and what reached
the Mac, USB drive and Google Photos. **Advanced** is a table with a row for each step. Speeds cover that step
alone, and Upload time includes the time rclone spends getting ready.

During an import, **Files** reads like `737 of 1,278 new`: 737 of the card's 1,278 new files have been
copied. Progress and Remaining measure those new files too, and the Camera card box shows the card as a
whole, such as `1,278 new files of 2,478`. Files already saved are checked but take no copying time.

The number of new files is an estimate from the card's own record, and each file is still checked when it's
reached, so the total can change slightly. On a card's first import, or a read-only card, there's nothing to
estimate from: **Files** just counts up (`737 new`), and Progress measures the whole card.

| Step | What is happening |
| --- | --- |
| **Scan card** | Listing the files on the card |
| **Read photo info** | Reading capture dates and camera models, to choose archive folders |
| **Copy new files** | Copying files not yet in the archive, and verifying each copy |
| **Back up to USB** | Copying missing files from the Mac to the USB drive |
| **Upload to Google Photos** | Sending new JPGs and video, in the background |

When the panel says *Finishing import*, the copies are done and the app is recording the result.
The USB and Google steps are skipped if the drive isn't connected or Google Photos isn't set up.

### The four cards

| Card | Shows | Buttons, when they apply |
| --- | --- | --- |
| **Camera card** | The mounted card, or *No card* | **Import now**, **Eject** |
| **Mac archive** | How many files are indexed | None |
| **USB backup** | Whether the drive is connected, and files backed up | **Back up now**, **Eject** |
| **Google Photos** | Setup and upload state | **Sync now**, **Retry**, **Resume** |

Buttons that use a card or drive are hidden while a transfer is running. *Not set up* on the
Google Photos card names the first missing step: see the [setup guide](google-photos.md).

### Messages you may see

| Message | Meaning |
| --- | --- |
| *Ready* | Nothing is happening. Insert a card |
| *NIKON Z 8 is mounted* | A card was found, and the import will start in a moment |
| *NIKON Z 8 was already imported* | This card was imported since it was inserted. See [Re-inserting a card](#re-inserting-a-card) |
| *Automatic imports are paused* | Cards won't import until you resume |
| *Photo Offloader is stopped* | The service is off. Press **Start** |
| *The last transfer was interrupted* | The process died mid-way. Insert the card again; finished files aren't copied twice |
| *Google Photos upload needs attention* | The upload failed. The detail line says why: see [troubleshooting](google-photos.md#troubleshooting) |

A finished import's result fades after 10 minutes. A failed one stays until you dismiss it with
**×**.

### When an import fails

The panel says what to do, usually `Don't format this card`, and the run opens by itself in Recent
activity. It shows how many files are safe on the Mac, then a line saying whether any photo lost both its
RAW and its JPG, matched by name. A RAW or JPG shot alone counts like a movie: the line says it wasn't backed up.

Below that is one row per file type, with what wasn't saved, what's safe and what's on the card. Click a
row to see its files, and why they failed. A row with more than 30 files names the first 20 and offers
**Open these in a list**. If a folder couldn't be read, its contents are unknown, so nothing is counted
for it. If at least the last 20 files all failed to read, the report says so first, because that looks like
the card or the reader, not the photos.

Before any of this, a file that failed to read gets a second try: one pass at the end of the copy, after 5
seconds, which stops after 10 failures in a row. A file saved this way goes through the same verified copy, and
the full list marks it.

Until a retry succeeds, the Camera card box says `Don't format: files not saved`, even if you dismiss the
panel, and **Eject** asks first. The warning follows the card by its volume ID, because cameras give every card
the same name: it returns if that card comes back and isn't imported again, such as while imports are paused,
and another card with the same name doesn't get it. A card with no ID is matched by name.

- **Try again** imports the card again. Files already saved are skipped.
- **Open full list** opens every file on the card in your spreadsheet app, with its result and hash.
- **Save debug package…** saves a zip to Downloads, for checking a run yourself or sending to someone
  who can help. It holds a summary, every card file's result, the run's log, the card's `diskutil info`
  and the end of the card detection and upload logs. It never holds photos, your Google sign-in or the
  dashboard's access token. File names and paths are included and show your Mac username, so look
  through it before you share it.

A USB backup that fails gets the same report, whether it ran after an import or on its own, with what's on the
drive and what isn't. Its files are still on the Mac, so the card gets no warning, and **Back up again** replaces
**Try again**. It says so if the Mac's own copy couldn't be read, because then that copy isn't safe either.

Runs that finish cleanly offer the package too. An import where some files needed a second try says so when you
open it, because that can be an early sign of a failing card or reader. The files behind these reports are in `run-reports`, in the
data folder: the latest 20 runs with problems and the latest 20 without, so clean runs can't push out a
failure's report. Saving one never changes an import's result.

### Recent activity

This lists past imports, USB backups and catalog updates, with what each one copied. The history
survives restarts and reinstalls, and the latest 500 runs are kept. **Clear** removes it, after
you click a second time to confirm. It only deletes the history, not your files.

Click a run to unfold its report under it, pushing the runs below down and dimming the others. Click it again
to fold it away. Runs from before this version show a dash for anything that wasn't saved then.

### The ⋯ menu

- **Pause / Resume automatic imports:** see [below](#pause-stop-and-start).
- **Update file catalog:** rescans the archives. It normally updates itself, so you rarely need this.
- **Back up to USB now**, **Sync Google Photos now:** run those steps without a card.
- **Eject card after import:** off by default, so a card stays mounted for Finder.
- **Show Terminal during imports:** off by default. When on, the import runs in a terminal window.
- **Appearance:** Auto, Light or Dark.
- **Import report:** Basic or Advanced, for how a finished run's report looks.
- **View logs:** opens a log in Console, where it updates live. **Import** is what each import
  did, **Cards** is card detection, **Google** is uploads, and **Dashboard** is what the dashboard
  showed and why.
- **Stop / Start Photo Offloader**.

## The menu bar

The 📷 changes to show the state: `↻` during a transfer, `⏸` when paused, `Off` when stopped. Its
menu has **Open Dashboard**, **Copy to USB Drive** (when a drive is connected), **Pause / Resume
Automatic Imports**, **View Import Log**, **Stop Photo Offloader…** and **Quit Menu Bar**. Quitting
the menu bar leaves the service running.

## Pause, stop and start

**Pause** before inserting a card you don't want imported. It doesn't interrupt a copy already
running. **Resume** re-checks any card that is mounted.

**Stop** turns off card detection. It refuses to stop during a transfer unless you choose to.
Stopped, the service comes back at your next login. From a terminal:

```bash
SERVICE=~/Library/Application\ Support/PhotoOffloader/scripts/service.sh

"$SERVICE" status
"$SERVICE" stop             # refuses while a transfer is running
"$SERVICE" stop --wait      # finish the current transfer, then stop
"$SERVICE" stop --force     # cancel the transfer and stop now
"$SERVICE" stop --disable   # also stay off at next login
"$SERVICE" start
```

## Ejecting

Press **Eject** on the Camera card or USB backup card. Once the copy to the Mac is verified, it is
safe to remove the card, even if the USB backup is still running. A failed USB backup doesn't
affect the import: reconnect the drive and press **Back up now**.

## Re-inserting a card

Cards are recognised by their volume ID, so a second card from the same camera counts as a new
card even though it has the same name.

If you take a card out, shoot more, and put it back after more than 20 seconds, a new import
copies only the new files. Shorter gaps are treated as the card briefly disconnecting. If a card
stayed mounted and you want to import it again, press **Import now**.

## Running an import by hand

```bash
IMPORT=~/Library/Application\ Support/PhotoOffloader/bin/photo-offloader-import

"$IMPORT" --source "/Volumes/NIKON Z 8"             # import a card
"$IMPORT" --source "/Volumes/NIKON Z 8" --no-usb    # skip the USB backup
"$IMPORT" --source "/Volumes/NIKON Z 8" --no-eject  # leave the card mounted
"$IMPORT" --backup-usb                              # back up to USB, no card needed
"$IMPORT" --update-catalog                          # rescan the archives
```
