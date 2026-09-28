"""Phase 5 SQL editor tests (SPEC §6.3 SQL-tab resolution, §8.1 SQL tab, §8.5): pytest-qt,
offscreen. Runs against the local-mode fixtures reused from `test_browser.py`.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from floe.core.context import QueryResult
from floe.ui.sql_tab import SQL_CHANNEL
from tests.test_browser import (  # noqa: F401
    fx_local,
    isolated_settings,
    local_window,
    open_window,
    store,
    wait_tree,
)

pytest.importorskip("pytestqt")

WAIT = 15000  # ms: generous upper bound, never an expected duration

SLOW_SQL = (
    "SELECT count(*) FROM range(1000000000) a, range(1000000) b WHERE a.range + b.range = -1"
)


def wait_ready(qtbot, window) -> None:
    qtbot.waitUntil(lambda: window.sql_tab.results.state == "ready", timeout=WAIT)


def wait_not_running(qtbot, window) -> None:
    qtbot.waitUntil(lambda: not window.sql_tab.is_running, timeout=WAIT)


def run_sql(qtbot, window, sql: str) -> None:
    sql_tab = window.sql_tab
    sql_tab.editor.setPlainText(sql)
    qtbot.mouseClick(sql_tab.run_button, Qt.MouseButton.LeftButton)


# --------------------------------------------------------------------------- basics


def test_join_across_tenant_and_reference_table(local_window, qtbot):
    window = local_window
    run_sql(
        qtbot,
        window,
        "SELECT m.id, c.label FROM bronze_medical m "
        "JOIN gold_ref_codes c ON m.code = c.code ORDER BY m.id",
    )
    wait_ready(qtbot, window)
    wait_not_running(qtbot, window)
    model = window.sql_tab.results.view.results_model
    assert model.rowCount() == 3  # tenant filter on: bronze_medical's 3 own rows join codes
    assert "3 rows" in window.result_label.text()
    assert window.sql_tab.run_button.isEnabled()
    assert not window.sql_tab.cancel_button.isEnabled()


def test_ctrl_enter_runs(local_window, qtbot):
    window = local_window
    window.show()
    window.tabs.setCurrentWidget(window.sql_tab)
    window.sql_tab.editor.setPlainText("SELECT 1 AS one")
    window.sql_tab.editor.setFocus()
    qtbot.waitActive(window)
    qtbot.waitUntil(lambda: window.sql_tab.editor.hasFocus(), timeout=WAIT)
    qtbot.keyClick(
        window.sql_tab.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier
    )
    wait_ready(qtbot, window)
    model = window.sql_tab.results.view.results_model
    assert model.rowCount() == 1


def test_selection_only_runs_selected_text(local_window, qtbot):
    window = local_window
    editor = window.sql_tab.editor
    editor.setPlainText("SELECT 1 AS a;\nSELECT 2 AS a")
    cursor = editor.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(len("SELECT 1 AS a"), cursor.MoveMode.KeepAnchor)
    editor.setTextCursor(cursor)
    qtbot.mouseClick(window.sql_tab.run_button, Qt.MouseButton.LeftButton)
    wait_ready(qtbot, window)
    df = window.sql_tab.results.view.results_model.dataframe
    assert list(df["a"]) == [1]


def test_truncation_notice(local_window, qtbot):
    window = local_window
    window.sql_tab.limit_spin.setValue(1)
    run_sql(qtbot, window, "SELECT * FROM range(5)")
    wait_ready(qtbot, window)
    model = window.sql_tab.results.view.results_model
    assert model.rowCount() == 1
    assert "limit reached" in window.result_label.text()


# --------------------------------------------------------------------------- errors


def test_write_statement_rejected(local_window, qtbot):
    window = local_window
    run_sql(qtbot, window, "DELETE FROM bronze_medical")
    qtbot.waitUntil(lambda: window.sql_tab.results.state == "error", timeout=WAIT)
    text = window.sql_tab.results.message_text
    assert "read-only" in text.lower()
    assert window.sql_tab.run_button.isEnabled()


def test_read_parquet_rejected(local_window, qtbot):
    window = local_window
    run_sql(qtbot, window, "SELECT * FROM read_parquet('does-not-matter.parquet')")
    qtbot.waitUntil(lambda: window.sql_tab.results.state == "error", timeout=WAIT)
    text = window.sql_tab.results.message_text.lower()
    assert "not allowed" in text or "blocked" in text


def test_missing_data_source_offers_to_run_without_filter(local_window, qtbot):
    window = local_window
    run_sql(qtbot, window, "SELECT * FROM silver_input_layer_no_ds")
    qtbot.waitUntil(lambda: window.sql_tab.results.state == "error", timeout=WAIT)
    text = window.sql_tab.results.message_text
    assert "silver.input_layer.no_ds" in text  # the offending table's key
    button = window.sql_tab.results.message.button
    assert not button.isHidden()
    assert button.text() == "Run without tenant filter"

    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    wait_ready(qtbot, window)
    assert window.sql_tab.results.view.results_model.rowCount() == 2
    assert "tenant filter off" in window.result_label.text()

    # The override was for that one run only: running again re-applies the filter.
    run_sql(qtbot, window, "SELECT * FROM silver_input_layer_no_ds")
    qtbot.waitUntil(lambda: window.sql_tab.results.state == "error", timeout=WAIT)


# --------------------------------------------------------------------------- registration errors


def test_registration_error_names_the_table(local_window, qtbot):
    window = local_window
    run_sql(qtbot, window, "SELECT * FROM silver_input_layer_no_ds WHERE 1=2")
    qtbot.waitUntil(lambda: window.sql_tab.results.state == "error", timeout=WAIT)
    assert "silver.input_layer.no_ds" in window.sql_tab.results.message_text


# --------------------------------------------------------------------------- cancel


def test_cancel_long_query(local_window, qtbot):
    window = local_window
    run_sql(qtbot, window, SLOW_SQL)
    qtbot.waitUntil(lambda: window.sql_tab.is_running, timeout=WAIT)

    def executing() -> bool:
        workers = window.runner.active(SQL_CHANNEL)
        return bool(workers) and workers[0].cancel_token.executing

    qtbot.waitUntil(executing, timeout=WAIT)
    assert not window.sql_tab.run_button.isEnabled()
    assert window.sql_tab.cancel_button.isEnabled()

    qtbot.mouseClick(window.sql_tab.cancel_button, Qt.MouseButton.LeftButton)
    assert not window.sql_tab.is_running
    assert window.sql_tab.run_button.isEnabled()
    assert not window.sql_tab.cancel_button.isEnabled()
    assert "cancelled" in window.sql_tab.results.message_text.lower()
    assert "cancelled" in window.statusBar().currentMessage().lower()
    qtbot.waitUntil(lambda: not window.runner.active(SQL_CHANNEL), timeout=WAIT)


def test_branch_switch_cancels_inflight_sql(local_window, qtbot):
    window = local_window
    run_sql(qtbot, window, SLOW_SQL)
    qtbot.waitUntil(lambda: window.sql_tab.is_running, timeout=WAIT)

    def executing() -> bool:
        workers = window.runner.active(SQL_CHANNEL)
        return bool(workers) and workers[0].cancel_token.executing

    qtbot.waitUntil(executing, timeout=WAIT)
    worker = window.runner.active(SQL_CHANNEL)[0]

    window.select_branch("eg-test2")
    assert worker.cancel_token.cancelled
    assert not window.sql_tab.is_running
    assert window.sql_tab.run_button.isEnabled()
    wait_tree(qtbot, window)
    qtbot.waitUntil(lambda: not window.runner.active(SQL_CHANNEL), timeout=WAIT)


# --------------------------------------------------------------------------- reloaded notice


def test_reloaded_notice_shown(local_window, qtbot, monkeypatch):
    window = local_window

    def fake_query(ref, sql, limit=None, tenant_filter=True, *, cancel_token=None):
        import pandas as pd

        df = pd.DataFrame({"a": [1]})
        return QueryResult(df=df, truncated=False, elapsed=0.01, reloaded=True)

    monkeypatch.setattr(window.session, "query", fake_query)
    run_sql(qtbot, window, "SELECT 1 AS a")
    wait_ready(qtbot, window)
    qtbot.waitUntil(
        lambda: "reloaded" in window.statusBar().currentMessage().lower(), timeout=WAIT
    )
