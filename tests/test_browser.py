"""Phase 4 browser UI tests (SPEC §8.1, §8.4, §8.5): pytest-qt, offscreen.

Local-mode windows run over the parquet fixtures; one remote-style window runs against the
in-process fake Nessie + pyiceberg fixtures (no Azure: `skip_storage_setup`).
"""

from __future__ import annotations

import threading

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QSettings, Qt
from PySide6.QtWidgets import QApplication

from floe.core.context import FloeSession, QueryResult
from floe.core.nessie import NessieClient
from floe.core.profiles import Profile, ProfileStore
from floe.ui.browser_panel import REFERENCE_GROUP, TABLE_ROLE, layer_sort_key
from floe.ui.main_window import INACTIVE_COLOR, NOT_APPLICABLE_ON_MAIN, MainWindow
from floe.ui.profile_dialog import ProfileDialog
from floe.ui.results_model import NULL_COLOR, NULL_TEXT, DataFrameModel
from floe.ui.results_view import ResultsView
from floe.ui.workers import TaskRunner
from tests.fakes.fake_nessie import DEFAULT_CLIENT_SECRET
from tests.fakes.iceberg_fixtures import (
    REGISTRY_KEY,
    SHARED_CONTAINERS,
    SHARED_NAMESPACES,
    seed_fake_nessie,
)

pytest.importorskip("pytestqt")

WAIT = 15000  # ms: generous upper bound, never an expected duration


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def store(tmp_path) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles.json")


def local_profile(fx, **overrides) -> Profile:
    values = dict(
        name="eg-local",
        mode="local",
        local_fixture_dir=str(fx.local_root),
        shared_containers=list(SHARED_CONTAINERS),
        shared_namespaces=list(SHARED_NAMESPACES),
        tenant_registry_table=REGISTRY_KEY,
        preview_row_limit=4,
    )
    values.update(overrides)
    return Profile(**values)


