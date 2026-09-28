"""Table tree panel (SPEC §8.1, §5.4; PLAN "Resolved open questions" 4, 5).

Tables are grouped by key elements: layer (first element) → middle namespaces → table.
Layers are not hard-coded: bronze / silver / gold sort first, any other top-level
namespace follows alphabetically. Shared tables (the profile's `shared_namespaces`, read
from the main ref) go under one "Reference (main)" group. A filter box above the tree
matches table names and SQL view names (case-insensitive) and keeps the parents of matches.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QLineEdit,
    QMenu,
    QStackedWidget,
    QStyle,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from floe.core.context import TableInfo

REFERENCE_GROUP = "Reference (main)"
PREFERRED_LAYERS = ("bronze", "silver", "gold")

TABLE_ROLE = Qt.ItemDataRole.UserRole + 1  # TableInfo on table items, None on groups
SEARCH_ROLE = Qt.ItemDataRole.UserRole + 2  # text the filter matches ("" on groups)
KIND_ROLE = Qt.ItemDataRole.UserRole + 3  # "group" | "table"

# Status → (QStyle standard icon, short label). Statuses without an entry have no icon.
_STATUS_ICONS: dict[str, tuple[QStyle.StandardPixmap, str]] = {
    "not_found": (QStyle.StandardPixmap.SP_MessageBoxQuestion, "Not found"),
    "corrupt": (QStyle.StandardPixmap.SP_MessageBoxWarning, "Corrupt pointer"),
    "scope_error": (QStyle.StandardPixmap.SP_MessageBoxCritical, "Outside its container"),
    "missing_data_source": (
        QStyle.StandardPixmap.SP_MessageBoxInformation,
        "No data_source column",
    ),
    "error": (QStyle.StandardPixmap.SP_MessageBoxCritical, "Error"),
}


def layer_sort_key(name: str) -> tuple[int, str]:
    """bronze, silver, gold first (in that order), then everything else alphabetically."""
    lowered = name.lower()
    if lowered in PREFERRED_LAYERS:
        return (PREFERRED_LAYERS.index(lowered), lowered)
    return (len(PREFERRED_LAYERS), lowered)


def table_tooltip(info: TableInfo) -> str:
    lines = [f"SQL view: {info.view_name}", f"Key: {info.dotted}"]
    if info.shared:
        lines.append(f"Resolved from ref: {info.source_ref}")
    if info.status in _STATUS_ICONS:
        label = _STATUS_ICONS[info.status][1]
        lines.append(f"{label}: {info.error}" if info.error else label)
    return "\n".join(lines)


class _TreeFilter(QSortFilterProxyModel):
    """Recursive, case-insensitive substring filter on `SEARCH_ROLE`; groups sort first."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRecursiveFilteringEnabled(True)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterRole(SEARCH_ROLE)


