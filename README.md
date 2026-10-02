# 📸 Photo Offloader Automation Suite

A macOS-focused camera media offloader that safely copies photos and video from camera cards to local storage and, when available, an external backup drive.

The importer is designed around a simple rule:

> **Copy → Verify → Report → Eject**

Files are never overwritten, incomplete temporary copies are cleaned up, and a camera card is only automatically ejected after the required copies have completed successfully.

---

## ✨ Features

### 📷 Camera Card Detection

The importer can automatically recognize configured camera volumes and locate their `DCIM` folders.

Currently configured volume patterns include:

- `NIKON*`
- `SONY*`
- `EOS*`

A camera volume or `DCIM` directory can also be supplied manually.

### 💾 Local + External Backup

Every import is copied to local storage:

```text
~/Pictures/CameraImports/
```

If the configured external drive is mounted at:

```text
/Volumes/Photos/
```

the importer also creates a second copy under:

```text
/Volumes/Photos/CameraImports/
```

The external destination is used automatically when available.

If the external drive is disconnected during an import, the session is treated as incomplete and the camera card is **not automatically ejected**.

Use `--no-usb` when an external backup is intentionally not required for a particular run.

### 🛡️ Safe Collision Handling

Existing files are never blindly overwritten.

If a destination already contains a file with the same name:

- If the existing file matches the source, it is safely skipped.
- If the existing file is different, the importer preserves both files by creating a conflict filename such as:

```text
DSC_1234__conflict-1.NEF
```

This prevents accidental destruction of previously imported media.

### 🔐 Copy Verification

Every copied file is verified after the copy completes.

By default, verification compares the source and destination file sizes.

For stronger verification, use:

```bash
python3 camera_offloader_v2.py --sha256
```

This calculates SHA-256 hashes for the source and destination files.

SHA-256 verification provides stronger data-integrity assurance but requires additional disk I/O.

### 📁 Organized Archive Structure

Files are organized into `YYYY-MM` folders.

For example:

```text
CameraImports/
└── 2026-10/
    ├── 100NIKON/
    │   ├── DSC_0001.NEF
    │   └── DSC_0002.JPG
    └── 101NIKON/
        └── DSC_0003.NEF
```

The camera's native DCIM subdirectories are preserved by default.

Supported media includes common:

- JPEG / JPG
- HEIC / HEIF
- NEF
- ARW
- CR2
- CR3
- DNG
- RAF
- ORF
- RW2
- MOV
- MP4
- AVI
- MTS
- M2TS

### 📊 Progress & Session Reporting

The importer provides a live progress display while files are being processed.

At the end of the session it reports:

- files found
- files copied
- files skipped
- files verified
- failures
- destination paths
- eject status

### 📝 Persistent Logging

Import activity is written to:

```text
~/.camera_transfer.log
```

Use `--verbose` to display more detailed logging in the terminal.

### 🔔 macOS Notifications

The importer uses macOS's native `osascript` notification mechanism to report:

- successful imports
- incomplete imports
- fatal errors
- canceled operations

Notification failure does not itself cause the import to fail.

### ⏏️ Safe Card Ejection

By default, the camera card is automatically ejected only after the import has completed successfully.

If any required copy fails, the card is left mounted so the problem can be investigated.

Disable automatic ejection with:

```bash
--no-eject
```

---

## 📋 System Requirements

### Operating System

- macOS
- Python **3.9 or newer**

The importer relies on macOS-native utilities including:

- `diskutil`
- `osascript`

### Python Dependency

The importer uses:

```text
alive-progress
Pillow
```

Install it with:

```bash
python3 -m pip install -r requirements.txt
```

---

## 🚀 Installation

Clone or download the project repository to your Mac.

Open Terminal and change into the project directory:

