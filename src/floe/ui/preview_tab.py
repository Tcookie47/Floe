"""Preview tab (SPEC §8.1): the first `preview_row_limit` rows of the selected table."""

from __future__ import annotations

import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from floe.core.context import TableInfo
from floe.core.errors import MissingDataSourceColumn
from floe.ui.results_view import ResultsView
from floe.ui.workers import user_error_text

EXPORT_HINT = "Export the rows currently shown to a CSV file."


class MessagePanel(QWidget):
    """A centred, selectable message with an optional action button."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.button = QPushButton()
        self.button.hide()
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.button)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(self.label)
        layout.addLayout(row)
        layout.addStretch(1)


class ResultsPane(QWidget):
    """A results grid that can instead show a loading / empty / error message.

    Optionally carries an "Export CSV…" button (SPEC §9): enabled only while a
    profile allows it (`set_export_allowed`) *and* a result is currently shown.
    """

    # Emitted when the user clicks "Export CSV…"; the owner supplies the dataframe
    # and destination (`MainWindow` drives the file dialog, confirmation, and worker).
    exportRequested = Signal()  # noqa: N815 - Qt signal naming

    def __init__(self, parent: QWidget | None = None, *, show_export: bool = False) -> None:
        super().__init__(parent)
        self.view = ResultsView()
        self.message = MessagePanel()
        self._stack = QStackedWidget()
        self._stack.addWidget(self.message)
        self._stack.addWidget(self.view)

        self.export_button = QPushButton("Export CSV…")
        self.export_button.setEnabled(False)
        self.export_button.setVisible(show_export)
        self.export_button.clicked.connect(self.exportRequested.emit)
        self._export_allowed = False

        bar = QHBoxLayout()
        bar.addStretch(1)
        bar.addWidget(self.export_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if show_export:
            layout.addLayout(bar)
        layout.addWidget(self._stack)
        self.state = "empty"  # "empty" | "loading" | "ready" | "error"

    def set_export_allowed(self, allowed: bool, disabled_tooltip: str = "") -> None:
        """Gate the export button on the active profile's `allow_export` (SPEC §9)."""
        self._export_allowed = allowed
        self.export_button.setToolTip(EXPORT_HINT if allowed else disabled_tooltip)
        self._update_export_enabled()

    def _update_export_enabled(self) -> None:
        self.export_button.setEnabled(self._export_allowed and self.state == "ready")

    def show_text(self, text: str, state: str = "empty") -> None:
        self.state = state
        self.view.clear()
        self.message.label.setText(text)
        self.message.button.hide()
        self._stack.setCurrentWidget(self.message)
        self._update_export_enabled()

    def show_dataframe(self, df: pd.DataFrame) -> None:
        self.state = "ready"
        self.message.button.hide()
        self.view.set_dataframe(df)
        self._stack.setCurrentWidget(self.view)
        self._update_export_enabled()

    @property
    def message_text(self) -> str:
        return self.message.label.text()


class PreviewTab(ResultsPane):
    """Shows preview rows; errors inline. The main window runs the query in a worker."""

    # Emitted with the TableInfo whose preview should be re-run without the tenant filter.
    disableTenantFilterRequested = Signal(object)  # noqa: N815 - Qt signal naming

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, show_export=True)
        self._table: TableInfo | None = None
        self.message.button.setText("Disable tenant filter for this table")
        self.message.button.clicked.connect(self._on_disable_filter)
        self.show_text("Select a table to preview its rows.")

    @property
    def table(self) -> TableInfo | None:
        return self._table

    @property
    def disable_filter_button(self) -> QPushButton:
        return self.message.button

    def clear(self, text: str = "Select a table to preview its rows.") -> None:
        self._table = None
        self.show_text(text)

    def show_loading(self, table: TableInfo) -> None:
        self._table = table
        self.show_text(f"Loading {table.view_name}…", "loading")

    def show_result(self, table: TableInfo, df: pd.DataFrame) -> None:
        self._table = table
        if df.empty and len(df.columns) == 0:
            self.show_text(f"{table.view_name} returned no columns.")
            return
        self.show_dataframe(df)

    def show_error(self, table: TableInfo, exc: BaseException) -> None:
        self._table = table
        self.show_text(user_error_text(exc), "error")
        if isinstance(exc, MissingDataSourceColumn):
            self.message.button.show()

    def _on_disable_filter(self) -> None:
        if self._table is not None:
            self.disableTenantFilterRequested.emit(self._table)
