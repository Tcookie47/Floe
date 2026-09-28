"""Enforce that floe.core and floe.web never import Qt (PySide6)."""

import subprocess
import sys
import textwrap

_SCRIPT = textwrap.dedent(
    """
    import importlib
    import pkgutil
    import sys


    class _BlockPySide6:
        def find_module(self, name, path=None):
            if name == "PySide6" or name.startswith("PySide6."):
                raise ImportError(f"PySide6 import blocked: {name}")
            return None

        def find_spec(self, name, path, target=None):
            if name == "PySide6" or name.startswith("PySide6."):
                raise ImportError(f"PySide6 import blocked: {name}")
            return None


    sys.meta_path.insert(0, _BlockPySide6())

    import floe.cli
    import floe.core
    import floe.web

    for package in (floe.core, floe.web):
        prefix = package.__name__ + "."
        for module_info in pkgutil.walk_packages(package.__path__, prefix=prefix):
            importlib.import_module(module_info.name)

    print("OK")
    """
)


def test_core_and_web_import_without_qt():
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