```bash
cd /path/to/PhotoOffloader
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The script will still run without `alive-progress` installed, but the progress bar will be disabled and a warning will be logged instead.

### Linting

This project includes Ruff for local linting and import-order checks:

```bash
source .venv/bin/activate
python -m ruff check camera_offloader_v2.py test_camera_offloader.py --output-format concise
```

The repo also includes a Ruff config file so you can use the same command in CI or before a PR.

### Common usage

```bash
source .venv/bin/activate
python camera_offloader_v2.py --source /Volumes/NIKON/DCIM
python camera_offloader_v2.py --source /Volumes/SONY/DCIM --sha256 --verbose
python camera_offloader_v2.py --no-usb --no-eject
```

If you do not pass `--source`, the program will scan for recognized camera cards and prompt you to choose one.

> **Note:** This project is designed for macOS and uses `diskutil` and `osascript` from the OS.

---

## ▶️ Running the Importer

The normal interactive workflow is:

```bash
python3 camera_offloader_v2.py
```

The program will ask you to select the source camera card when necessary.

You can also provide the camera volume or `DCIM` directory directly:

```bash
python3 camera_offloader_v2.py --source /Volumes/NIKON1234
```

or:

```bash
python3 camera_offloader_v2.py --source /Volumes/NIKON1234/DCIM
```

---

## ⚙️ Command-Line Options

### Skip External USB Backup

```bash
python3 camera_offloader_v2.py --no-usb
```

Copies only to the local destination, even if `/Volumes/Photos` is mounted.

### Disable Automatic Ejection

```bash
python3 camera_offloader_v2.py --no-eject
```

Leaves the camera card mounted after the import.

### Enable SHA-256 Verification

```bash
python3 camera_offloader_v2.py --sha256
```

Performs stronger content verification after copying.

### Verbose Logging

```bash
python3 camera_offloader_v2.py --verbose
```

Displays detailed diagnostic information in the terminal.

### Combine Options

For example:

```bash
python3 camera_offloader_v2.py --sha256 --verbose --no-eject
```

---

## ⚙️ Configuration

The primary configuration values are located near the top of:

```text
camera_offloader_v2.py
```

### Camera Volume Detection

Edit:

```python
CARD_VOLUME_GLOBS = [
    "/Volumes/NIKON*/DCIM",
    "/Volumes/SONY*/DCIM",
    "/Volumes/EOS*/DCIM",
]
```

Add additional camera volume patterns as required.

### Local Destination

Default:

```python
LOCAL_DEST_ROOT = Path.home() / "Pictures" / "CameraImports"
```

### External Backup Drive

Default:

```python
USB_DEST_VOLUME = Path("/Volumes/Photos")
USB_DEST_ROOT = USB_DEST_VOLUME / "CameraImports"
```

The external drive should therefore be mounted with the volume name:

```text
Photos
```

### Import Retry Period

If a newly mounted card does not immediately expose its media, the importer waits and retries:

```python
WAIT_SECONDS = 30
```

### Automatic Ejection

Automatic ejection is enabled by default:

```python
EJECT_CARD_AFTER_SUCCESS = True
```

It can also be disabled for an individual run with:

```bash
--no-eject
```

### Preserving Camera Folders

Camera DCIM subdirectories are preserved by default:

```python
PRESERVE_CAMERA_SUBFOLDERS = True
```

This allows structures such as:

```text
100NIKON/
100MSDCF/
101CANON/
```

to remain distinguishable in the archive.

---

## 📂 Destination Layout

The default archive layout is:

```text
CameraImports/
└── YYYY-MM/
    └── CAMERA_FOLDER/
        └── filename
```

For example:

```text
~/Pictures/CameraImports/
└── 2026-10/
    └── 100NIKON/
        ├── DSC_1001.NEF
        ├── DSC_1001.JPG
        └── DSC_1002.NEF
```

When the external drive is mounted, the same archive structure is created under:

```text
/Volumes/Photos/CameraImports/
```

---

## 🔒 Safety Model

The importer is intentionally conservative.

### It does not:

- delete files from the camera card
- overwrite existing destination files
- consider a partial temporary copy to be a completed import
- automatically eject the card after a failed import

### It does:

1. Scan the camera card.
2. Identify supported media.
3. Create destination directories.
4. Copy files through temporary files.
5. Verify the completed copy.
6. Install the verified file at its final destination.
7. Record the result.
8. Eject the camera card only after a successful session.

The camera card remains the original source throughout the process.

---

## 🧪 Recommended First Test

Before using the importer with an important card, test with a card containing a small number of sample files.

First run:

```bash
python3 camera_offloader_v2.py --no-eject --verbose
```

Then confirm:

1. The expected camera files were detected.
2. Files appear in the local `CameraImports` directory.
3. The external backup contains the expected files when `/Volumes/Photos` is mounted.
4. The files open correctly.
5. The session log contains no errors.

Once the workflow has been confirmed, normal operation can use:

```bash
python3 camera_offloader_v2.py
```

---

## 🧹 Uninstallation

To remove the installed background service and bundled app files, run:

```bash
./uninstall.sh
```

This removes:

- the LaunchAgent installed in `~/Library/LaunchAgents/com.user.photooffloader.plist`
- the application files under `~/Library/Application Support/PhotoOffloader`
- the local log file at `~/.camera_transfer.log`

It does not delete the imported media archive under `~/Pictures/CameraImports`.

> Review the script before running it if you have customized the install location or launch configuration.

---

## 🛠️ Troubleshooting

### No camera card detected

Confirm the card is mounted in Finder and that it contains a `DCIM` directory.

You can bypass automatic detection:

```bash
python3 camera_offloader_v2.py --source /Volumes/YourCard
```

### External drive is not being used

Confirm that the drive is mounted as:

```text
/Volumes/Photos
```

Then run:

```bash
ls /Volumes/Photos
```

If the drive has a different volume name, update:

```python
USB_DEST_VOLUME
```

in the configuration.

### Import reports failures

Check the terminal output and:

```text
~/.camera_transfer.log
```

Do not eject or remove the camera card until the source files and backup status have been confirmed.

### Existing files are being skipped

This is expected when the destination file matches the source.

The importer does not overwrite an existing matching file.

If an existing file has the same filename but different contents, the importer creates a conflict filename instead.

### Need maximum verification

Run:

```bash
python3 camera_offloader_v2.py --sha256
```

This performs SHA-256 verification rather than relying only on file size.

---

## 📌 Archive date behavior

The importer prefers the camera's EXIF capture date when it is available, then falls back to the file's filesystem timestamp.

In practice, the `YYYY-MM` folder is created from:

1. EXIF DateTimeOriginal / DateTimeDigitized / DateTime when present
2. macOS filesystem birth time (`st_birthtime`) when available
3. file modification time (`st_mtime`) as the final fallback

This keeps imports aligned with the actual shooting date when the camera metadata is present, while still working for files without EXIF data.

---

## 📄 License

This project is licensed under the MIT License.

```text
MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```