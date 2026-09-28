"""Floe: browse and query Iceberg tables via Nessie and DuckDB."""

import subprocess
import sys


def _fallback_commit() -> str:
    """Short git SHA of HEAD, or "unknown" if git isn't available (e.g. in a release)."""
    if getattr(sys, "frozen", False):
        # A frozen app has no checkout, and on macOS running `git` without the Command
        # Line Tools pops up an "install developer tools" dialog.
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


try:
    from floe._build_info import __commit__, __version__  # type: ignore[import-not-found]
except ImportError:
    __version__ = "0.0.0.dev0"  # PEP 440 (hatchling reads it for `pip install git+…`)
    __commit__ = _fallback_commit()

__all__ = ["__commit__", "__version__"]