@pytest.fixture
def isolated_settings(tmp_path):
    """A `QSettings` backed by a private ini file — never the real user settings."""
    return QSettings(str(tmp_path / "floe-settings.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def open_window(qtbot, store, isolated_settings):
    windows: list[MainWindow] = []

    def open_(factory=None, settings=None) -> MainWindow:
        window = MainWindow(
            store, session_factory=factory, settings=settings or isolated_settings
        )
        qtbot.addWidget(window)
        windows.append(window)
        return window

    yield open_
    for window in windows:
        assert window.shutdown(5000)


def wait_tree(qtbot, window: MainWindow) -> None:
    qtbot.waitUntil(lambda: window.browser.state == "ready", timeout=WAIT)


def wait_preview(qtbot, window: MainWindow, state: str = "ready") -> None:
    qtbot.waitUntil(lambda: window.preview_tab.state == state, timeout=WAIT)


def children(item) -> list[str]:
    return [item.child(r).text() for r in range(item.rowCount())]


def top_item(window: MainWindow, name: str):
    model = window.browser.source_model
    for r in range(model.rowCount()):
        if model.item(r).text() == name:
            return model.item(r)
    raise AssertionError(f"no top-level item {name!r}")


@pytest.fixture
def local_window(fx_local, open_window, store, qtbot):
    store.save(fx_local)
    window = open_window()
    qtbot.waitUntil(lambda: window.branch_combo.count() == 3, timeout=WAIT)
    wait_tree(qtbot, window)
    assert window.select_branch("eg-test1")
    wait_tree(qtbot, window)
    return window


@pytest.fixture
def fx_local(iceberg_fixtures):
    return local_profile(iceberg_fixtures)


# --------------------------------------------------------------------------- local mode


def test_local_window_branches_and_tree(local_window, qtbot):
    window = local_window
    combo = window.branch_combo
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert texts == ["eg-c3", "eg-test1", "eg-test2 (inactive)"]
    assert combo.itemData(2, Qt.ItemDataRole.ForegroundRole).color() == INACTIVE_COLOR
    assert combo.itemData(1, Qt.ItemDataRole.ForegroundRole) is None

    assert window.current_ref == "eg-test1"
    assert window.browser.top_level_names() == ["bronze", "silver", "gold", REFERENCE_GROUP]
    silver = top_item(window, "silver")
    assert children(silver) == ["input_layer"]
    assert children(silver.child(0)) == ["medical_claim", "no_ds"]
    reference = top_item(window, REFERENCE_GROUP)
    assert children(reference) == ["gold_ref", "registry"]
    codes = window.browser.table_item("gold_ref.codes")
    assert "gold_ref_codes" in codes.toolTip()
    assert "Resolved from ref" in codes.toolTip()
    assert "silver_input_layer_medical_claim" in window.browser.table_item(
        "silver.input_layer.medical_claim"
    ).toolTip()

    # Local mode: no head hash, catalog label says so, tenant filter applies.
    assert window.head_button.text() == "local" and not window.head_button.isEnabled()
    assert "Local mode" in window.catalog_label.text()
    assert window.tenant_filter_checkbox.isEnabled()

    # An inactive branch is still selectable.
    assert window.select_branch("eg-test2")
    wait_tree(qtbot, window)
    assert window.current_ref == "eg-test2"
    assert "bronze" in window.browser.top_level_names()


def test_layer_order():
    names = ["zeta", "Gold", "alpha", "silver", "bronze"]
    assert sorted(names, key=layer_sort_key) == ["bronze", "silver", "Gold", "alpha", "zeta"]


def test_filter_box(local_window):
    browser = local_window.browser
    everything = sorted(browser.visible_table_names())
    assert len(everything) == 6
    browser.filter_edit.setText("CLAIM")
    assert browser.visible_table_names() == ["silver_input_layer_medical_claim"]
    browser.filter_edit.setText("gold_ref_c")  # view-name match
    assert browser.visible_table_names() == ["gold_ref_codes"]
    browser.filter_edit.setText("gold")  # layer name alone isn't a table match, view names are
    assert sorted(browser.visible_table_names()) == ["gold_ref_codes", "gold_summary"]
    browser.filter_edit.setText("nothing-matches")
    assert browser.visible_table_names() == []
    browser.filter_edit.clear()
    assert sorted(browser.visible_table_names()) == everything


def test_preview_and_schema(local_window, qtbot):
    window = local_window
    assert window.browser.select_table("bronze.medical")
    wait_preview(qtbot, window)
    model = window.preview_tab.view.results_model
    assert model.rowCount() == 3  # tenant filter on: 3 of 5 rows
    assert set(model.dataframe["data_source"]) == {"eg-test1"}
    assert "3 rows" in window.result_label.text()
    assert "truncated" not in window.result_label.text()
    qtbot.waitUntil(lambda: window.schema_tab.state == "ready", timeout=WAIT)
    schema = window.schema_tab.view.results_model.dataframe
    assert list(schema["column"]) == ["id", "amount", "code", "data_source"]
    assert list(schema.columns) == ["column", "type", "nullable"]
    assert schema.loc[0, "type"] == "BIGINT"
    assert window.browser.status_of("bronze.medical") == "registered"

    # Tenant filter off: 5 rows, truncated at the preview limit (4).
    window.tenant_filter_checkbox.setChecked(False)
    qtbot.waitUntil(lambda: model.rowCount() == 4, timeout=WAIT)
    wait_preview(qtbot, window)
    assert "truncated" in window.result_label.text()
    assert "tenant filter off" in window.result_label.text()
    window.tenant_filter_checkbox.setChecked(True)
    qtbot.waitUntil(lambda: model.rowCount() == 3, timeout=WAIT)


def test_missing_data_source_offers_to_disable_filter(local_window, qtbot):
    window = local_window
    window.browser.select_table("silver.input_layer.no_ds")
    wait_preview(qtbot, window, "error")
    preview = window.preview_tab
    assert "data_source" in preview.message_text
    assert not preview.disable_filter_button.isHidden()
    assert window.browser.status_of("silver.input_layer.no_ds") == "missing_data_source"
    assert not window.browser.table_item("silver.input_layer.no_ds").icon().isNull()

    qtbot.mouseClick(preview.disable_filter_button, Qt.MouseButton.LeftButton)
    wait_preview(qtbot, window)
    assert preview.view.results_model.rowCount() == 2
    assert preview.disable_filter_button.isHidden()
    assert "tenant filter off" in window.result_label.text()
    assert window.browser.status_of("silver.input_layer.no_ds") == "registered"


# --------------------------------------------------------------------------- stale results


class SlowSession(FloeSession):
    """Preview blocks until `gate` is set, then returns a result even if cancelled."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.started = threading.Event()
        self.gate = threading.Event()
        self.finished = threading.Event()

    def preview(self, ref, view_name, tenant_filter=True, *, cancel_token=None) -> QueryResult:
        self.started.set()
        try:
            self.gate.wait(10)
            return super().preview(ref, view_name, tenant_filter)
        finally:
            self.finished.set()


def test_switching_branch_mid_preview_drops_stale_result(open_window, store, fx_local, qtbot):
    store.save(fx_local)
    sessions: list[SlowSession] = []

    def factory(profile):
        sessions.append(SlowSession(profile))
        return sessions[-1]

    window = open_window(factory)
    wait_tree(qtbot, window)
    window.select_branch("eg-test1")
    wait_tree(qtbot, window)
    session = sessions[-1]

    window.browser.select_table("bronze.medical")
    assert session.started.wait(10)
    workers = window.runner.active("preview")
    assert len(workers) == 1
    worker = workers[0]
    assert window.preview_tab.state == "loading"

    window.select_branch("eg-test2")
    assert worker.cancel_token.cancelled  # in-flight preview work was cancelled
    session.gate.set()
    assert session.finished.wait(10)
    qtbot.waitUntil(lambda: not window.runner.active("preview"), timeout=WAIT)
    wait_tree(qtbot, window)
    # The old branch's preview result arrived but was dropped.
    assert window.preview_tab.state == "empty"
    assert window.preview_tab.view.results_model.rowCount() == 0
    assert window.current_ref == "eg-test2"


def test_task_runner_generations(qtbot):
    runner = TaskRunner()
    results: list = []
    gate = threading.Event()
    try:
        runner.submit("c", lambda: (gate.wait(10), "old")[1], on_result=results.append)
        runner.submit("c", lambda: "new", on_result=results.append)  # replaces the first
        gate.set()
        qtbot.waitUntil(lambda: results == ["new"] and not runner.active(), timeout=WAIT)

        seen: list = []
        worker = runner.submit(
            "d", lambda cancel_token: cancel_token, on_result=seen.append, replace=False
        )
        qtbot.waitUntil(lambda: bool(seen), timeout=WAIT)
        assert seen[0] is worker.cancel_token

        errors: list = []
        runner.submit("e", lambda: 1 / 0, on_error=errors.append)
        qtbot.waitUntil(lambda: bool(errors), timeout=WAIT)
        assert isinstance(errors[0], ZeroDivisionError)

        late: list = []
        gate2 = threading.Event()
        w = runner.submit("f", lambda: gate2.wait(10), on_result=late.append)
        runner.invalidate()  # e.g. profile switch
        assert w.cancel_token.cancelled
        gate2.set()
        qtbot.waitUntil(lambda: not runner.active(), timeout=WAIT)
        assert late == []
    finally:
        gate.set()
        assert runner.shutdown(5000)
    with pytest.raises(RuntimeError):
        runner.submit("c", lambda: None)


def test_close_cancels_running_preview(open_window, store, fx_local, qtbot):
    store.save(fx_local)
    sessions: list[SlowSession] = []
    window = open_window(lambda p: sessions.append(SlowSession(p)) or sessions[-1])
    wait_tree(qtbot, window)
    window.browser.select_table("gold.summary")
    assert sessions[-1].started.wait(10)
    worker = window.runner.active("preview")[0]
    sessions[-1].gate.set()
    window.close()
    assert worker.cancel_token.cancelled
    assert window.runner.pool.activeThreadCount() == 0


# --------------------------------------------------------------------------- remote (fake Nessie)


@pytest.fixture
def remote_window(
    fake_nessie, iceberg_fixtures, iceberg_unavailable_reason, storage_extensions_available,
    open_window, store, qtbot,
):
    if iceberg_unavailable_reason:
        pytest.skip(iceberg_unavailable_reason)
    fx = iceberg_fixtures
    seed_fake_nessie(fake_nessie, fx)
    fake_nessie.add_table(
        "eg-test1", "gold.broken", fx.location("eg-test1", "gold.summary"), snapshot_id=-1
    )
    profile = fake_nessie.profile(
        name="eg-remote",
        shared_containers=list(SHARED_CONTAINERS),
        shared_namespaces=list(SHARED_NAMESPACES),
        tenant_registry_table=REGISTRY_KEY,
        nessie_head_ttl_seconds=0,
    )
    store.save(profile)

    def factory(p: Profile) -> FloeSession:
        return FloeSession(
            p,
            {"nessie_client_secret": DEFAULT_CLIENT_SECRET},
            NessieClient(p, DEFAULT_CLIENT_SECRET, timeout=5),
            local_storage_root=fx.iceberg_root,
            skip_storage_setup=not storage_extensions_available,
        )

    window = open_window(factory)
    qtbot.waitUntil(lambda: window.branch_combo.count() == 4, timeout=WAIT)
    wait_tree(qtbot, window)
    return window, fake_nessie


def test_remote_branches_tree_and_head(remote_window, qtbot):
    window, fake = remote_window
    combo = window.branch_combo
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert texts == ["eg-test1", "eg-test2 (inactive)", "eg-test3", "main"]
    assert combo.itemData(1, Qt.ItemDataRole.ForegroundRole).color() == INACTIVE_COLOR
    assert window.current_ref == "eg-test1"
    assert window.browser.top_level_names() == ["bronze", "silver", "gold", REFERENCE_GROUP]
    assert children(top_item(window, "gold")) == ["broken", "summary"]

    head = fake.state.branches["eg-test1"]
    assert window.head_hash == head
    assert window.head_button.text() == head[:8]
    qtbot.mouseClick(window.head_button, Qt.MouseButton.LeftButton)
    assert QApplication.clipboard().text() == head
    assert "Copied head hash" in window.statusBar().currentMessage()
    assert "Catalog OK" in window.catalog_label.text()
    assert window.tenant_filter_checkbox.isEnabled()

    # main: tenant filter not applicable.
    window.select_branch("main")
    wait_tree(qtbot, window)
    assert not window.tenant_filter_checkbox.isEnabled()
    assert window.tenant_filter_checkbox.toolTip() == NOT_APPLICABLE_ON_MAIN
    assert window.browser.top_level_names() == [REFERENCE_GROUP]
    assert children(top_item(window, REFERENCE_GROUP)) == ["gold_ref", "registry"]
    assert window.head_button.text() == fake.state.branches["main"][:8]


def test_main_branch_shared_tables_not_duplicated(remote_window, qtbot, iceberg_fixtures):
    """A main-ref table under a non-shared namespace must not also show under Reference,
    and a shared-namespace table must show only under Reference (main), never twice."""
    window, fake = remote_window
    fx = iceberg_fixtures
    fake.add_table(
        "main",
        "audit.notes",
        metadata_location=fx.location("main", "gold_ref.codes"),
        snapshot_id=fx.snapshot_ids[("main", "gold_ref.codes")],
    )
    window.select_branch("main")
    wait_tree(qtbot, window)
    assert window.browser.top_level_names() == ["audit", REFERENCE_GROUP]
    assert children(top_item(window, "audit")) == ["notes"]
    assert children(top_item(window, REFERENCE_GROUP)) == ["gold_ref", "registry"]


def test_remote_preview_and_status_icons(remote_window, qtbot):
    window, fake = remote_window
    browser = window.browser
    assert browser.select_table("bronze.medical")
    wait_preview(qtbot, window)
    assert window.preview_tab.view.results_model.rowCount() == 3

    # Corrupt pointer (snapshotId == -1): inline error + warning icon.
    browser.select_table("gold.broken")
    wait_preview(qtbot, window, "error")
    assert "corrupt" in window.preview_tab.message_text
    assert browser.status_of("gold.broken") == "corrupt"
    assert not browser.table_item("gold.broken").icon().isNull()
    assert "Corrupt pointer" in browser.table_item("gold.broken").toolTip()

    # Removed after listing: not found.
    fake.remove_table("eg-test1", "gold.summary")
    browser.select_table("gold.summary")
    wait_preview(qtbot, window, "error")
    assert "not found" in window.preview_tab.message_text.lower()
    assert browser.status_of("gold.summary") == "not_found"

    # Shared table from main on a tenant branch.
    browser.select_table("gold_ref.codes")
    wait_preview(qtbot, window)
    assert window.preview_tab.view.results_model.rowCount() == 4


def test_remote_refresh_and_stale_catalog(remote_window, qtbot):
    window, fake = remote_window
    old = window.head_hash
    new = fake.move_head("eg-test1")
    window.browser.select_table("bronze.medical")
    wait_preview(qtbot, window)
    window.refresh()
    wait_tree(qtbot, window)
    assert window.head_hash == new != old
    # The selection survives a refresh.
    qtbot.waitUntil(lambda: window.preview_tab.table is not None, timeout=WAIT)
    assert window.preview_tab.table.dotted == "bronze.medical"
    wait_preview(qtbot, window)

    # Nessie down: the last known head is kept and the indicator says so.
    fake.fail_next(50, 503, path_prefix="/api/v2/trees/eg-test1")
    window.refresh()
    wait_tree(qtbot, window)
    assert window.head_hash == new
    qtbot.waitUntil(lambda: window.catalog_status == "stale", timeout=WAIT)
    assert "unreachable" in window.catalog_label.text()


# --------------------------------------------------------------------------- results model


def test_dataframe_model_display_and_null():
    df = pd.DataFrame({"a": [1, None, 3], "b": ["x", None, "z"]})
    model = DataFrameModel(df)
    assert model.rowCount() == 3 and model.columnCount() == 2
    assert model.headerData(1, Qt.Orientation.Horizontal) == "b"
    assert model.headerData(0, Qt.Orientation.Vertical) == "1"
    assert model.data(model.index(0, 0)) == "1.0"
    assert model.data(model.index(1, 0)) == NULL_TEXT
    assert model.data(model.index(1, 1)) == NULL_TEXT
    assert model.data(model.index(1, 1), Qt.ItemDataRole.ForegroundRole) == NULL_COLOR
    assert model.data(model.index(0, 1), Qt.ItemDataRole.ForegroundRole) is None
    assert model.data(model.index(0, 1)) == "x"


def test_dataframe_model_sort_stable_nulls_last():
    df = pd.DataFrame(
        {
            "k": [2.0, 1.0, np.nan, 2.0, 1.0],
            "tag": ["a", "b", "c", "d", "e"],
        }
    )
    model = DataFrameModel(df)
    model.sort(0, Qt.SortOrder.AscendingOrder)
    assert list(model.dataframe["tag"]) == ["b", "e", "a", "d", "c"]
    model.sort(0, Qt.SortOrder.DescendingOrder)
    assert list(model.dataframe["tag"]) == ["a", "d", "b", "e", "c"]
    model.sort(-1)
    assert list(model.dataframe["tag"]) == ["a", "b", "c", "d", "e"]
    # Mixed types fall back to text order without raising.
    mixed = DataFrameModel(pd.DataFrame({"m": [3, "b", None, 1.5]}))
    mixed.sort(0, Qt.SortOrder.AscendingOrder)
    assert mixed.data(mixed.index(3, 0)) == NULL_TEXT


def test_dataframe_model_tsv():
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x\ty", None, "z"], "c": [True, False, True]})
    model = DataFrameModel(df)
    idx = [model.index(0, 0), model.index(0, 1), model.index(1, 1), model.index(2, 2)]
    assert model.to_tsv(idx) == "1\tx y\t\n\t\t\n\t\tTrue"
    assert model.to_tsv([model.index(1, 0), model.index(1, 1)], include_header=True) == (
        "a\tb\n2\t"
    )
    assert model.to_tsv([]) == ""


def test_results_view_copy_shortcut(qtbot):
    view = ResultsView()
    qtbot.addWidget(view)
    view.set_dataframe(pd.DataFrame({"a": [1, 2], "b": ["p", "q"]}))
    model = view.results_model
    selection = QItemSelection(model.index(0, 0), model.index(1, 1))
    view.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.Select)
    QApplication.clipboard().clear()
    qtbot.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "1\tp\n2\tq"
    assert view.copy_selection(include_header=True) == "a\tb\n1\tp\n2\tq"
    assert view.isSortingEnabled()
    # Clicking a header sorts; new data resets to the original order.
    view.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert list(model.dataframe["b"]) == ["q", "p"]
    view.set_dataframe(pd.DataFrame({"a": [2, 1]}))
    assert list(model.dataframe["a"]) == [2, 1]
    assert all(60 <= view.columnWidth(c) <= 320 for c in range(model.columnCount()))


# --------------------------------------------------------------------------- restrict_file_access


def test_profile_dialog_restrict_file_access(qtbot, store, tmp_path):
    store.save(Profile(name="eg-p", mode="local", local_fixture_dir=str(tmp_path)))
    dialog = ProfileDialog(store)
    qtbot.addWidget(dialog)
    dialog._reload_list(select="eg-p")
    box = dialog._widgets["restrict_file_access"]
    assert box.isChecked()
    assert "Access denied" in box.toolTip()
    box.setChecked(False)
    dialog._on_save()
    assert store.get("eg-p").restrict_file_access is False
    assert Profile.from_dict({"name": "x"}).restrict_file_access is True


def test_tree_items_carry_table_info(local_window):
    item = local_window.browser.table_item("bronze.medical")
    info = item.data(TABLE_ROLE)
    assert info.view_name == "bronze_medical" and not info.shared


def test_no_profiles_shows_hint(open_window, qtbot):
    window = open_window()
    assert window.browser.state == "empty"
    assert "Edit profiles" in window.browser.message
    assert not window.runner.active()