class BrowserPanel(QWidget):
    """Filter box + table tree. Emits `tableSelected(TableInfo)` for table items."""

    tableSelected = Signal(object)  # noqa: N815 - Qt signal naming
    # Emitted with a view name (str) to insert into the SQL editor: double-click on a
    # table, or its context menu's "Insert view name" (small SQL-tab convenience).
    insertViewNameRequested = Signal(str)  # noqa: N815 - Qt signal naming

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter tables…")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)

        self._model = QStandardItemModel(self)
        self._proxy = _TreeFilter(self)
        self._proxy.setSourceModel(self._model)

        self._tree = QTreeView()
        self._tree.setModel(self._proxy)
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.selectionModel().currentChanged.connect(self._on_current_changed)
        self._tree.doubleClicked.connect(self._on_double_clicked)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._show_context_menu)

        self._message = QLabel()
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._tree)
        self._stack.addWidget(self._message)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._filter)
        layout.addWidget(self._stack, stretch=1)

        self._items: dict[str, QStandardItem] = {}  # dotted key → table item
        self._state = "empty"
        self.show_message("No branch selected.")

    # ----- accessors (also used by tests) ----------------------------------
    @property
    def tree(self) -> QTreeView:
        return self._tree

    @property
    def filter_edit(self) -> QLineEdit:
        return self._filter

    @property
    def source_model(self) -> QStandardItemModel:
        return self._model

    @property
    def state(self) -> str:
        """"loading" | "ready" | "empty" | "error"."""
        return self._state

    @property
    def message(self) -> str:
        return self._message.text()

    def table_item(self, dotted: str) -> QStandardItem | None:
        return self._items.get(dotted)

    def top_level_names(self) -> list[str]:
        return [self._model.item(r).text() for r in range(self._model.rowCount())]

    def visible_table_names(self) -> list[str]:
        """View names of the table items that pass the filter."""
        out: list[str] = []

        def walk(parent: QModelIndex) -> None:
            for r in range(self._proxy.rowCount(parent)):
                idx = self._proxy.index(r, 0, parent)
                info = idx.data(TABLE_ROLE)
                if info is not None:
                    out.append(info.view_name)
                walk(idx)

        walk(QModelIndex())
        return out

    # ----- states ----------------------------------------------------------
    def show_message(self, text: str, state: str = "empty") -> None:
        self._state = state
        self._message.setText(text)
        self._stack.setCurrentWidget(self._message)

    def show_loading(self, text: str = "Loading tables…") -> None:
        self.clear()
        self.show_message(text, "loading")

    def show_error(self, text: str) -> None:
        self.clear()
        self.show_message(text, "error")

    def clear(self) -> None:
        self._tree.selectionModel().clearCurrentIndex()
        self._model.clear()
        self._items.clear()

    # ----- population ------------------------------------------------------
    def set_tables(self, tables: Iterable[TableInfo]) -> None:
        """Rebuild the tree from a `FloeSession.list_tables` result."""
        tables = list(tables)
        self.clear()
        if not tables:
            self.show_message("No tables on this branch.", "empty")
            return
        own = [t for t in tables if not t.shared]
        shared = [t for t in tables if t.shared]
        root = self._model.invisibleRootItem()
        self._add_grouped(root, own)
        if shared:
            group = self._group_item(REFERENCE_GROUP)
            group.setToolTip(
                "Shared reference tables, read from the main ref's head. "
                "Their views work in SQL from any branch."
            )
            root.appendRow(group)
            self._add_grouped(group, shared)
        self._state = "ready"
        self._stack.setCurrentWidget(self._tree)
        self._apply_filter(self._filter.text())

    def _add_grouped(self, parent: QStandardItem, tables: list[TableInfo]) -> None:
        # nested dict: element → subtree; tables stored under the "" key of their parent.
        tree: dict = {}
        for info in tables:
            node = tree
            for element in info.key.elements[:-1]:
                node = node.setdefault(element, {})
            node.setdefault("", []).append(info)
        self._fill(parent, tree, top=True)

    def _fill(self, parent: QStandardItem, node: dict, top: bool) -> None:
        groups = [k for k in node if k != ""]
        groups.sort(key=layer_sort_key if top else str.lower)
        for name in groups:
            item = self._group_item(name)
            parent.appendRow(item)
            self._fill(item, node[name], top=False)
        for info in sorted(node.get("", []), key=lambda i: i.key.elements[-1].lower()):
            item = self._table_item(info)
            parent.appendRow(item)
            self._items[info.dotted] = item

    @staticmethod
    def _group_item(name: str) -> QStandardItem:
        item = QStandardItem(name)
        item.setEditable(False)
        item.setSelectable(False)
        item.setData("", SEARCH_ROLE)
        item.setData("group", KIND_ROLE)
        return item

    def _table_item(self, info: TableInfo) -> QStandardItem:
        item = QStandardItem(info.key.elements[-1])
        item.setEditable(False)
        item.setData("table", KIND_ROLE)
        item.setData(f"{info.key.elements[-1]}\n{info.view_name}", SEARCH_ROLE)
        self._apply_status(item, info)
        return item

    def _apply_status(self, item: QStandardItem, info: TableInfo) -> None:
        item.setData(info, TABLE_ROLE)
        item.setToolTip(table_tooltip(info))
        entry = _STATUS_ICONS.get(info.status)
        if entry is None:
            item.setIcon(QIcon())
        else:
            item.setIcon(self.style().standardIcon(entry[0]))

    def update_status(self, dotted: str, status: str, error: str | None) -> None:
        """Update one table's status (e.g. after a preview failed or succeeded)."""
        item = self._items.get(dotted)
        if item is None:
            return
        info: TableInfo = item.data(TABLE_ROLE)
        if info.status == status and info.error == error:
            return
        self._apply_status(item, replace(info, status=status, error=error))

    def status_of(self, dotted: str) -> str | None:
        item = self._items.get(dotted)
        return None if item is None else item.data(TABLE_ROLE).status

    # ----- filter / selection ----------------------------------------------
    def _apply_filter(self, text: str) -> None:
        self._proxy.setFilterFixedString(text.strip())
        if text.strip():
            self._tree.expandAll()
        else:
            self._tree.collapseAll()
            # Top-level groups open by default so the layers' contents are visible.
            for r in range(self._proxy.rowCount()):
                self._tree.expand(self._proxy.index(r, 0))

    def select_table(self, dotted: str) -> bool:
        """Select a table by dotted key (emits `tableSelected`). False if not visible."""
        item = self._items.get(dotted)
        if item is None:
            return False
        idx = self._proxy.mapFromSource(item.index())
        if not idx.isValid():
            return False
        self._tree.scrollTo(idx)
        self._tree.setCurrentIndex(idx)
        return True

    def selected_table(self) -> TableInfo | None:
        idx = self._tree.currentIndex()
        return idx.data(TABLE_ROLE) if idx.isValid() else None

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            return
        info = current.data(TABLE_ROLE)
        if info is not None:
            self.tableSelected.emit(info)

    def _on_double_clicked(self, index: QModelIndex) -> None:
        info = index.data(TABLE_ROLE)
        if info is not None:
            self.insertViewNameRequested.emit(info.view_name)

    def _show_context_menu(self, pos) -> None:
        index = self._tree.indexAt(pos)
        info = index.data(TABLE_ROLE) if index.isValid() else None
        if info is None:
            return
        menu = QMenu(self)
        action = menu.addAction("Insert view name")
        action.triggered.connect(lambda: self.insertViewNameRequested.emit(info.view_name))
        menu.exec(self._tree.viewport().mapToGlobal(pos))
