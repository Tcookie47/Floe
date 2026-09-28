"""Shared pytest fixtures: in-memory keyring backend and isolated FLOE_HOME.

Qt (PySide6) is optional (SPEC §15.5): this module must import cleanly without
it so core-only tests can run, and only touches `QSettings` when it's
actually installed.
"""

import keyring
import pytest
from keyring.backend import KeyringBackend

try:
    from PySide6.QtCore import QSettings
except ImportError:  # pragma: no cover - exercised in the core-only CI job
    QSettings = None


class InMemoryKeyring(KeyringBackend):
    """A dict-backed keyring backend that never touches the real Keychain."""

    priority = 1

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop((service, username), None)


class RaisingKeyring(KeyringBackend):
    """A keyring backend whose set/get/delete always raise `KeyringError`.

    Used to simulate a broken Keychain, or the user clicking "Deny" on the
    macOS Keychain access prompt.
    """

    priority = 1

    def set_password(self, service: str, username: str, password: str) -> None:
        raise keyring.errors.PasswordSetError("simulated Keychain denial")

    def get_password(self, service: str, username: str) -> str | None:
        raise keyring.errors.KeyringError("simulated Keychain read failure")

    def delete_password(self, service: str, username: str) -> None:
        raise keyring.errors.KeyringError("simulated Keychain delete failure")


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path, monkeypatch):
    """Use an in-memory keyring, a temp FLOE_HOME, and isolated QSettings for every
    test — `MainWindow`'s default `QSettings("tcookie", "Floe")` must never touch the
    real user settings (SPEC M6)."""
    keyring.set_keyring(InMemoryKeyring())
    monkeypatch.setenv("FLOE_HOME", str(tmp_path))
    if QSettings is not None:
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(
            QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings")
        )
    yield


@pytest.fixture
def fake_nessie():
    """A running in-process fake Nessie server (see tests/fakes/fake_nessie.py)."""
    from tests.fakes.fake_nessie import FakeNessie

    with FakeNessie() as fake:
        yield fake


@pytest.fixture(scope="session")
def iceberg_fixtures(tmp_path_factory):
    """Synthetic pyiceberg tables + local-mode parquet (tests/fakes/iceberg_fixtures.py)."""
    from tests.fakes.iceberg_fixtures import build_fixtures

    return build_fixtures(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture(scope="session")
def iceberg_unavailable_reason():
    """None if DuckDB's iceberg extension can be installed/loaded, else the reason."""
    from tests.fakes.iceberg_fixtures import extension_unavailable_reason

    return extension_unavailable_reason("iceberg")


@pytest.fixture(scope="session")
def storage_extensions_available():
    """True if DuckDB's azure + httpfs extensions can be installed/loaded here."""
    from tests.fakes.iceberg_fixtures import extension_unavailable_reason

    return extension_unavailable_reason("azure", "httpfs") is None
