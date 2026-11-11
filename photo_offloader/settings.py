"""settings.txt: the archive folders and backup drive, changeable without touching the code.

The file lives in the data folder, so reinstalling or updating the code keeps it. It is plain
``name = value`` lines with ``#`` comments, so it can be edited in TextEdit (whose smart quotes
would break JSON) and read by ``scripts/trigger.sh`` without Python. With no file, the defaults
apply. A file with a problem is never half applied: every value falls back to its default and
``problem`` says what's wrong, so the programs that copy files can refuse to run.

This module imports nothing from the project, so ``config`` can use it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, NamedTuple, Optional

FILE_NAME = "settings.txt"
VOLUMES_ROOT = Path("/Volumes")
ARCHIVE = "archive_folder"
RAW_ARCHIVE = "raw_archive_folder"
BACKUP_DRIVE = "backup_drive"
KNOWN = (ARCHIVE, RAW_ARCHIVE, BACKUP_DRIVE)
QUOTES = {'"': '"', "'": "'", "“": "”", "‘": "’"}


class Settings(NamedTuple):
    archive_folder: Path
    raw_archive_folder: Path
    backup_drive: str
    problem: Optional[str] = None

    @property
    def usb_volume(self) -> Path:
        return VOLUMES_ROOT / self.backup_drive


class SettingsProblem(RuntimeError):
    """Raised by a program that won't copy or check files while settings.txt has a problem."""

    def __init__(self, problem: str) -> None:
        super().__init__(
            f"Photo Offloader's settings file has a problem, so nothing was done. {problem} "
            "Fix it with Edit settings… in the dashboard's ⋯ menu."
        )


def defaults(home: Path) -> Settings:
    pictures = home / "Pictures"
    return Settings(pictures / "CameraImports", pictures / "CameraImportsRaw", "Photos")


def load(path: Path, home: Path) -> Settings:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return defaults(home)
    except (OSError, UnicodeDecodeError) as exc:
        return defaults(home)._replace(problem=f"{path.name} can't be read: {exc}.")
    return parse(text, home)


def parse(text: str, home: Path) -> Settings:
    found: Dict[str, str] = {}
    lines: Dict[str, int] = {}  # the line each setting is on, for a problem found later
    for number, line in enumerate(text.splitlines(), start=1):
        problem = _read_line(line, found)
        if problem:
            return defaults(home)._replace(problem=f"Line {number}: {problem}")
        lines.update((name, number) for name in found if name not in lines)
    base = defaults(home)
    values = Settings(
        _folder(found.get(ARCHIVE), home) or base.archive_folder,
        _folder(found.get(RAW_ARCHIVE), home) or base.raw_archive_folder,
        found.get(BACKUP_DRIVE, base.backup_drive),
    )
    problem = _check(values, found, lines)
    return base._replace(problem=problem) if problem else values


def _read_line(line: str, found: Dict[str, str]) -> Optional[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    name, equals, value = stripped.partition("=")
    name, value = name.strip(), _unquote(value.strip())
    if not equals:
        return f"expected a setting such as {BACKUP_DRIVE} = Photos, found “{stripped}”."
    if name not in KNOWN:
        return f"there's no setting called “{name}”. The settings are {', '.join(KNOWN)}."
    if name in found:
        return f"{name} is set twice."
    if not value:
        return f"{name} has no value after the =."
    found[name] = value
    return None


def _unquote(value: str) -> str:
    if len(value) >= 2 and QUOTES.get(value[0]) == value[-1]:
        return value[1:-1].strip()
    return value


def _folder(value: Optional[str], home: Path) -> Optional[Path]:
    if value is None:
        return None
    if value == "~" or value.startswith("~/"):
        value = str(home) + value[1:]
    return Path(os.path.normpath(value)) if value.startswith("/") else Path(value)


def _check(values: Settings, found: Dict[str, str], lines: Dict[str, int]) -> Optional[str]:
    for name, folder in (
        (ARCHIVE, values.archive_folder),
        (RAW_ARCHIVE, values.raw_archive_folder),
    ):
        if not folder.is_absolute():
            return (
                f"Line {lines[name]}: {name} must be a full path such as "
                f"~/Pictures/CameraImports, not “{found[name]}”."
            )
    if "/" in values.backup_drive or values.backup_drive in (".", ".."):
        return (
            f"Line {lines[BACKUP_DRIVE]}: {BACKUP_DRIVE} is the drive's name as Finder shows it, "
            f"such as Photos, not a path (“{values.backup_drive}”)."
        )
    archive, raw = values.archive_folder, values.raw_archive_folder
    if archive == raw:
        return f"{ARCHIVE} and {RAW_ARCHIVE} must be different folders."
    if archive in raw.parents or raw in archive.parents:
        return f"{ARCHIVE} and {RAW_ARCHIVE} can't be inside one another."
    return None


def template(values: Settings, home: Path) -> str:
    """The file Edit settings… creates: these values, with what each one means."""
    return (
        "# Photo Offloader settings\n"
        "#\n"
        "# Change a value after the =, then save. The next import uses it; the dashboard and\n"
        "# the menu bar pick it up within a few seconds. Lines starting with # are notes.\n"
        "# More: https://devturner.github.io/photo_offloader/docs/configuration.html\n"
        "#\n"
        "# Changing a folder doesn't move what's already in the old one. The next import\n"
        "# copies what's still on the card into the new folder. To keep one archive, stop\n"
        "# Photo Offloader, move the folder in Finder, then change the setting here.\n"
        "\n"
        "# Where photos, videos and other files go.\n"
        f"{ARCHIVE} = {_shown(values.archive_folder, home)}\n"
        "\n"
        "# Where camera RAW files go.\n"
        f"{RAW_ARCHIVE} = {_shown(values.raw_archive_folder, home)}\n"
        "\n"
        "# The USB backup drive's name, as Finder shows it. Backups go in its CameraImports folder.\n"
        f"{BACKUP_DRIVE} = {values.backup_drive}\n"
    )


def _shown(folder: Path, home: Path) -> str:
    try:
        return "~/" + str(folder.relative_to(home))
    except ValueError:
        return str(folder)


def create_if_missing(path: Path, values: Settings, home: Path) -> bool:
    """Write the template for ``values`` unless the file exists. True if it was written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(template(values, home))
    except FileExistsError:
        return False
    return True
