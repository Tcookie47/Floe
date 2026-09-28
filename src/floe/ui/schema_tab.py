"""Schema tab (SPEC §8.1): column name, type, nullable."""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd
from PySide6.QtWidgets import QWidget

from floe.core.context import ColumnInfo, TableInfo
from floe.ui.preview_tab import ResultsPane
from floe.ui.workers import user_error_text

SCHEMA_COLUMNS = ["column", "type", "nullable"]


def schema_dataframe(columns: Sequence[ColumnInfo]) -> pd.DataFrame:
    return pd.DataFrame(
        [(c.name, c.type, "YES" if c.nullable else "NO") for c in columns],
        columns=SCHEMA_COLUMNS,
    )


class SchemaTab(ResultsPane):
    """Column list of the selected table (runs in a worker owned by the main window)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._table: TableInfo | None = None
        self.show_text("Select a table to see its columns.")

    @property
    def table(self) -> TableInfo | None:
        return self._table

    def clear(self, text: str = "Select a table to see its columns.") -> None:
        self._table = None
        self.show_text(text)

    def show_loading(self, table: TableInfo) -> None:
        self._table = table
        self.show_text(f"Loading columns of {table.view_name}…", "loading")

    def show_columns(self, table: TableInfo, columns: Sequence[ColumnInfo]) -> None:
        self._table = table
        self.show_dataframe(schema_dataframe(columns))

    def show_error(self, table: TableInfo, exc: BaseException) -> None:
        self._table = table
        self.show_text(user_error_text(exc), "error")
