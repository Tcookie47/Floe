"""Tests for floe.ui.profile_dialog (SPEC §8.2, §13.2)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from floe.core.profiles import ProfileStore
from floe.ui.main_window import MainWindow
from floe.ui.profile_dialog import ProfileDialog, _mode_local


def _store(tmp_path) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles.json")


def test_create_local_profile_and_save(qtbot, tmp_path):
    store = _store(tmp_path)
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()

    dialog = ProfileDialog(store)
    qtbot.addWidget(dialog)

    dialog._widgets["name"].setText("eg-local1")
    dialog._widgets["mode"].setCurrentText("local")
    dialog._widgets["local_fixture_dir"].setText(str(fixture_dir))

    dialog._on_save()

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert "eg-local1" in raw
    assert raw["eg-local1"]["mode"] == "local"
    assert raw["eg-local1"]["local_fixture_dir"] == str(fixture_dir)

    loaded = store.get("eg-local1")
    assert loaded is not None
    assert loaded.mode == "local"


def test_secret_field_shows_saved_placeholder_and_stays_empty(qtbot, tmp_path):
    store = _store(tmp_path)
    from floe.core.profiles import Profile

    store.save(
        Profile(name="eg-test1", nessie_uri="https://nessie.example/api/v2"),
        secrets={"adls_account_key": "super-secret-value"},
    )

    dialog = ProfileDialog(store)
    qtbot.addWidget(dialog)

    for i in range(dialog._list.count()):
        if dialog._list.item(i).text() == "eg-test1":
            dialog._list.setCurrentRow(i)
            break

    secret_widget = dialog._widgets["adls_account_key"]
    line = secret_widget.line_edit
    assert line.text() == ""
    assert line.placeholderText() == "(saved)"

    # Saving again without touching the secret field leaves it unchanged.
    dialog._on_save()
    assert store.get_secret("eg-test1", "adls_account_key") == "super-secret-value"


def test_clear_button_deletes_secret_on_save(qtbot, tmp_path):
    from floe.core.profiles import Profile

    store = _store(tmp_path)
    store.save(
        Profile(name="eg-test1", nessie_uri="https://nessie.example/api/v2"),
        secrets={"adls_account_key": "super-secret-value"},
    )

    dialog = ProfileDialog(store)
    qtbot.addWidget(dialog)
    for i in range(dialog._list.count()):
        if dialog._list.item(i).text() == "eg-test1":
            dialog._list.setCurrentRow(i)
            break

    dialog._widgets["adls_account_key"].clear_btn.click()
    dialog._on_save()

    assert store.get_secret("eg-test1", "adls_account_key") is None


def test_visibility_toggles_by_mode_and_auth(qtbot, tmp_path):
    store = _store(tmp_path)
    dialog = ProfileDialog(store)
    qtbot.addWidget(dialog)

    # `isVisible()` also factors in whether the (never-shown) window itself is
    # visible; `isHidden()` reflects only this widget's own explicit state.
    dialog._widgets["mode"].setCurrentText("remote")
    dialog._update_visibility()
    assert not dialog._widgets["nessie_uri"].isHidden()
    assert dialog._widgets["local_fixture_dir"].isHidden()

    dialog._widgets["mode"].setCurrentText("local")
    dialog._update_visibility()
    assert not dialog._widgets["local_fixture_dir"].isHidden()
    assert dialog._group_boxes["Nessie"].isHidden()
    assert dialog._group_boxes["Storage (ADLS)"].isHidden()

    dialog._widgets["mode"].setCurrentText("remote")
    dialog._widgets["adls_auth"].setCurrentText("account_key")
    dialog._update_visibility()
    assert not dialog._widgets["adls_account_key"].isHidden()
    assert dialog._widgets["adls_client_id"].isHidden()

    dialog._widgets["adls_auth"].setCurrentText("service_principal")
    dialog._update_visibility()
    assert dialog._widgets["adls_account_key"].isHidden()
    assert not dialog._widgets["adls_client_id"].isHidden()

    dialog._widgets["nessie_auth"].setCurrentText("none")
    dialog._update_visibility()
    assert dialog._widgets["nessie_client_secret"].isHidden()

    dialog._widgets["nessie_auth"].setCurrentText("oauth2")
    dialog._update_visibility()
    assert not dialog._widgets["nessie_client_secret"].isHidden()


def test_import_env_fills_form_without_saving(qtbot, tmp_path):
    store = _store(tmp_path)
    dialog = ProfileDialog(store)
    qtbot.addWidget(dialog)

    env_path = tmp_path / ".env"
    env_path.write_text(
        "ADLS_ACCOUNT=acct-a\nNESSIE_URI=https://nessie.example.test\n"
        "NESSIE_KEY_VAULT=some-vault\n",
        encoding="utf-8",
    )

    from PySide6.QtWidgets import QFileDialog, QMessageBox

    qtbot.monkeypatch = getattr(qtbot, "monkeypatch", None)
    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    try:
        mp.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(env_path), ""))
        notes = {}
        mp.setattr(
            QMessageBox,
            "information",
            staticmethod(lambda *a, **k: notes.setdefault("shown", a[-1])),
        )
        dialog._on_import_env()
    finally:
        mp.undo()

    assert dialog._widgets["adls_account"].text() == "acct-a"
    assert dialog._widgets["nessie_uri"].text() == "https://nessie.example.test"
    assert "key vault" in notes.get("shown", "").lower()
    assert not store.path.exists()


def test_rename_via_dialog_moves_keyring_entries(qtbot, tmp_path):
    from floe.core.profiles import Profile

    store = _store(tmp_path)
    store.save(
        Profile(name="eg-old", nessie_uri="https://nessie.example/api/v2"),
        secrets={"adls_account_key": "key-value-abc"},
    )

    dialog = ProfileDialog(store)
    qtbot.addWidget(dialog)
    for i in range(dialog._list.count()):
        if dialog._list.item(i).text() == "eg-old":
            dialog._list.setCurrentRow(i)
            break

    dialog._widgets["name"].setText("eg-new")
    dialog._on_save()

    assert store.get("eg-old") is None
    assert store.get("eg-new") is not None
    assert store.get_secret("eg-old", "adls_account_key") is None
    assert store.get_secret("eg-new", "adls_account_key") == "key-value-abc"


def test_save_shows_warning_and_leaves_no_partial_profile_on_keyring_failure(
    qtbot, tmp_path, monkeypatch
):
    import keyring
    from PySide6.QtWidgets import QMessageBox

    from tests.conftest import RaisingKeyring

    store = _store(tmp_path)
    dialog = ProfileDialog(store)
    qtbot.addWidget(dialog)

    dialog._widgets["name"].setText("eg-broken")
    dialog._widgets["nessie_uri"].setText("https://nessie.example/api/v2")
    dialog._widgets["adls_account_key"].line_edit.setText("super-secret-value")

    warnings = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *a, **k: warnings.append(a[-1])),
    )
    keyring.set_keyring(RaisingKeyring())

    dialog._on_save()

    assert warnings, "expected a warning message box on Keychain failure"
    assert "super-secret-value" not in warnings[0]
    assert "keyring" in warnings[0].lower()
    # profiles.json must not have been written since the secret write failed.
    assert store.get("eg-broken") is None
    assert not store.path.exists()


def test_copy_diagnostics_has_no_secrets(qtbot, tmp_path, monkeypatch):
    from floe.core.profiles import Profile

    store = _store(tmp_path)
    secret_value = "very-secret-account-key-xyz"
    store.save(
        Profile(name="eg-test1", adls_account="acct-a"),
        secrets={"adls_account_key": secret_value},
    )

    window = MainWindow(store)
    qtbot.addWidget(window)
    window._profile_combo.setCurrentText("eg-test1")

    window._copy_diagnostics()

    from PySide6.QtWidgets import QApplication

    clip = QApplication.clipboard().text()
    assert "eg-test1" in clip
    assert secret_value not in clip


def test_mode_local_helper() -> None:
    assert _mode_local({"mode": "local"})
    assert not _mode_local({"mode": "remote"})
