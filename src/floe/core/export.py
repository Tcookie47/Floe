"""CSV export core logic (SPEC §9): gated by the profile's `allow_export` flag.

No Qt here. The UI is responsible for the file dialog, the confirmation
prompt, and running `export_csv` in a worker thread; this module only ever
writes the CSV it's given, and refuses outright when the caller isn't
allowed to export. Logs the destination path and row count only — never any
row data.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from floe.core.errors import FloeError

log = logging.getLogger("floe.core.export")


class ExportNotAllowed(FloeError):
    """Raised when a CSV export is attempted on a profile with `allow_export=False`."""

    def user_message(self) -> str:
        return "Export is disabled for this profile (Edit profiles… → Safety)."


def export_csv(df: pd.DataFrame, destination: str | Path, *, allow_export: bool) -> int:
    """Write `df` to `destination` as UTF-8 CSV (no index). Returns the row count.

    Refuses with `ExportNotAllowed` unless `allow_export` is True — this is the guard
    SPEC §9 requires regardless of what the UI does or doesn't enable.
    """
    if not allow_export:
        raise ExportNotAllowed()
    destination = Path(destination)
    df.to_csv(destination, index=False, encoding="utf-8")
    row_count = len(df)
    log.info("Exported %d row(s) to %s", row_count, destination)
    return row_count
