"""UI smoke test for the minimal M2 main window (SPEC §8.1, §13.2)."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import floe
from floe.ui.main_window import MainWindow


def test_main_window_opens_with_about(qtbot, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)

    assert window.windowTitle() == "Floe"

    help_action = next(a for a in window.menuBar().actions() if a.text() == "Help")
    assert help_action is not None
    help_menu = help_action.menu()

    about_action = next(a for a in help_menu.actions() if a.text() == "About Floe")
    assert about_action is not None

    captured = {}

    def fake_about(parent, title, text):
        captured["title"] = title
        captured["text"] = text

    monkeypatch.setattr("floe.ui.main_window.QMessageBox.about", fake_about)

    about_action.trigger()

    assert captured["title"] == "About Floe"
    assert floe.__version__ in captured["text"]
