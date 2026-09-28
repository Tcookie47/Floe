"""Results grid widget (SPEC §8.5): sortable, resizable, Cmd/Ctrl+C copies TSV."""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QMenu, QTableView, QWidget

from floe.ui.results_model import DataFrameModel

MIN_COLUMN_WIDTH = 60
MAX_COLUMN_WIDTH = 320
RESIZE_PRECISION_ROWS = 100  # rows sampled when sizing columns to their contents


class ResultsView(QTableView):
    """A `QTableView` over its own `DataFrameModel`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = DataFrameModel(parent=self)
        self.setModel(self._model)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setResizeContentsPrecision(RESIZE_PRECISION_ROWS)
        header.setHighlightSections(False)
        if hasattr(header, "setSortIndicatorClearable"):
            header.setSortIndicatorClearable(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.setSortingEnabled(True)
        self.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 6)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

    @property
    def results_model(self) -> DataFrameModel:
        return self._model

    def set_dataframe(self, df: pd.DataFrame | None) -> None:
        """Show `df` unsorted, with default column widths."""
        self.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self._model.set_dataframe(df)
        self.resize_columns()

    def clear(self) -> None:
        self.set_dataframe(None)

    def resize_columns(self) -> None:
        self.resizeColumnsToContents()
        header = self.horizontalHeader()
        for col in range(self._model.columnCount()):
            width = header.sectionSize(col)
            header.resizeSection(col, max(MIN_COLUMN_WIDTH, min(MAX_COLUMN_WIDTH, width)))

    # ----- copy ------------------------------------------------------------
    def selection_tsv(self, include_header: bool = False) -> str:
        return self._model.to_tsv(self.selectionModel().selectedIndexes(), include_header)

    def copy_selection(self, include_header: bool = False) -> str:
        """Copy the selection as TSV to the clipboard; returns the copied text."""
        text = self.selection_tsv(include_header)
        if text:
            QGuiApplication.clipboard().setText(text)
        return text

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _show_menu(self, pos) -> None:
        menu = QMenu(self)
        copy = QAction("Copy", menu)
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.triggered.connect(lambda: self.copy_selection())
        with_header = QAction("Copy with headers", menu)
        with_header.triggered.connect(lambda: self.copy_selection(include_header=True))
        select_all = QAction("Select all", menu)
        select_all.triggered.connect(self.selectAll)
        has_selection = self.selectionModel().hasSelection()
        copy.setEnabled(has_selection)
        with_header.setEnabled(has_selection)
        menu.addActions([copy, with_header, select_all])
        menu.exec(self.viewport().mapToGlobal(pos))
