#!/usr/bin/env python3
"""Print the Python that install.sh should build its venv from.

A venv made from inside another venv links to Homebrew's version folder
(.../Cellar/python@3.14/3.14.8_3/...), which a Homebrew cleanup deletes, and then nothing in
the app can start. .../opt/python@3.14 always points at the current version.
"""

from __future__ import annotations

import os
import re
import sys

_HOMEBREW_VERSION_FOLDER = re.compile(
    r"^(?P<prefix>.*)/Cellar/(?P<formula>[^/]+)/[^/]+/.*/(?P<name>python\d+(?:\.\d+)*)$"
)


def stable_interpreter(path: str) -> str:
    """``path`` with links followed, as the ``opt`` path when it's a Homebrew version folder."""
    real = os.path.realpath(path)
    match = _HOMEBREW_VERSION_FOLDER.match(real)
    if match:
        stable = f"{match['prefix']}/opt/{match['formula']}/bin/{match['name']}"
        if os.path.exists(stable):
            return stable
    return real


if __name__ == "__main__":
    print(stable_interpreter(getattr(sys, "_base_executable", sys.executable)))
