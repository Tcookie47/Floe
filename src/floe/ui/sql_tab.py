"""Ad-hoc SQL editor tab (SPEC §6.3 SQL-tab resolution, §8.1 SQL tab, §8.5).

Self-contained like `PreviewTab`: `MainWindow` supplies accessors for the current
session / ref / tenant-filter setting (mirroring `_tenant_filter_for`) plus callbacks for
the status-bar text, and `SqlTab` drives its own `TaskRunner` channel ("sql"). Cancelling
here is always synchronous from the widget's point of view: `TaskRunner` drops every
callback of a cancelled worker (see `workers.TaskRunner._on_completed`), so the Run/Cancel
buttons and the results pane are reset directly by `cancel()` / `cancel_silently()` rather
than waiting on a callback that will never arrive.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from floe.core.context import FloeSession, QueryResult
from floe.core.errors import MissingDataSourceColumn, QueryCancelled
from floe.core.history import HistoryEntry
from floe.ui.preview_tab import ResultsPane
from floe.ui.workers import TaskRunner, Worker, user_error_text

SQL_CHANNEL = "sql"
DEFAULT_ROW_LIMIT = 10_000
MAX_ROW_LIMIT = 1_000_000
HINT_TEXT = "Tables are available as views — hover a table in the tree to see its view name."
PLACEHOLDER_TEXT = "Run a query to see results here."


def _selected_text(editor: QPlainTextEdit) -> str:
    """The editor's selection, or its full text if there's no selection.

    `QTextCursor.selectedText()` uses U+2029 as its line separator; normalise it back to
    `\\n` so multi-line selections run as the user wrote them.
    """
    cursor = editor.textCursor()
    if cursor.hasSelection():
        return cursor.selectedText().replace(" ", "\n")
    return editor.toPlainText()


class SqlTab(QWidget):
    """Monospace SQL editor, Run/Cancel, a row-limit override, and a results grid."""

    def __init__(
        self,
        runner: TaskRunner,
        get_session: Callable[[], FloeSession | None],
        get_ref: Callable[[], str | None],
        get_tenant_filter: Callable[[], bool],
        *,
        set_status: Callable[[str], None] | None = None,
        show_notice: Callable[[str, int], None] | None = None,
        get_history: Callable[[], list[HistoryEntry]] | None = None,
        on_run_success: Callable[[str], None] | None = None,
        on_clear_history: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._runner = runner
        self._get_session = get_session
        self._get_ref = get_ref
        self._get_tenant_filter = get_tenant_filter
        self._set_status = set_status or (lambda _text: None)
        self._show_notice = show_notice or (lambda _text, _ms: None)
        self._get_history = get_history or (lambda: [])
        self._on_run_success = on_run_success or (lambda _sql: None)
        self._on_clear_history = on_clear_history or (lambda: None)
        self._running = False
        self._worker: Worker | None = None
        self._filter_override_once = False
        self._last_sql = ""

        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText("SELECT * FROM ...")
        self._editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self._run_btn = QPushButton("Run")
        self._run_btn.clicked.connect(self.run)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self.cancel)

        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(1, MAX_ROW_LIMIT)
        self._limit_spin.setValue(DEFAULT_ROW_LIMIT)
        self._limit_spin.setToolTip("Row limit for this query's results (SPEC §8.5).")

        self._history_btn = QToolButton()
        self._history_btn.setText("History")
        self._history_btn.setToolTip(
            "Past queries for this profile (SQL text only — never results)."
        )
        self._history_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._history_menu = QMenu(self._history_btn)
        self._history_menu.aboutToShow.connect(self._populate_history_menu)
        self._history_btn.setMenu(self._history_menu)

        hint = QLabel(HINT_TEXT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray;")

        toolbar = QHBoxLayout()
        toolbar.addWidget(self._run_btn)
        toolbar.addWidget(self._cancel_btn)
        toolbar.addSpacing(12)
        toolbar.addWidget(QLabel("Row limit:"))
        toolbar.addWidget(self._limit_spin)
        toolbar.addSpacing(12)
        toolbar.addWidget(self._history_btn)
        toolbar.addStretch(1)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(hint)
        top_layout.addWidget(self._editor, stretch=1)
        top_layout.addLayout(toolbar)

        self._results = ResultsPane(show_export=True)
        self._results.message.button.clicked.connect(self._on_disable_filter_clicked)
        self._results.show_text(PLACEHOLDER_TEXT)

        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.addWidget(top)
        self._splitter.addWidget(self._results)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([260, 400])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._splitter)

        # Cmd+Enter / Ctrl+Enter (Qt's ControlModifier is Cmd on macOS).
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(
                QKeySequence(Qt.KeyboardModifier.ControlModifier | key), self._editor
            )
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.run)

    # ----- accessors (tests, MainWindow) ------------------------------------
    @property
    def editor(self) -> QPlainTextEdit:
        return self._editor

    @property
    def run_button(self) -> QPushButton:
        return self._run_btn

    @property
    def cancel_button(self) -> QPushButton:
        return self._cancel_btn

    @property
    def limit_spin(self) -> QSpinBox:
        return self._limit_spin

    @property
    def results(self) -> ResultsPane:
        return self._results

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def history_button(self) -> QToolButton:
        return self._history_btn

    @property
    def history_menu(self) -> QMenu:
        return self._history_menu

    def splitter_sizes(self) -> list[int]:
        return self._splitter.sizes()

    def set_splitter_sizes(self, sizes: list[int]) -> None:
        if sizes:
            self._splitter.setSizes(sizes)

    def insert_text(self, text: str) -> None:
        """Insert `text` at the cursor (used for "Insert view name" from the table tree)."""
        self._editor.textCursor().insertText(text)
        self._editor.setFocus()

    # ----- history -------------------------------------------------------------
    def _populate_history_menu(self) -> None:
        menu = self._history_menu
        menu.clear()
        entries = self._get_history()
        if not entries:
            empty = menu.addAction("(no history yet)")
            empty.setEnabled(False)
        else:
            for entry in entries:
                first_line = entry.sql.strip().splitlines()[0] if entry.sql.strip() else ""
                label = first_line[:60] + ("…" if len(first_line) > 60 else "")
                action = menu.addAction(label)
                action.setToolTip(entry.sql)
                action.triggered.connect(
                    lambda _checked=False, sql=entry.sql: self._insert_history(sql)
                )
        menu.addSeparator()
        clear_action = menu.addAction("Clear history")
        clear_action.triggered.connect(self._on_clear_history)

    def _insert_history(self, sql: str) -> None:
        self._editor.setPlainText(sql)
        self._editor.setFocus()

    # ----- run / cancel ------------------------------------------------------
    def run(self) -> None:
        if self._running:
            return
        session = self._get_session()
        ref = self._get_ref()
        if session is None or ref is None:
            self._results.show_text("No active session.", "error")
            return
        sql = _selected_text(self._editor)
        if not sql.strip():
            return
        self._last_sql = sql
        limit = self._limit_spin.value()
        tenant_filter = self._get_tenant_filter()
        if self._filter_override_once:
            tenant_filter = False
        self._filter_override_once = False

        self._set_running(True)
        self._results.show_text("Running…", "loading")
        self._set_status("Running…")

        def do_run(cancel_token: object) -> QueryResult:
            return session.query(
                ref, sql, limit=limit, tenant_filter=tenant_filter, cancel_token=cancel_token
            )

        self._worker = self._runner.submit(
            SQL_CHANNEL,
            do_run,
            on_result=lambda result: self._on_result(result, tenant_filter),
            on_error=self._on_error,
            on_finished=lambda: self._set_running(False),
        )

    def cancel(self) -> None:
        """User-initiated cancel: interrupt the query and show the quiet cancelled message."""
        self._cancel_internal(silent=False)

    def cancel_silently(self) -> None:
        """Cancel without a status message (branch/profile switch invalidates this tab too)."""
        self._cancel_internal(silent=True)

    def _cancel_internal(self, *, silent: bool) -> None:
        was_running = self._running
        if self._worker is not None:
            self._worker.cancel()
        self._set_running(False)
        if was_running and not silent:
            self._results.show_text("Query cancelled.")
            self._set_status("")
            self._show_notice("Query cancelled.", 5000)

    def _set_running(self, running: bool) -> None:
        self._running = running
        self._run_btn.setEnabled(not running)
        self._cancel_btn.setEnabled(running)
        if not running:
            self._worker = None

    # ----- results -----------------------------------------------------------
    def _on_result(self, result: QueryResult, tenant_filter: bool) -> None:
        self._results.show_dataframe(result.df)
        self._set_status(self._summarize(result, tenant_filter))
        if result.reloaded:
            self._show_notice("Catalog changed — reloaded and retried.", 8000)
        self._on_run_success(self._last_sql)

    @staticmethod
    def _summarize(result: QueryResult, tenant_filter: bool) -> str:
        parts = [f"{result.row_count:,} rows", f"{result.elapsed:.2f} s"]
        if result.truncated:
            parts.append(f"Showing first {result.row_count:,} rows (limit reached)")
        if not tenant_filter:
            parts.append("tenant filter off")
        return " · ".join(parts)

    def _on_error(self, exc: BaseException) -> None:
        if isinstance(exc, QueryCancelled):
            # Cancelled through the runner (e.g. a stale generation); already handled by
            # `cancel()` / `cancel_silently()` for the normal path, but cover this too.
            self._results.show_text("Query cancelled.")
            self._set_status("")
            return
        self._results.show_text(user_error_text(exc), "error")
        self._set_status("Query failed")
        if isinstance(exc, MissingDataSourceColumn):
            self._results.message.button.setText("Run without tenant filter")
            self._results.message.button.show()

    def _on_disable_filter_clicked(self) -> None:
        self._filter_override_once = True
        self.run()
