"""DataFrame-backed table model for the results grid (SPEC §8.5).

`DataFrameModel` shows a pandas DataFrame read-only: display strings, NULLs as a dimmed
"NULL", column names as headers, stable sorting (NULLs last) and TSV export of a
selection. Shared by the Preview and Schema tabs and (Phase 5) the SQL tab.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

import numpy as np
import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QColor, QFont

NULL_TEXT = "NULL"
NULL_COLOR = QColor(140, 140, 140)

_Index = QModelIndex | QPersistentModelIndex


def is_null(value: Any) -> bool:
    """True for None / NaN / NaT / pd.NA (scalars only; arrays and lists are never NULL)."""
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    if isinstance(value, np.generic):
        try:
            return bool(pd.isna(value))
        except (TypeError, ValueError):
            return False
    return False


def format_value(value: Any) -> str:
    """Display text for one cell (NULL → "NULL")."""
    if is_null(value):
        return NULL_TEXT
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    if isinstance(value, np.ndarray):
        return str(value.tolist())
    return str(value)


def _tsv_cell(value: Any) -> str:
    """TSV text for one cell: NULL → empty; tabs and newlines → spaces."""
    if is_null(value):
        return ""
    text = format_value(value)
    return text.replace("\t", " ").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _stable_order(series: pd.Series, ascending: bool) -> np.ndarray:
    """Positions that sort `series` stably, NULLs last (falls back to text order)."""
    s = series.reset_index(drop=True)
    try:
        ordered = s.sort_values(kind="mergesort", ascending=ascending, na_position="last")
    except (TypeError, ValueError):
        keys = pd.Series(
            [None if is_null(v) else format_value(v) for v in s], dtype="object"
        )
        ordered = keys.sort_values(kind="mergesort", ascending=ascending, na_position="last")
    return ordered.index.to_numpy()


def _is_numeric(dtype: Any) -> bool:
    try:
        return pd.api.types.is_numeric_dtype(dtype) and not pd.api.types.is_bool_dtype(dtype)
    except TypeError:
        return False


class DataFrameModel(QAbstractTableModel):
    """Read-only Qt model over a DataFrame. `sort()` reorders a copy; the original is kept."""

    def __init__(self, df: pd.DataFrame | None = None, parent: Any = None) -> None:
        super().__init__(parent)
        self._original = pd.DataFrame()
        self._df = self._original
        self._numeric: list[bool] = []
        self._sort: tuple[int, Qt.SortOrder] | None = None
        if df is not None:
            self.set_dataframe(df)

    # ----- data ------------------------------------------------------------
    @property
    def dataframe(self) -> pd.DataFrame:
        """The DataFrame in its current (possibly sorted) order."""
        return self._df

    def set_dataframe(self, df: pd.DataFrame | None) -> None:
        self.beginResetModel()
        self._original = (df if df is not None else pd.DataFrame()).reset_index(drop=True)
        self._df = self._original
        self._numeric = [_is_numeric(t) for t in self._original.dtypes]
        self._sort = None
        self.endResetModel()

    def clear(self) -> None:
        self.set_dataframe(None)

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._df)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._df.columns)

    def value(self, row: int, column: int) -> Any:
        return self._df.iat[row, column]

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            text = format_value(self.value(row, col))
            if role == Qt.ItemDataRole.ToolTipRole and len(text) < 60:
                return None
            return text
        if role == Qt.ItemDataRole.ForegroundRole:
            return NULL_COLOR if is_null(self.value(row, col)) else None
        if role == Qt.ItemDataRole.FontRole:
            if is_null(self.value(row, col)):
                font = QFont()
                font.setItalic(True)
                return font
            return None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col < len(self._numeric) and self._numeric[col]:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def headerData(  # noqa: N802
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(self._df.columns):
                return str(self._df.columns[section])
            return None
        return str(section + 1)

    def flags(self, index: _Index) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ----- sorting ---------------------------------------------------------
    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Stable sort by `column` (NULLs last in both directions); -1 restores the order."""
        self.beginResetModel()
        if column < 0 or column >= len(self._original.columns):
            self._df = self._original
            self._sort = None
        else:
            ascending = order == Qt.SortOrder.AscendingOrder
            positions = _stable_order(self._original.iloc[:, column], ascending)
            self._df = self._original.iloc[positions].reset_index(drop=True)
            self._sort = (column, order)
        self.endResetModel()

    # ----- copy ------------------------------------------------------------
    def to_tsv(self, indexes: Iterable[_Index], include_header: bool = False) -> str:
        """TSV of the selected cells: the rows × columns spanned by the selection.

        Cells inside that grid which aren't selected are left empty; NULL is empty; tabs and
        newlines inside values become spaces.
        """
        cells = {(i.row(), i.column()) for i in indexes if i.isValid()}
        if not cells:
            return ""
        rows = sorted({r for r, _ in cells})
        cols = sorted({c for _, c in cells})
        lines: list[str] = []
        if include_header:
            lines.append("\t".join(_tsv_cell(str(self._df.columns[c])) for c in cols))
        for r in rows:
            lines.append(
                "\t".join(_tsv_cell(self.value(r, c)) if (r, c) in cells else "" for c in cols)
            )
        return "\n".join(lines)
