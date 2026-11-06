# Google Photos setup

Photo Offloader can upload your JPGs, HEICs and videos to Google Photos after each import. This
is optional, and imports and USB backups work without it. Setup takes about 20 minutes, and
most of that is in the Google Cloud console.

Photo Offloader uploads through [rclone](https://rclone.org/googlephotos/), so **you need your
own Google Cloud OAuth client**. rclone's shared client is being retired and stops working
during 2026. The client is free, and only your own account uses it.

If you get stuck at any point, this command shows which step is missing and what to do next:

```bash
~/Library/Application\ Support/PhotoOffloader/bin/photo-offloader-google-sync --check
```

Google changes its console from time to time, so labels may differ slightly from the ones
below. These steps were checked against the rclone and Google documentation in November 2026.

## 1. Install rclone

```bash
brew install rclone
```

`rclone version` should now print a version. Use v1.70 or newer, because older versions no
longer work with Google's API.

## 2. Create your Google Cloud client

Open the [Google Cloud console](https://console.cloud.google.com/) and sign in. Any Google
account can own the project; it doesn't have to be the one holding your photos.

1. **Create a project.** Use the project picker at the top, then choose **New project**. Any
   name will do, such as `Photo Offloader`.
2. **Enable the API.** Go to **APIs & Services › Library**, search for **Photos Library API**,
   open it and press **Enable**.
3. **Set up the consent screen.** Go to **APIs & Services › OAuth consent screen** and press
   **Get started**. Enter an app name and your email, choose **External** as the audience, and
   finish.
4. **Add the scopes.** Under **Data Access**, press **Add or remove scopes** and paste these
   three into the manual entry box, then save:

   ```text
   https://www.googleapis.com/auth/photoslibrary.appendonly
   https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata
   https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata
   ```

5. **Create the client.** Under **Clients**, press **Create client** and choose **Desktop app**.
   Google then shows a **client ID** and **client secret**. Keep this page open for step 3.
6. **Publish the app: don't skip this.** Under **Audience**, press **Publish app** and confirm,
   so the status reads **In production**.

> **Why publishing matters.** Google [issues sign-ins that expire after 7
> days](https://developers.google.com/identity/protocols/oauth2#expiration) to an External app
> whose status is *Testing*. Setup appears to work, then uploads stop a week later.

Publishing does not make anything public, and you don't need Google's review to use your own
app. [rclone's guide](https://rclone.org/drive/#making-your-own-client-id) notes that apps for
personal use don't need verification. You will see an "unverified app" warning once, in the
next step.

If **Publish app** is greyed out, fill in the app home page and privacy policy links under
**Branding**, then try again.

## 3. Connect rclone to your account

Run `rclone config`. These are the prompts, and what to answer:

| rclone asks | Answer |
| --- | --- |
| `n) New remote` … | `n` |
| `Enter name for new remote.` | `gphotos` |
| `Type of storage to configure.` | `google photos` |
| `OAuth Client Id.` | your client ID |
| `OAuth Client Secret.` | your client secret |
| `Set to make the Google Photos backend read only.` | press Enter, to keep `false` (uploads need this) |
| `Edit advanced config?` | `n` |
| `Use web browser to automatically authenticate rclone with remote?` | `y` |

Your browser then opens:

1. Choose the Google account that holds your photos.
2. At the "Google hasn't verified this app" warning, choose **Advanced**, then **Go to
   *your app name* (unsafe)**. This is your own app.
3. Allow all the permissions it lists.

Back in the terminal, confirm the remote with `y`, then press `q` to quit.

rclone keeps the sign-in in its own config file. Photo Offloader never stores your credentials.

## 4. Test the connection

```bash
rclone lsd gphotos:album
```

On a new setup this prints nothing and no error, because rclone can only see albums it created.
Then run the setup check:

```text
$ ~/Library/Application\ Support/PhotoOffloader/bin/photo-offloader-google-sync --check
Google Photos setup check

  ✓ rclone is installed
  ✓ rclone has a remote named 'gphotos'
  ✓ Signed in to Google
  ✓ Album 'Photo Offloader'
      Not created yet; the first sync creates it.

All set. Press Sync now in the dashboard to upload.
```

The check only reads. It creates no album and uploads nothing, so you can run it at any time.

## 5. Upload

In the dashboard, press **Sync now** on the Google Photos card, or choose **⋯ › Sync Google
Photos now**. The panel shows progress, and the first run can take a long time if your archive
is large. After this, uploads start on their own after each successful import.

To see the result, open [Google Photos](https://photos.google.com) and go to **Albums** (see
[Albums](#albums) for what to expect there).

## What gets uploaded

- **Uploaded:** photos (JPG, JPEG, HEIC, HEIF) and video (MOV, MP4, AVI, MTS, M2TS, M4V, 3GP,
  3G2, ASF, DIVX, MKV, MPG, WMV) in `~/Pictures/CameraImports`.
- **Not uploaded:** RAW files, and anything else that isn't a listed photo or video type.
- **Only new files.** The catalog records each uploaded file, so a sync with nothing new doesn't
  contact Google.
- **One way.** Nothing is ever deleted from Google Photos, and nothing is downloaded from it.
- **Original quality.** Uploads count against your Google storage, because the API has no
  storage-saver mode.
- A failed upload never affects your verified local copies or USB backup.

## Albums

Your archive's folders become separate albums, each named with its whole path:

```text
Photo Offloader                          (stays empty)
Photo Offloader/2026-11/NIKON-Z-8/Photos
Photo Offloader/2026-11/NIKON-Z-8/Movies
```

Google's API sets these limits, as listed in
[rclone's documentation](https://rclone.org/googlephotos/#limitations):

- rclone can only add to albums it created, so you can't aim uploads at an album you made by hand.
- rclone can only list and download what it uploaded, not the rest of your library.
- Neither rclone nor Photo Offloader can delete an album; you do that in Google Photos.
- rclone only checks that a file with that name is in the album, not what it contains, so an
  edited file that keeps its name isn't replaced.
- A second upload of identical bytes keeps the first file's name.

## Changing the remote or album name

The defaults are remote `gphotos` and album `Photo Offloader`. Keep them unless you have a
reason to change them, because the setting must reach several background programs.
Use `PHOTO_OFFLOADER_GPHOTOS_REMOTE` and `PHOTO_OFFLOADER_GPHOTOS_ALBUM`, as described in
[Configuration](configuration.md#setting-environment-variables).

A new album name applies to new uploads only. Files already uploaded stay where they are and
aren't sent again.

## Troubleshooting

Run the [check command](#google-photos-setup) first. It recognises the problems below.

| You see | What it means | What to do |
| --- | --- | --- |
| **Not set up:** Install rclone to set this up | rclone isn't installed | Go to [step 1](#1-install-rclone) |
| **Not set up:** Add an rclone remote named gphotos | No remote with that name | Go to [step 3](#3-connect-rclone-to-your-account). The name must match exactly |
| **Upload failed:** The Google sign-in has expired | The sign-in expired or was revoked | Run `rclone config reconnect gphotos:` |
| The sign-in expires again about a week later | The app is still in *Testing* | Do step 2's [Publish the app](#2-create-your-google-cloud-client), then reconnect |
| **Upload failed:** Google does not accept the client ID and secret | The ID or secret is wrong, or the client was deleted | Run `rclone config`, edit `gphotos` and re-enter them |
| **Upload failed:** The Photos Library API is not enabled | Enabled in a different project, or not at all | Enable it in the project that owns your client, wait a few minutes |
| **Upload failed:** The sign-in is missing a Google Photos permission | Scopes weren't added, a permission was unticked at sign-in, or `read_only` is `true` | Check the [scopes](#2-create-your-google-cloud-client) and `read_only`, then reconnect and allow everything |
| **Could not reach Google Photos** from the check | Usually the network; rclone's message follows | Check your connection and try again |
| Uploads stopped after months without use | Google also expires sign-ins [unused for six months](https://developers.google.com/identity/protocols/oauth2#expiration) | Run `rclone config reconnect gphotos:` |

For anything else, read the upload log: **⋯ › View logs › Google** in the dashboard, or
`~/Library/Application Support/PhotoOffloader/google_photos_sync.log`.

After fixing a problem, press **Retry** on the Google Photos card. Files that didn't finish are
sent on the next sync.
