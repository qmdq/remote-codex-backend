from __future__ import annotations

import os
from pathlib import Path


def normalize_path(raw: str | Path) -> Path:
    path = Path(os.path.abspath(os.path.expanduser(str(raw))))
    if os.path.exists(path):
        path = Path(os.path.realpath(path))
    if os.name == "nt":
        # Windows path comparison is case-insensitive; lowercasing the text is
        # sufficient for roots stored in configuration and SQLite UNIQUE keys.
        return Path(str(path).replace("/", "\\").lower())
    return path


def user_display_path(path: Path) -> str:
    return str(path)
