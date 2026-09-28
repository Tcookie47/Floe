"""App Support and Logs directory helpers, overridable via FLOE_HOME.

Uses `platformdirs` so the same code resolves to the right per-user directory
on macOS, Linux and Windows (SPEC §15.2). On macOS this is the same
`~/Library/Application Support/Floe` / `~/Library/Logs/Floe` the v0.9 Qt app
used, so both versions share `profiles.json`.
"""

import os
from pathlib import Path

from platformdirs import user_data_dir, user_log_dir

_APP_NAME = "Floe"


def app_support_dir() -> Path:
    """Return the app support directory, creating no directories on import."""
    floe_home = os.environ.get("FLOE_HOME")
    if floe_home:
        return Path(floe_home) / "support"
    return Path(user_data_dir(_APP_NAME, appauthor=False))


def logs_dir() -> Path:
    """Return the logs directory, creating no directories on import."""
    floe_home = os.environ.get("FLOE_HOME")
    if floe_home:
        return Path(floe_home) / "logs"
    return Path(user_log_dir(_APP_NAME, appauthor=False))


def prefs_path() -> Path:
    """Return the path to the small JSON prefs file (SPEC §15.2)."""
    return app_support_dir() / "prefs.json"
