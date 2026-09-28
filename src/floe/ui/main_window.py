"""Main application window (SPEC §8.1, §8.4).

Top bar: profile, Edit profiles…, branch, refresh, head hash (click to copy), tenant
filter toggle, catalog status. Left: table tree (`BrowserPanel`). Right: Preview and
Schema tabs. Status bar: elapsed time, row count, truncation notice.

Threading (SPEC §8.4): every Nessie / DuckDB call runs through a `TaskRunner` channel
("session", "tables", "preview", "schema"). Switching profile or branch invalidates the
affected channels, which cancels their `CancelToken`s and drops any result that still
arrives. Closing the window cancels everything and waits briefly for the pool.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pandas as pd
from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QBrush, QCloseEvent, QColor, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from floe import __commit__, __version__
from floe.core import diagnostics, paths
from floe.core.context import BranchInfo, FloeSession, QueryResult, TableInfo
from floe.core.errors import (
    CorruptPointer,
    FloeError,
    MissingDataSourceColumn,
    QueryCancelled,
    TableNotFound,
    TenantScopeError,
    ViewNameCollision,
)
from floe.core.export import export_csv
from floe.core.history import HistoryStore
from floe.core.profiles import SECRET_FIELDS, Profile, ProfileStore
from floe.ui.browser_panel import BrowserPanel
from floe.ui.preview_tab import PreviewTab
from floe.ui.profile_dialog import ProfileDialog
from floe.ui.schema_tab import SchemaTab
from floe.ui.sql_tab import SqlTab
from floe.ui.workers import TaskRunner, user_error_text

log = logging.getLogger("floe.ui")

SessionFactory = Callable[[Profile], FloeSession]

BRANCH_ROLE = Qt.ItemDataRole.UserRole + 1
INACTIVE_COLOR = QColor(150, 150, 150)
INACTIVE_SUFFIX = " (inactive)"
HEAD_CHARS = 8

CATALOG_TEXT = {
    "ok": ("Catalog OK", "#2e7d32", "Nessie answered the last request."),
    "stale": (
        "Catalog unreachable, showing last known state",
        "#e65100",
        "The last head refresh failed; Floe is using the last known head.",
    ),
    "unknown": ("Catalog status unknown", "#757575", "No catalog request has completed yet."),
    "local": ("Local mode", "#757575", "Local mode reads parquet files; there is no catalog."),
}
NOT_APPLICABLE_ON_MAIN = "Not applicable on the main branch"
TENANT_FILTER_TOOLTIP = (
    "Only show this branch's rows (WHERE data_source = <branch value>) in tenant tables."
)

TABLE_CHANNELS = ("tables", "preview", "schema")
SQL_CHANNEL = "sql"
EXPORT_CHANNEL = "export"

EXPORT_DISABLED_TOOLTIP = "Export is disabled for this profile (Edit profiles… → Safety)."
EXPORT_ENABLED_TOOLTIP = "Export the rows currently shown to a CSV file."
SETTINGS_ORG = "tcookie"
SETTINGS_APP = "Floe"


def _int_list(value: object) -> list[int]:
    """Coerce a `QSettings` list value (strings from ini, ints from native) to ints."""
    if not value:
        return []
    try:
        return [int(v) for v in value]
    except (TypeError, ValueError):
        return []


def default_session_factory(store: ProfileStore) -> SessionFactory:
    """Build a `FloeSession` with the profile's Keychain secrets (runs in a worker)."""

    def make(profile: Profile) -> FloeSession:
        secrets: dict[str, str | None] = {}
        if profile.mode != "local":
            for field_name in SECRET_FIELDS:
                secrets[field_name] = store.get_secret(profile.name, field_name)
        return FloeSession(profile, secrets)

    return make


def _status_for_error(exc: BaseException) -> str | None:
    """Tree status for a per-table error (None: not a table-level problem)."""
    if isinstance(exc, TableNotFound):
        return "not_found"
    if isinstance(exc, CorruptPointer):
        return "corrupt"
    if isinstance(exc, TenantScopeError):
        return "scope_error"
    if isinstance(exc, MissingDataSourceColumn):
        return "missing_data_source"
    if isinstance(exc, ViewNameCollision):
        return "error"
    return None


