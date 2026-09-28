"""Phase 6 polish tests (SPEC §9, §14 M6): pytest, offscreen for the Qt parts.

Covers gated CSV export, query history (SQL text only), keyboard shortcuts, and
remembered window/profile/branch/SQL-editor state via an isolated `QSettings`.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QMessageBox

from floe.core.export import ExportNotAllowed, export_csv
from floe.core.history import MAX_ENTRIES, HistoryStore
from floe.core.profiles import ProfileStore
from floe.ui.main_window import EXPORT_DISABLED_TOOLTIP, MainWindow
from tests.test_browser import (  # noqa: F401
    fx_local,
    isolated_settings,
    local_profile,
    local_window,
    open_window,
    store,
    wait_tree,
)
from tests.test_sql_tab import SLOW_SQL

pytest.importorskip("pytestqt")

WAIT = 15000  # ms: generous upper bound, never an expected duration


# --------------------------------------------------------------------------- core: export


def test_export_csv_refuses_when_not_allowed(tmp_path):
    df = pd.DataFrame({"a": [1, 2, 3]})
    dest = tmp_path / "out.csv"
    with pytest.raises(ExportNotAllowed):
        export_csv(df, dest, allow_export=False)
    assert not dest.exists()


def test_export_csv_writes_when_allowed(tmp_path):
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    dest = tmp_path / "out.csv"
    row_count = export_csv(df, dest, allow_export=True)
    assert row_count == 3
    written = pd.read_csv(dest)
    assert written.equals(df)


# --------------------------------------------------------------------------- core: history


def test_history_records_dedupes_and_caps(tmp_path):
    hstore = HistoryStore(tmp_path / "history.json")
    hstore.record("eg-p", "SELECT 1", "main")
    hstore.record("eg-p", "SELECT 2", "main")
    hstore.record("eg-p", "SELECT 1", "main")  # re-run: moves to front, no duplicate
    entries = hstore.list("eg-p")
    assert [e.sql for e in entries] == ["SELECT 1", "SELECT 2"]

    for i in range(MAX_ENTRIES + 50):
        hstore.record("eg-p", f"SELECT {i}", "main")
    entries = hstore.list("eg-p")
    assert len(entries) == MAX_ENTRIES
    assert entries[0].sql == f"SELECT {MAX_ENTRIES + 49}"


def test_history_per_profile_and_never_stores_results(tmp_path):
    hstore = HistoryStore(tmp_path / "history.json")
    hstore.record("eg-a", "SELECT a", "ref-a")
    hstore.record("eg-b", "SELECT b", "ref-b")
    assert [e.sql for e in hstore.list("eg-a")] == ["SELECT a"]
    assert [e.sql for e in hstore.list("eg-b")] == ["SELECT b"]

    raw = json.loads(hstore.path.read_text(encoding="utf-8"))
    for entries in raw.values():
        for entry in entries:
            assert set(entry) == {"sql", "branch", "timestamp"}


def test_history_persists_across_instances_and_clears(tmp_path):
    path = tmp_path / "history.json"
    HistoryStore(path).record("eg-p", "SELECT 1", None)
    reopened = HistoryStore(path)
    assert [e.sql for e in reopened.list("eg-p")] == ["SELECT 1"]
    reopened.clear("eg-p")
    assert HistoryStore(path).list("eg-p") == []


def test_history_ignores_blank_sql(tmp_path):
    hstore = HistoryStore(tmp_path / "history.json")
    hstore.record("eg-p", "   ", None)
    assert hstore.list("eg-p") == []


# --------------------------------------------------------------------------- UI: export gate


def test_export_disabled_when_profile_does_not_allow_it(local_window):
    window = local_window
    assert not window.export_action.isEnabled()
    assert not window.preview_tab.export_button.isEnabled()
    assert not window.sql_tab.results.export_button.isEnabled()
    assert window.preview_tab.export_button.toolTip() == EXPORT_DISABLED_TOOLTIP


@pytest.fixture
def export_profile(iceberg_fixtures):
    return local_profile(iceberg_fixtures, name="eg-export", allow_export=True)


@pytest.fixture
def export_window(export_profile, open_window, store, qtbot):  # noqa: F811
    store.save(export_profile)
    window = open_window()
    qtbot.waitUntil(lambda: window.branch_combo.count() == 3, timeout=WAIT)
    wait_tree(qtbot, window)
    assert window.select_branch("eg-test1")
    wait_tree(qtbot, window)
    return window


def test_export_enabled_when_profile_allows_it(export_window):
    window = export_window
    assert window.export_action.isEnabled()


def test_export_flow_writes_csv_after_confirmation(export_window, qtbot, tmp_path, monkeypatch):
    window = export_window
    assert window.browser.select_table("bronze.medical")
    qtbot.waitUntil(lambda: window.preview_tab.state == "ready", timeout=WAIT)
    assert window.preview_tab.export_button.isEnabled()
    row_count = window.preview_tab.view.results_model.rowCount()

    dest = tmp_path / "out.csv"
    monkeypatch.setattr(
        "floe.ui.main_window.QFileDialog.getSaveFileName",
        lambda *a, **k: (str(dest), "CSV files (*.csv)"),
    )
    monkeypatch.setattr(
        "floe.ui.main_window.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.Yes,
    )
    qtbot.mouseClick(window.preview_tab.export_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: dest.exists(), timeout=WAIT)
    qtbot.waitUntil(lambda: "Exported" in window.statusBar().currentMessage(), timeout=WAIT)
    written = pd.read_csv(dest)
    assert len(written) == row_count


def test_export_flow_writes_nothing_when_declined(export_window, qtbot, tmp_path, monkeypatch):
    window = export_window
    assert window.browser.select_table("bronze.medical")
    qtbot.waitUntil(lambda: window.preview_tab.state == "ready", timeout=WAIT)

    dest = tmp_path / "out.csv"
    monkeypatch.setattr(
        "floe.ui.main_window.QFileDialog.getSaveFileName",
        lambda *a, **k: (str(dest), "CSV files (*.csv)"),
    )
    monkeypatch.setattr(
        "floe.ui.main_window.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.No,
    )
    qtbot.mouseClick(window.preview_tab.export_button, Qt.MouseButton.LeftButton)
    assert not dest.exists()


# --------------------------------------------------------------------------- UI: history


def test_sql_success_recorded_and_failure_not(local_window, qtbot):
    window = local_window
    window.tabs.setCurrentWidget(window.sql_tab)
    window.sql_tab.editor.setPlainText("SELECT 1 AS a")
    qtbot.mouseClick(window.sql_tab.run_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.sql_tab.results.state == "ready", timeout=WAIT)
    entries = window.history.list(window.active_profile().name)
    assert [e.sql for e in entries] == ["SELECT 1 AS a"]

    window.sql_tab.editor.setPlainText("DELETE FROM bronze_medical")
    qtbot.mouseClick(window.sql_tab.run_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.sql_tab.results.state == "error", timeout=WAIT)
    entries_after = window.history.list(window.active_profile().name)
    assert [e.sql for e in entries_after] == ["SELECT 1 AS a"]  # the failed run isn't recorded


def test_history_menu_reinserts_query_and_clears(local_window, qtbot):
    window = local_window
    window.tabs.setCurrentWidget(window.sql_tab)
    window.sql_tab.editor.setPlainText("SELECT 1 AS a")
    qtbot.mouseClick(window.sql_tab.run_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.sql_tab.results.state == "ready", timeout=WAIT)

    window.sql_tab.editor.clear()
    window.sql_tab._populate_history_menu()
    actions = window.sql_tab.history_menu.actions()
    matching = [a for a in actions if a.toolTip() == "SELECT 1 AS a"]
    assert matching
    matching[0].trigger()
    assert window.sql_tab.editor.toPlainText() == "SELECT 1 AS a"

    window.sql_tab._on_clear_history()
    assert window.history.list(window.active_profile().name) == []


# --------------------------------------------------------------------------- UI: shortcuts


def test_shortcuts_are_assigned_the_documented_keys(local_window):
    from PySide6.QtGui import QKeySequence

    window = local_window
    assert window.refresh_action.shortcut() == QKeySequence("Ctrl+R")
    assert QKeySequence("Ctrl+.") in window.cancel_action.shortcuts()
    assert QKeySequence(Qt.Key.Key_Escape) in window.cancel_action.shortcuts()
    assert window.focus_filter_action.shortcut() == QKeySequence.StandardKey.Find
    assert window.preview_tab_action.shortcut() == QKeySequence("Ctrl+1")
    assert window.schema_tab_action.shortcut() == QKeySequence("Ctrl+2")
    assert window.sql_tab_action.shortcut() == QKeySequence("Ctrl+3")
    assert window.export_action.shortcut() == QKeySequence("Ctrl+E")


def test_shortcut_refresh_triggers_refresh(local_window, monkeypatch):
    window = local_window
    called = []
    monkeypatch.setattr(window, "refresh", lambda: called.append(True))
    window.refresh_action.trigger()
    assert called


def test_shortcut_tab_switch_triggers_tab_change(local_window):
    window = local_window
    window.sql_tab_action.trigger()
    assert window.tabs.currentWidget() is window.sql_tab
    window.schema_tab_action.trigger()
    assert window.tabs.currentWidget() is window.schema_tab
    window.preview_tab_action.trigger()
    assert window.tabs.currentWidget() is window.preview_tab


def test_shortcut_focus_filter_focuses_the_table_filter(local_window, qtbot):
    window = local_window
    window.show()
    qtbot.waitExposed(window)
    window.focus_filter_action.trigger()
    qtbot.waitUntil(lambda: window.browser.filter_edit.hasFocus(), timeout=WAIT)


def test_shortcut_cancel_stops_a_running_query(local_window, qtbot):
    window = local_window
    window.tabs.setCurrentWidget(window.sql_tab)
    window.sql_tab.editor.setPlainText(SLOW_SQL)
    qtbot.mouseClick(window.sql_tab.run_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: window.sql_tab.is_running, timeout=WAIT)
    window.cancel_action.trigger()
    qtbot.waitUntil(lambda: not window.sql_tab.is_running, timeout=WAIT)


def test_shortcut_export_triggers_export_for_the_current_tab(export_window, monkeypatch):
    window = export_window
    assert window.browser.select_table("bronze.medical")
    window.tabs.setCurrentWidget(window.preview_tab)
    called = []
    monkeypatch.setattr(window, "_export_preview", lambda: called.append(True))
    window.export_action.trigger()
    assert called


# --------------------------------------------------------------------------- UI: remembered state


def test_state_round_trip(iceberg_fixtures, store, qtbot, tmp_path):  # noqa: F811
    profile_a = local_profile(iceberg_fixtures, name="eg-a")
    profile_b = local_profile(iceberg_fixtures, name="eg-b")
    store.save(profile_a)
    store.save(profile_b)
    settings = QSettings(str(tmp_path / "floe-settings.ini"), QSettings.Format.IniFormat)

    window1 = MainWindow(store, settings=settings)
    qtbot.addWidget(window1)
    qtbot.waitUntil(lambda: window1.branch_combo.count() == 3, timeout=WAIT)
    window1._profile_combo.setCurrentText("eg-b")
    qtbot.waitUntil(
        lambda: window1.active_profile() is not None and window1.active_profile().name == "eg-b",
        timeout=WAIT,
    )
    qtbot.waitUntil(lambda: window1.branch_combo.count() == 3, timeout=WAIT)
    assert window1.select_branch("eg-test2")
    wait_tree(qtbot, window1)
    window1.tabs.setCurrentWidget(window1.sql_tab)
    window1.sql_tab.editor.setPlainText("SELECT 1 AS demo")
    window1.save_state()
    assert window1.shutdown(5000)

    window2 = MainWindow(store, settings=settings)
    qtbot.addWidget(window2)
    assert window2.active_profile() is not None
    assert window2.active_profile().name == "eg-b"
    qtbot.waitUntil(lambda: window2.branch_combo.count() == 3, timeout=WAIT)
    qtbot.waitUntil(lambda: window2.current_ref == "eg-test2", timeout=WAIT)
    assert window2.sql_tab.editor.toPlainText() == "SELECT 1 AS demo"
    assert window2.tabs.currentWidget() is window2.sql_tab
    assert window2.shutdown(5000)


def test_missing_profile_falls_back_cleanly(iceberg_fixtures, qtbot, tmp_path):
    profile = local_profile(iceberg_fixtures, name="eg-solo")
    store_a = ProfileStore(tmp_path / "profiles_a.json")
    store_a.save(profile)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)

    window1 = MainWindow(store_a, settings=settings)
    qtbot.addWidget(window1)
    qtbot.waitUntil(lambda: window1.branch_combo.count() == 3, timeout=WAIT)
    window1.save_state()
    assert window1.shutdown(5000)

    store_b = ProfileStore(tmp_path / "profiles_b.json")  # no "eg-solo" here
    window2 = MainWindow(store_b, settings=settings)
    qtbot.addWidget(window2)
    assert window2.active_profile() is None
    assert window2.browser.state == "empty"
    assert window2.shutdown(5000)


def test_missing_branch_falls_back_to_first(iceberg_fixtures, store, qtbot, tmp_path):  # noqa: F811
    profile = local_profile(iceberg_fixtures, name="eg-branch")
    store.save(profile)
    settings = QSettings(str(tmp_path / "settings2.ini"), QSettings.Format.IniFormat)
    settings.setValue("profile/last", "eg-branch")
    settings.setValue("branch/eg-branch", "does-not-exist")

    window = MainWindow(store, settings=settings)
    qtbot.addWidget(window)
    qtbot.waitUntil(lambda: window.branch_combo.count() == 3, timeout=WAIT)
    assert window.current_ref == "eg-c3"
    assert window.shutdown(5000)
