---
title: Google Drive for desktop stopped uploading to Google Photos
description: Since August 10, 2026, Google Drive for desktop no longer backs up folders to Google Photos. Here are the ways to get camera photos from a Mac into Google Photos now.
---

# Google Drive for desktop stopped uploading to Google Photos

If you used **Google Drive for desktop** to send photos from your Mac to **Google Photos**, that
stopped on **August 10, 2026**. Google stopped accepting new backup folders in the app on June 15,
and ended backups from existing folders on August 10. Photos already uploaded stay in Google
Photos; nothing was deleted. Backup from the Google Photos app on a phone isn't affected.

There is no Google Photos app for the Mac to replace it. This page lists the ways that are left,
for anyone who takes photos with a camera and copies them to a Mac.

## Your options

| Option | Runs by itself | Setup | Good for |
| --- | --- | --- | --- |
| [Google Photos website](#the-google-photos-website) | Only while the tab is open | None | An occasional upload |
| [Your phone](#through-your-phone) | Yes, once photos reach the phone | Camera's phone app | A few photos at a time |
| [rclone](#rclone-on-its-own) | If you schedule it | Command line and a Google Cloud client | People at home in the Terminal |
| [Photo Offloader](#photo-offloader) | Yes, when a card goes in | About 20 minutes, mostly in Google Cloud | Copying whole cards, with a verified backup |

### The Google Photos website

Google's suggested replacement is **Back up folders** on [photos.google.com](https://photos.google.com):
press **+** and choose **Back up folders**. It only uploads while that browser tab stays open,
so it can't upload in the background or start on its own after you copy a card. See
[Google's help page](https://support.google.com/drive/answer/13470231) for the details.

### Through your phone

Most cameras have a phone app, such as Nikon's SnapBridge, that can send photos to your phone,
where the Google Photos app backs them up. It needs nothing on the Mac, but transfers over
Bluetooth or Wi-Fi are slow and often send smaller copies, so it suits a few photos, not a full card.

### rclone on its own

[rclone](https://rclone.org/googlephotos/) is a free command-line tool that can upload to Google
Photos. You need your own Google Cloud sign-in for it (the [setup guide](google-photos.md) walks
through it), and then a command like this uploads a folder to an album:

```bash
rclone copy ~/Pictures/MyShoot gphotos:album/MyShoot
```

It doesn't watch for new photos, so you run it yourself or schedule it.

### Photo Offloader

Photo Offloader is a free, open-source Mac app that does the whole job when you insert a card:

1. It copies every photo and video to your Mac and checks each copy with SHA-256 before giving it
   its final name.
2. It mirrors the archive to a USB drive, if you use one.
3. It uploads JPGs, HEICs and videos to a Google Photos album through rclone. RAW files stay on
   your disks.

It never writes to or deletes anything from the card, and never overwrites a file. Google Photos
is optional, and the copies to the Mac and the USB drive work without it. When Google changes
something again, an upload can fail, but your verified copies on the Mac and the USB drive don't
depend on it.

Insert the card, walk away, and the photos are on your Mac, your backup drive and in Google
Photos when you come back.

- [Get started](../README.md): requirements and install
- [Google Photos setup](google-photos.md): the Google Cloud and rclone steps, with a command that
  checks each one

## Watch out for the 7-day sign-in

Both rclone and Photo Offloader need your own Google Cloud client. If you leave that client in
Google's **Testing** status, the sign-in expires after 7 days and uploads stop without much warning.
**Publish** it, as [step 2 of the setup guide](google-photos.md#2-create-your-google-cloud-client)
describes. Publishing doesn't make anything public, and a client for your own use doesn't need
Google's review.

## Keep a copy that isn't in the cloud

Google Photos is a good place to see and share photos, but it shouldn't be your only copy. Keep
the originals, RAW files included, on your Mac and on a second drive, and check them before you
format a card.