class MainWindow(QMainWindow):
    """Floe's top-level window."""

    def __init__(
        self,
        store: ProfileStore | None = None,
        session_factory: SessionFactory | None = None,
        settings: QSettings | None = None,
        history: HistoryStore | None = None,
    ) -> None:
        super().__init__()
        self._store = store if store is not None else ProfileStore()
        self._session_factory = session_factory or default_session_factory(self._store)
        self._settings = (
            settings
            if settings is not None
            else QSettings(
                QSettings.Format.IniFormat, QSettings.Scope.UserScope, SETTINGS_ORG, SETTINGS_APP
            )
        )
        self._history = history if history is not None else HistoryStore()
        self._runner = TaskRunner(self)
        self._session: FloeSession | None = None
        self._active_profile: Profile | None = None
        self._ref: str | None = None
        self._table: TableInfo | None = None
        self._head: str | None = None
        self._reselect: str | None = None  # dotted key to select again after a refresh
        self._filter_overrides: set[tuple[str, str]] = set()  # (ref, dotted) filter off
        self._catalog_status = "unknown"
        self._closed = False

        self.setWindowTitle("Floe")
        self.resize(1200, 760)
        self._build_central()
        self._build_menu()
        self._reload_profiles()
        self._restore_window_state()
        self._activate_profile(self._preferred_profile())

    # ----- layout ------------------------------------------------------------
    def _build_central(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)

        top_bar = QHBoxLayout()
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(180)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self._edit_profiles_btn = QPushButton("Edit profiles…")
        self._edit_profiles_btn.clicked.connect(self._open_profile_dialog)

        self._branch_combo = QComboBox()
        self._branch_combo.setMinimumWidth(180)
        self._branch_combo.setEnabled(False)
        self._branch_combo.currentIndexChanged.connect(self._on_branch_changed)

        self._refresh_btn = QToolButton()
        self._refresh_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self._refresh_btn.setToolTip("Refresh: re-read the branch head and reload the table tree")
        self._refresh_btn.clicked.connect(self.refresh)

        self._head_btn = QToolButton()
        self._head_btn.setAutoRaise(True)
        self._head_btn.setEnabled(False)
        self._head_btn.setText("—")
        self._head_btn.clicked.connect(self.copy_head_hash)

        self._tenant_filter_cb = QCheckBox("Tenant filter")
        self._tenant_filter_cb.setChecked(True)
        self._tenant_filter_cb.setToolTip(TENANT_FILTER_TOOLTIP)
        self._tenant_filter_cb.toggled.connect(self._on_tenant_filter_toggled)

        self._catalog_label = QLabel()

        top_bar.addWidget(QLabel("Profile:"))
        top_bar.addWidget(self._profile_combo)
        top_bar.addWidget(self._edit_profiles_btn)
        top_bar.addSpacing(12)
        top_bar.addWidget(QLabel("Branch:"))
        top_bar.addWidget(self._branch_combo)
        top_bar.addWidget(self._refresh_btn)
        top_bar.addWidget(QLabel("Head:"))
        top_bar.addWidget(self._head_btn)
        top_bar.addSpacing(12)
        top_bar.addWidget(self._tenant_filter_cb)
        top_bar.addStretch(1)
        top_bar.addWidget(self._catalog_label)
        outer.addLayout(top_bar)

        self._browser = BrowserPanel()
        self._browser.tableSelected.connect(self._on_table_selected)
        self._browser.insertViewNameRequested.connect(self._insert_view_name)
        self._preview = PreviewTab()
        self._preview.disableTenantFilterRequested.connect(self._disable_filter_for_table)
        self._preview.exportRequested.connect(self._export_preview)
        self._schema = SchemaTab()
        self._sql = SqlTab(
            self._runner,
            lambda: self._session,
            lambda: self._ref,
            lambda: self._tenant_filter_cb.isChecked(),
            set_status=lambda text: self._result_label.setText(text),
            show_notice=lambda text, ms: self.statusBar().showMessage(text, ms),
            get_history=self._get_history,
            on_run_success=self._record_history,
            on_clear_history=self._clear_history,
        )
        self._sql.results.exportRequested.connect(self._export_sql)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._preview, "Preview")
        self._tabs.addTab(self._schema, "Schema")
        self._tabs.addTab(self._sql, "SQL")

        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self._browser)
        self._splitter.addWidget(self._tabs)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([300, 900])
        outer.addWidget(self._splitter, stretch=1)
        self.setCentralWidget(central)

        self._result_label = QLabel()
        self.statusBar().addPermanentWidget(self._result_label)
        self._set_catalog_status("unknown")

    def _build_menu(self) -> None:
        help_menu = self.menuBar().addMenu("Help")
        about_action = help_menu.addAction("About Floe")
        about_action.triggered.connect(self._show_about)
        diagnostics_action = help_menu.addAction("Copy diagnostics")
        diagnostics_action.triggered.connect(self._copy_diagnostics)
        log_folder_action = help_menu.addAction("Show log folder")
        log_folder_action.triggered.connect(self._open_log_folder)

        view_menu = self.menuBar().addMenu("View / Query")

        self._refresh_action = view_menu.addAction("Refresh")
        self._refresh_action.setShortcut(QKeySequence("Ctrl+R"))
        self._refresh_action.triggered.connect(self.refresh)
        self.addAction(self._refresh_action)

        self._cancel_action = view_menu.addAction("Cancel query")
        self._cancel_action.setShortcuts(
            [QKeySequence("Ctrl+."), QKeySequence(Qt.Key.Key_Escape)]
        )
        self._cancel_action.triggered.connect(self._cancel_running_query)
        self.addAction(self._cancel_action)

        self._focus_filter_action = view_menu.addAction("Focus table filter")
        self._focus_filter_action.setShortcut(QKeySequence.StandardKey.Find)
        self._focus_filter_action.triggered.connect(self._focus_table_filter)
        self.addAction(self._focus_filter_action)

        view_menu.addSeparator()
        self._preview_tab_action = view_menu.addAction("Preview tab")
        self._preview_tab_action.setShortcut(QKeySequence("Ctrl+1"))
        self._preview_tab_action.triggered.connect(
            lambda: self._tabs.setCurrentWidget(self._preview)
        )
        self.addAction(self._preview_tab_action)

        self._schema_tab_action = view_menu.addAction("Schema tab")
        self._schema_tab_action.setShortcut(QKeySequence("Ctrl+2"))
        self._schema_tab_action.triggered.connect(
            lambda: self._tabs.setCurrentWidget(self._schema)
        )
        self.addAction(self._schema_tab_action)

        self._sql_tab_action = view_menu.addAction("SQL tab")
        self._sql_tab_action.setShortcut(QKeySequence("Ctrl+3"))
        self._sql_tab_action.triggered.connect(lambda: self._tabs.setCurrentWidget(self._sql))
        self.addAction(self._sql_tab_action)

        view_menu.addSeparator()
        self._export_action = view_menu.addAction("Export CSV…")
        self._export_action.setShortcut(QKeySequence("Ctrl+E"))
        self._export_action.setEnabled(False)
        self._export_action.triggered.connect(self._export_current_tab)
        self.addAction(self._export_action)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Floe",
            f"Floe {__version__}\nCommit: {__commit__}",
        )

    def _open_log_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs_dir())))

    # ----- accessors (tests, Phase 5) ----------------------------------------
    @property
    def runner(self) -> TaskRunner:
        return self._runner

    @property
    def session(self) -> FloeSession | None:
        return self._session

    @property
    def current_ref(self) -> str | None:
        return self._ref

    @property
    def head_hash(self) -> str | None:
        return self._head

    @property
    def browser(self) -> BrowserPanel:
        return self._browser

    @property
    def preview_tab(self) -> PreviewTab:
        return self._preview

    @property
    def schema_tab(self) -> SchemaTab:
        return self._schema

    @property
    def sql_tab(self) -> SqlTab:
        return self._sql

    @property
    def tabs(self) -> QTabWidget:
        return self._tabs

    @property
    def branch_combo(self) -> QComboBox:
        return self._branch_combo

    @property
    def tenant_filter_checkbox(self) -> QCheckBox:
        return self._tenant_filter_cb

    @property
    def head_button(self) -> QToolButton:
        return self._head_btn

    @property
    def catalog_label(self) -> QLabel:
        return self._catalog_label

    @property
    def result_label(self) -> QLabel:
        return self._result_label

    @property
    def settings(self) -> QSettings:
        return self._settings

    @property
    def history(self) -> HistoryStore:
        return self._history

    @property
    def main_splitter(self) -> QSplitter:
        return self._splitter

    @property
    def export_action(self) -> object:
        return self._export_action

    @property
    def refresh_action(self) -> object:
        return self._refresh_action

    @property
    def cancel_action(self) -> object:
        return self._cancel_action

    @property
    def focus_filter_action(self) -> object:
        return self._focus_filter_action

    @property
    def preview_tab_action(self) -> object:
        return self._preview_tab_action

    @property
    def schema_tab_action(self) -> object:
        return self._schema_tab_action

    @property
    def sql_tab_action(self) -> object:
        return self._sql_tab_action

    # ----- profiles ----------------------------------------------------------
    def _reload_profiles(self) -> None:
        current = self._profile_combo.currentText()
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        names = sorted(p.name for p in self._store.list())
        self._profile_combo.addItems(names)
        if current in names:
            self._profile_combo.setCurrentText(current)
        self._profile_combo.blockSignals(False)

    def _on_profiles_edited(self) -> None:
        self._reload_profiles()
        profile = self.active_profile()
        if profile != self._active_profile:  # renamed, deleted or edited: reconnect
            self._activate_profile(profile)

    def _preferred_profile(self) -> Profile | None:
        """The profile remembered from a previous session, if it still exists."""
        last_name = self._settings.value("profile/last")
        if last_name:
            profile = self._store.get(str(last_name))
            if profile is not None:
                idx = self._profile_combo.findText(profile.name)
                if idx >= 0:
                    self._profile_combo.setCurrentIndex(idx)
                return profile
        return self.active_profile()

    def _open_profile_dialog(self) -> None:
        dialog = ProfileDialog(self._store, self)
        dialog.profilesChanged.connect(self._on_profiles_edited)
        dialog.exec()

    def active_profile(self) -> Profile | None:
        name = self._profile_combo.currentText()
        if not name:
            return None
        return self._store.get(name)

    def _on_profile_changed(self, _index: int) -> None:
        self._activate_profile(self.active_profile())

    def _cancel_all(self) -> None:
        self._runner.invalidate()
        if self._session is not None:
            self._session.interrupt_all()

    def _activate_profile(self, profile: Profile | None) -> None:
        """Drop the old session (cancelling its work) and connect `profile` in a worker."""
        if self._closed:
            return
        old_profile = self._active_profile
        self._cancel_all()
        self._session = None
        self._active_profile = profile
        self._ref = None
        self._table = None
        self._reselect = None
        self._filter_overrides.clear()
        self._branch_combo.blockSignals(True)
        self._branch_combo.clear()
        self._branch_combo.blockSignals(False)
        self._branch_combo.setEnabled(False)
        self._set_head(None)
        self._set_catalog_status("unknown")
        self._preview.clear()
        self._schema.clear()
        self._sql.cancel_silently()
        self._result_label.clear()
        if old_profile is not None:
            self._settings.setValue(
                f"sql_text/{old_profile.name}", self._sql.editor.toPlainText()
            )
        self._sql.editor.setPlainText(
            str(self._settings.value(f"sql_text/{profile.name}", "")) if profile else ""
        )
        self._update_export_gate()
        if profile is None:
            self._browser.show_message("No profile yet. Use Edit profiles… to create one.")
            return
        self._browser.show_loading("Connecting…")
        factory = self._session_factory

        def load() -> tuple[FloeSession | None, list[BranchInfo] | None, BaseException | None]:
            session = factory(profile)
            try:
                return session, session.branches(), None
            except FloeError as exc:
                return session, None, exc

        self._runner.submit(
            "session", load, on_result=self._on_session_loaded, on_error=self._on_session_error
        )

    def _on_session_loaded(
        self, outcome: tuple[FloeSession, list[BranchInfo] | None, BaseException | None]
    ) -> None:
        session, branches, exc = outcome
        self._session = session
        self._update_catalog_status()
        if exc is not None or branches is None:
            text = user_error_text(exc) if exc is not None else "No branches."
            self._browser.show_error(f"Could not list branches: {text}")
            self.statusBar().showMessage(f"Could not list branches: {text}", 10000)
            return
        self._populate_branches(branches)

    def _on_session_error(self, exc: BaseException) -> None:
        text = user_error_text(exc)
        self._browser.show_error(f"Could not open the profile: {text}")
        self.statusBar().showMessage(f"Could not open the profile: {text}", 10000)

    # ----- branches ----------------------------------------------------------
    def _populate_branches(self, branches: list[BranchInfo]) -> None:
        combo = self._branch_combo
        combo.blockSignals(True)
        combo.clear()
        for branch in branches:
            inactive = branch.active is False
            combo.addItem(branch.name + (INACTIVE_SUFFIX if inactive else ""), branch.name)
            idx = combo.count() - 1
            if inactive:
                combo.setItemData(idx, QBrush(INACTIVE_COLOR), Qt.ItemDataRole.ForegroundRole)
                combo.setItemData(
                    idx, "Inactive tenant (still selectable)", Qt.ItemDataRole.ToolTipRole
                )
        default_idx = 0 if branches else -1
        if branches and self._active_profile is not None:
            wanted = self._settings.value(f"branch/{self._active_profile.name}")
            if wanted:
                found = combo.findData(str(wanted))
                if found >= 0:
                    default_idx = found
        combo.setCurrentIndex(default_idx)
        combo.blockSignals(False)
        combo.setEnabled(bool(branches))
        if not branches:
            self._browser.show_message("No branches found.")
            return
        self._on_branch_changed(combo.currentIndex())

    def select_branch(self, name: str) -> bool:
        """Select a branch by name (as if picked in the combo)."""
        idx = self._branch_combo.findData(name)
        if idx < 0:
            return False
        self._branch_combo.setCurrentIndex(idx)
        return True

    def _on_branch_changed(self, index: int) -> None:
        if index < 0 or self._session is None:
            return
        ref = self._branch_combo.itemData(index)
        if ref:
            self._select_ref(str(ref))

    def _select_ref(self, ref: str, *, refresh: bool = False) -> None:
        """Show `ref`'s tables; cancels in-flight tree / preview / schema work (SPEC §8.4)."""
        session = self._session
        if session is None or self._closed:
            return
        self._runner.invalidate(*TABLE_CHANNELS, SQL_CHANNEL)
        self._sql.cancel_silently()
        self._reselect = self._table.dotted if (refresh and self._table is not None) else None
        self._ref = ref
        self._table = None
        self._update_tenant_filter_enabled()
        self._browser.show_loading()
        self._preview.clear()
        self._schema.clear()
        self._result_label.clear()
        self._set_head(None, loading=not session.is_local)

        def load() -> tuple[list[TableInfo], str | None]:
            if refresh:
                session.refresh()
            return session.list_tables(ref), session.current_head(ref)

        self._runner.submit(
            "tables", load, on_result=self._on_tables_loaded, on_error=self._on_tables_error
        )

    def _on_tables_loaded(self, outcome: tuple[list[TableInfo], str | None]) -> None:
        tables, head = outcome
        self._set_head(head)
        self._update_catalog_status()
        self._browser.set_tables(tables)
        self.statusBar().showMessage(f"{len(tables)} tables on {self._ref}.", 5000)
        reselect, self._reselect = self._reselect, None
        if reselect:
            self._browser.select_table(reselect)

    def _on_tables_error(self, exc: BaseException) -> None:
        self._update_catalog_status()
        text = user_error_text(exc)
        self._browser.show_error(f"Could not list tables: {text}")
        self.statusBar().showMessage(f"Could not list tables on {self._ref}: {text}", 10000)

    def refresh(self) -> None:
        """Clear cached heads and reload the tree (or reconnect if there's no session)."""
        if self._session is None or self._ref is None:
            self._activate_profile(self.active_profile())
            return
        self._select_ref(self._ref, refresh=True)

    # ----- head hash / catalog status ------------------------------------------
    def _set_head(self, head: str | None, *, loading: bool = False) -> None:
        self._head = head
        if head:
            self._head_btn.setText(head[:HEAD_CHARS])
            self._head_btn.setToolTip(f"Head {head}\nClick to copy the full hash.")
            self._head_btn.setEnabled(True)
            return
        local = self._session is not None and self._session.is_local
        self._head_btn.setText("…" if loading else ("local" if local else "—"))
        self._head_btn.setToolTip("Local mode has no catalog head." if local else "")
        self._head_btn.setEnabled(False)

    def copy_head_hash(self) -> None:
        if not self._head:
            return
        QApplication.clipboard().setText(self._head)
        self.statusBar().showMessage(
            f"Copied head hash {self._head[:HEAD_CHARS]}… to the clipboard.", 5000
        )

    def _update_catalog_status(self) -> None:
        session = self._session
        if session is None:
            self._set_catalog_status("unknown")
        elif session.is_local:
            self._set_catalog_status("local")
        else:
            self._set_catalog_status(session.catalog_status)

    def _set_catalog_status(self, status: str) -> None:
        self._catalog_status = status
        text, color, tip = CATALOG_TEXT.get(status, CATALOG_TEXT["unknown"])
        self._catalog_label.setText(f'<span style="color:{color}">●</span> {text}')
        self._catalog_label.setToolTip(tip)

    @property
    def catalog_status(self) -> str:
        return self._catalog_status

    # ----- tenant filter -------------------------------------------------------
    def _update_tenant_filter_enabled(self) -> None:
        session, ref = self._session, self._ref
        applicable = session is not None and ref is not None and session.is_tenant_ref(ref)
        self._tenant_filter_cb.setEnabled(applicable)
        self._tenant_filter_cb.setToolTip(
            TENANT_FILTER_TOOLTIP if applicable or session is None else NOT_APPLICABLE_ON_MAIN
        )

    def _tenant_filter_for(self, table: TableInfo) -> bool:
        if self._ref is not None and (self._ref, table.dotted) in self._filter_overrides:
            return False
        return self._tenant_filter_cb.isChecked()

    def _on_tenant_filter_toggled(self, _checked: bool) -> None:
        if self._table is not None:
            self._run_table_work()

    def _disable_filter_for_table(self, table: TableInfo) -> None:
        if self._ref is None:
            return
        self._filter_overrides.add((self._ref, table.dotted))
        self.statusBar().showMessage(
            f"Tenant filter disabled for {table.view_name} on {self._ref}.", 5000
        )
        if self._table is not None and self._table.dotted == table.dotted:
            self._run_table_work()

    # ----- preview / schema ----------------------------------------------------
    def _on_table_selected(self, table: TableInfo) -> None:
        self._table = table
        self._run_table_work()

    def _run_table_work(self) -> None:
        self._run_preview()
        self._run_schema()

    def _run_preview(self) -> None:
        session, ref, table = self._session, self._ref, self._table
        if session is None or ref is None or table is None or self._closed:
            return
        tenant_filter = self._tenant_filter_for(table)
        self._preview.show_loading(table)
        self._result_label.setText("Loading preview…")

        def run(cancel_token) -> QueryResult:
            return session.preview(
                ref, table.view_name, tenant_filter=tenant_filter, cancel_token=cancel_token
            )

        self._runner.submit(
            "preview",
            run,
            on_result=lambda result: self._on_preview_result(table, tenant_filter, result),
            on_error=lambda exc: self._on_preview_error(table, exc),
        )

    def _on_preview_result(
        self, table: TableInfo, tenant_filter: bool, result: QueryResult
    ) -> None:
        self._update_catalog_status()
        self._preview.show_result(table, result.df)
        self._browser.update_status(table.dotted, "registered", None)
        self._result_label.setText(self._summary(result, tenant_filter, table))
        if result.reloaded:
            self.statusBar().showMessage("Catalog changed — reloaded and retried.", 8000)

    def _summary(self, result: QueryResult, tenant_filter: bool, table: TableInfo) -> str:
        parts = [f"{result.row_count:,} rows", f"{result.elapsed:.2f} s"]
        if result.truncated:
            limit = self._session.profile.preview_row_limit if self._session else result.row_count
            parts.append(f"truncated to the preview limit ({limit:,} rows)")
        if self._ref is not None and self._session is not None:
            filterable = self._session.is_tenant_ref(self._ref) and not table.shared
            if filterable and not tenant_filter:
                parts.append("tenant filter off")
        return " · ".join(parts)

    def _on_preview_error(self, table: TableInfo, exc: BaseException) -> None:
        self._update_catalog_status()
        if isinstance(exc, QueryCancelled):
            self._preview.clear("Preview cancelled.")
            self._result_label.clear()
            self.statusBar().showMessage("Preview cancelled.", 5000)
            return
        self._preview.show_error(table, exc)
        self._result_label.setText("Preview failed")
        status = _status_for_error(exc)
        if status is not None:
            self._browser.update_status(table.dotted, status, user_error_text(exc))

    def _run_schema(self) -> None:
        session, ref, table = self._session, self._ref, self._table
        if session is None or ref is None or table is None or self._closed:
            return
        tenant_filter = self._tenant_filter_for(table)
        self._schema.show_loading(table)

        def run(cancel_token):
            return session.schema(
                ref, table.view_name, tenant_filter=tenant_filter, cancel_token=cancel_token
            )

        self._runner.submit(
            "schema",
            run,
            on_result=lambda cols: self._schema.show_columns(table, cols),
            on_error=lambda exc: self._on_schema_error(table, exc),
        )

    def _on_schema_error(self, table: TableInfo, exc: BaseException) -> None:
        if isinstance(exc, QueryCancelled):
            self._schema.clear("Cancelled.")
            return
        self._schema.show_error(table, exc)

    # ----- SQL tab ---------------------------------------------------------
    def _insert_view_name(self, view_name: str) -> None:
        self._sql.insert_text(view_name)
        self._tabs.setCurrentWidget(self._sql)

    # ----- query history (SPEC §9: SQL text only, never results) ---------------
    def _get_history(self) -> list:
        if self._active_profile is None:
            return []
        return self._history.list(self._active_profile.name)

    def _record_history(self, sql: str) -> None:
        if self._active_profile is None:
            return
        self._history.record(self._active_profile.name, sql, self._ref)

    def _clear_history(self) -> None:
        if self._active_profile is None:
            return
        self._history.clear(self._active_profile.name)
        self.statusBar().showMessage("Query history cleared.", 5000)

    # ----- CSV export (SPEC §9: gated by allow_export) --------------------------
    def _update_export_gate(self) -> None:
        profile = self._active_profile
        allowed = bool(profile is not None and profile.allow_export)
        tooltip = "" if allowed else EXPORT_DISABLED_TOOLTIP
        self._preview.set_export_allowed(allowed, tooltip)
        self._sql.results.set_export_allowed(allowed, tooltip)
        self._export_action.setEnabled(allowed)
        self._export_action.setToolTip(EXPORT_ENABLED_TOOLTIP if allowed else tooltip)

    def _export_current_tab(self) -> None:
        current = self._tabs.currentWidget()
        if current is self._preview:
            self._export_preview()
        elif current is self._sql:
            self._export_sql()

    def _export_preview(self) -> None:
        stem = self._table.view_name if self._table is not None else "query"
        self._start_export(lambda: self._preview.view.results_model.dataframe, stem)

    def _export_sql(self) -> None:
        self._start_export(lambda: self._sql.results.view.results_model.dataframe, "query")

    def _start_export(self, get_df: Callable[[], pd.DataFrame], default_stem: str) -> None:
        profile = self._active_profile
        if profile is None or not profile.allow_export:
            return
        df = get_df()
        if df is None or (df.empty and len(df.columns) == 0):
            self.statusBar().showMessage("Nothing to export.", 5000)
            return
        dest, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", f"{default_stem}.csv", "CSV files (*.csv)"
        )
        if not dest:
            return
        row_count = len(df)
        reply = QMessageBox.question(
            self,
            "Export CSV",
            f"Export {row_count:,} row(s) to:\n{dest}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        def do_export() -> int:
            return export_csv(df, dest, allow_export=profile.allow_export)

        self.statusBar().showMessage(f"Exporting {row_count:,} rows…", 5000)
        self._runner.submit(
            EXPORT_CHANNEL,
            do_export,
            replace=False,
            on_result=lambda rows: self.statusBar().showMessage(
                f"Exported {rows:,} row(s) to {dest}", 8000
            ),
            on_error=lambda exc: self.statusBar().showMessage(
                f"Export failed: {user_error_text(exc)}", 8000
            ),
        )

    # ----- keyboard shortcuts ---------------------------------------------------
    def _cancel_running_query(self) -> None:
        if self._sql.is_running:
            self._sql.cancel()

    def _focus_table_filter(self) -> None:
        self._browser.filter_edit.setFocus()
        self._browser.filter_edit.selectAll()

    # ----- diagnostics ---------------------------------------------------------
    def _copy_diagnostics(self) -> None:
        text = diagnostics.build_diagnostics(
            self.active_profile(),
            catalog_status=self._catalog_status,
            version=__version__,
            commit=__commit__,
        )
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage("Diagnostics copied to clipboard.", 5000)

    # ----- remembered state (SPEC M6: QSettings("tcookie", "Floe")) ------------
    def _restore_window_state(self) -> None:
        """Restore geometry, splitters and the last active tab. Profile / branch
        selection is restored separately (`_preferred_profile`, `_populate_branches`)
        since they depend on the profile store and the branch list being loaded."""
        settings = self._settings
        geometry = settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = settings.value("window/state")
        if state is not None:
            self.restoreState(state)
        main_sizes = _int_list(settings.value("splitter/main"))
        if main_sizes:
            self._splitter.setSizes(main_sizes)
        sql_sizes = _int_list(settings.value("splitter/sql"))
        if sql_sizes:
            self._sql.set_splitter_sizes(sql_sizes)
        tab_index = settings.value("tab/last")
        if tab_index is not None:
            try:
                index = int(tab_index)
            except (TypeError, ValueError):
                index = -1
            if 0 <= index < self._tabs.count():
                self._tabs.setCurrentIndex(index)

    def save_state(self) -> None:
        """Persist geometry, splitters, the active tab, and the current profile /
        branch / SQL text so the next launch can restore them (SPEC M6)."""
        settings = self._settings
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("window/state", self.saveState())
        settings.setValue("splitter/main", self._splitter.sizes())
        settings.setValue("splitter/sql", self._sql.splitter_sizes())
        settings.setValue("tab/last", self._tabs.currentIndex())
        if self._active_profile is not None:
            settings.setValue("profile/last", self._active_profile.name)
            settings.setValue(
                f"sql_text/{self._active_profile.name}", self._sql.editor.toPlainText()
            )
            if self._ref is not None:
                settings.setValue(f"branch/{self._active_profile.name}", self._ref)
        settings.sync()

    # ----- shutdown ------------------------------------------------------------
    def shutdown(self, timeout_ms: int = 3000) -> bool:
        """Cancel all background work (`interrupt_all`) and wait briefly for the pool."""
        if self._closed:
            return True
        self.save_state()
        self._closed = True
        if self._session is not None:
            self._session.interrupt_all()
        return self._runner.shutdown(timeout_ms)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self.shutdown()
        super().closeEvent(event)
