"""Tests for floe.core.paths (SPEC §15.2)."""

import sys
from pathlib import Path

import platformdirs
import pytest
from platformdirs.macos import MacOS

from floe.core import paths


def test_app_support_dir_uses_floe_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOE_HOME", str(tmp_path))
    assert paths.app_support_dir() == tmp_path / "support"


def test_logs_dir_uses_floe_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOE_HOME", str(tmp_path))
    assert paths.logs_dir() == tmp_path / "logs"


def test_prefs_path_uses_floe_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOE_HOME", str(tmp_path))
    assert paths.prefs_path() == tmp_path / "support" / "prefs.json"


def test_app_support_dir_default_matches_platformdirs(monkeypatch):
    monkeypatch.delenv("FLOE_HOME", raising=False)
    assert paths.app_support_dir() == Path(platformdirs.user_data_dir("Floe", appauthor=False))


def test_logs_dir_default_matches_platformdirs(monkeypatch):
    monkeypatch.delenv("FLOE_HOME", raising=False)
    assert paths.logs_dir() == Path(platformdirs.user_log_dir("Floe", appauthor=False))


def test_does_not_create_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOE_HOME", str(tmp_path))
    support = paths.app_support_dir()
    logs = paths.logs_dir()
    assert not support.exists()
    assert not logs.exists()


def test_macos_app_support_dir_matches_v0_9_layout():
    """On macOS, platformdirs must resolve to the same directory the v0.9 app's
    hard-coded path used, so both versions share profiles.json (SPEC §15.2).
    Asserted directly against platformdirs' macOS class so it holds regardless
    of the OS actually running the test."""
    mac_dirs = MacOS(appname="Floe", appauthor=False)
    assert Path(mac_dirs.user_data_dir) == Path.home() / "Library" / "Application Support" / "Floe"


def test_macos_logs_dir_matches_v0_9_layout():
    mac_dirs = MacOS(appname="Floe", appauthor=False)
    assert Path(mac_dirs.user_log_dir) == Path.home() / "Library" / "Logs" / "Floe"


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real macOS resolution")
def test_app_support_dir_default_on_macos(monkeypatch):
    monkeypatch.delenv("FLOE_HOME", raising=False)
    assert paths.app_support_dir() == Path.home() / "Library" / "Application Support" / "Floe"


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises the real macOS resolution")
def test_logs_dir_default_on_macos(monkeypatch):
    monkeypatch.delenv("FLOE_HOME", raising=False)
    assert paths.logs_dir() == Path.home() / "Library" / "Logs" / "Floe"
