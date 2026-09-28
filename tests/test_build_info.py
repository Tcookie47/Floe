"""Tests for scripts/write_build_info.py and floe.core.extensions (SPEC §11)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_write_build_info_strips_leading_v(tmp_path):
    write_build_info = _load_module(
        "write_build_info", _SCRIPTS_DIR / "write_build_info.py"
    )

    out_path = tmp_path / "_build_info.py"
    write_build_info.write_build_info("v1.2.3", "abc1234", out_path)

    contents = out_path.read_text()
    assert "1.2.3" in contents
    assert "v1.2.3" not in contents
    assert "abc1234" in contents

    module = _load_module("_build_info_test", out_path)
    assert module.__version__ == "1.2.3"
    assert module.__commit__ == "abc1234"


def test_write_build_info_keeps_version_without_v_prefix(tmp_path):
    write_build_info = _load_module(
        "write_build_info", _SCRIPTS_DIR / "write_build_info.py"
    )

    out_path = tmp_path / "_build_info.py"
    write_build_info.write_build_info("2.0.0", "deadbee", out_path)

    module = _load_module("_build_info_test2", out_path)
    assert module.__version__ == "2.0.0"


def test_bundled_extension_dir_none_when_not_frozen(monkeypatch):
    from floe.core.extensions import bundled_extension_dir

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert bundled_extension_dir() is None


def test_bundled_extension_dir_none_when_frozen_but_missing(tmp_path, monkeypatch):
    from floe.core.extensions import bundled_extension_dir

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    # tmp_path has no duckdb_extensions subdirectory.
    assert bundled_extension_dir() is None


def test_bundled_extension_dir_found_when_frozen(tmp_path, monkeypatch):
    from floe.core.extensions import bundled_extension_dir

    ext_dir = tmp_path / "duckdb_extensions"
    ext_dir.mkdir()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    result = bundled_extension_dir()
    assert result == ext_dir


def test_write_build_info_normalises_and_rejects_non_pep440(tmp_path):
    import pytest

    wbi = _load_module("write_build_info_pep440", _SCRIPTS_DIR / "write_build_info.py")
    out_path = tmp_path / "_build_info.py"
    assert wbi.write_build_info("v1.2.0-rc1", "abc1234", out_path) == "1.2.0rc1"
    assert wbi.write_build_info("desktop-v0.3.0", "abc1234", out_path) == "0.3.0"
    assert wbi.write_build_info("0.0.0.dev0", "abc1234", out_path) == "0.0.0.dev0"
    for bad in ("v0.0.0-ci", "latest", "desktop-vX", ""):
        with pytest.raises(wbi.BuildInfoError, match="PEP 440"):
            wbi.write_build_info(bad, "abc1234", out_path)
    assert wbi.main(["write_build_info.py", "v0.0.0-ci", "abc1234"]) == 2


def test_fallback_version_is_pep440():
    from packaging.version import Version

    import floe

    Version(floe.__version__)  # raises InvalidVersion otherwise
