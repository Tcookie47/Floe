"""Locating bundled DuckDB extensions inside the frozen (PyInstaller) app (SPEC §11.3).

No Qt here. This module only inspects `sys.frozen` / `sys._MEIPASS` and the filesystem
next to the running executable; it never touches the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

EXTENSION_DIR_NAME = "duckdb_extensions"


def bundled_extension_dir() -> Path | None:
    """Return the bundled DuckDB extension directory when running frozen, else None.

    PyInstaller sets `sys.frozen = True` and, for a onedir/onefile build,
    `sys._MEIPASS` to the directory holding bundled data files (for a macOS `.app`
    BUNDLE this is inside `Contents/Resources` / `Contents/MacOS`). We look for a
    `duckdb_extensions` directory there and return it only if it actually exists,
    so callers can fall back to DuckDB's normal runtime download behavior.
    """
    if not getattr(sys, "frozen", False):
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    candidate = Path(meipass) / EXTENSION_DIR_NAME
    if candidate.is_dir():
        return candidate
    return None
