"""Profile create/edit dialog (SPEC §8.2)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from floe.core.connection_test import run_connection_test
from floe.core.errors import FloeError
from floe.core.profiles import SECRET_FIELDS, Profile, ProfileStore, parse_env_file
from floe.ui.workers import Worker, run_in_background, user_error_text

_SAVED_PLACEHOLDER = "(saved)"

# ----- form field descriptors -----------------------------------------------

# kinds: "str", "path", "int", "bool", "enum", "list", "dict", "secret"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: str
    group: str
    placeholder: str = ""
    choices: tuple[str, ...] = ()
    visible_when: Callable[[dict[str, Any]], bool] | None = None
    tooltip: str = ""


def _mode_local(values: dict[str, Any]) -> bool:
    return values.get("mode") == "local"


def _mode_remote(values: dict[str, Any]) -> bool:
    return values.get("mode") != "local"


def _account_key_auth(values: dict[str, Any]) -> bool:
    return values.get("adls_auth") == "account_key"


def _service_principal_auth(values: dict[str, Any]) -> bool:
    return values.get("adls_auth") == "service_principal"


def _oauth2_auth(values: dict[str, Any]) -> bool:
    return values.get("nessie_auth") == "oauth2"


GENERAL = "General"
STORAGE = "Storage (ADLS)"
NESSIE = "Nessie"
TENANT = "Tenant scoping"
DUCKDB = "DuckDB"
SAFETY = "Safety"

# Groups hidden entirely in local mode: no ADLS/Nessie/tenant creds are needed.
REMOTE_ONLY_GROUPS = (STORAGE, NESSIE, TENANT)

FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("name", "Name", "str", GENERAL, placeholder="eg-my-profile"),
    FieldSpec("mode", "Mode", "enum", GENERAL, choices=("remote", "local")),
    FieldSpec(
        "local_fixture_dir",
        "Local fixture directory",
        "path",
        GENERAL,
        placeholder="/path/to/fixtures",
        visible_when=_mode_local,
    ),
    FieldSpec("adls_account", "Storage account", "str", STORAGE, placeholder="myaccount"),
    FieldSpec(
        "adls_auth", "Auth mode", "enum", STORAGE, choices=("account_key", "service_principal")
    ),
    FieldSpec(
        "adls_account_key", "Account key", "secret", STORAGE, visible_when=_account_key_auth
    ),
    FieldSpec(
        "adls_tenant_id", "Tenant ID", "str", STORAGE, visible_when=_service_principal_auth
    ),
    FieldSpec(
        "adls_client_id", "Client ID", "str", STORAGE, visible_when=_service_principal_auth
    ),
    FieldSpec(
        "adls_client_secret",
        "Client secret",
        "secret",
        STORAGE,
        visible_when=_service_principal_auth,
    ),
    FieldSpec(
        "adls_ca_cert_file",
        "CA cert file",
        "path",
        STORAGE,
        placeholder="/path/to/ca-bundle.pem (optional)",
    ),
    FieldSpec(
        "nessie_uri", "Nessie URI", "str", NESSIE, placeholder="https://nessie.example/api/v2"
    ),
    FieldSpec("nessie_auth", "Auth mode", "enum", NESSIE, choices=("oauth2", "none")),
    FieldSpec(
        "nessie_token_endpoint",
        "Token endpoint",
        "str",
        NESSIE,
        placeholder="https://auth.example/oauth2/token",
        visible_when=_oauth2_auth,
    ),
    FieldSpec("nessie_client_id", "Client ID", "str", NESSIE, visible_when=_oauth2_auth),
    FieldSpec("nessie_scope", "Scope", "str", NESSIE, visible_when=_oauth2_auth),
    FieldSpec(
        "nessie_client_secret", "Client secret", "secret", NESSIE, visible_when=_oauth2_auth
    ),
    FieldSpec("nessie_main_ref", "Main ref", "str", NESSIE, placeholder="main"),
    FieldSpec("nessie_head_ttl_seconds", "Head TTL (seconds)", "int", NESSIE),
    FieldSpec(
        "shared_containers",
        "Shared containers (one per line)",
        "list",
        TENANT,
    ),
    FieldSpec(
        "shared_namespaces",
        "Shared namespaces (one per line)",
        "list",
        TENANT,
    ),
    FieldSpec(
        "tenant_registry_table",
        "Tenant registry table",
        "str",
        TENANT,
        placeholder="registry.tenants (optional)",
    ),
    FieldSpec(
        "tenant_container_map",
        "Tenant → container (key=value per line)",
        "dict",
        TENANT,
    ),
    FieldSpec(
        "tenant_data_source_map",
        "Tenant → data_source (key=value per line)",
        "dict",
        TENANT,
    ),
    FieldSpec(
        "duckdb_memory_limit", "Memory limit", "str", DUCKDB, placeholder="4GB (optional)"
    ),
    FieldSpec("duckdb_threads", "Threads", "int", DUCKDB, placeholder="(optional)"),
    FieldSpec("conn_cache_max", "Max cached connections", "int", DUCKDB),
    FieldSpec("allow_export", "Allow CSV export", "bool", SAFETY),
    FieldSpec("preview_row_limit", "Preview row limit", "int", SAFETY),
    FieldSpec(
        "restrict_file_access",
        "Restrict file access",
        "bool",
        SAFETY,
        tooltip=(
            "Restrict DuckDB file access to this branch's containers. Turn off only if "
            "legitimate reads fail with 'Access denied'."
        ),
    ),
)

GROUP_ORDER = (GENERAL, STORAGE, NESSIE, TENANT, DUCKDB, SAFETY)


def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _parse_kv_lines(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in _split_lines(text):
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


class ProfileDialog(QDialog):
    """Create, edit, duplicate, delete and test-connect profiles."""

    profilesChanged = Signal()

    def __init__(self, store: ProfileStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._widgets: dict[str, QWidget] = {}
        self._secret_state: dict[str, bool] = {}  # field -> explicitly cleared
        self._current_name: str | None = None
        self._test_worker: Worker | None = None

        self.setWindowTitle("Profiles")
        self.resize(760, 560)
        self._build_ui()
        self._reload_list()

    # ----- UI construction ---------------------------------------------------
    def _build_ui(self) -> None:
        splitter = QSplitter(self)

        self._list = QListWidget(splitter)
        self._list.currentItemChanged.connect(self._on_selection_changed)
        splitter.addWidget(self._list)

        right = QWidget(splitter)
        right_layout = QVBoxLayout(right)

        scroll = QScrollArea(right)
        scroll.setWidgetResizable(True)
        form_host = QWidget()
        form_layout = QVBoxLayout(form_host)
        self._group_boxes: dict[str, QGroupBox] = {}
        for group in GROUP_ORDER:
            box = QGroupBox(group, form_host)
            box_form = QFormLayout(box)
            self._group_boxes[group] = box
            form_layout.addWidget(box)
            for spec in FIELDS:
                if spec.group != group:
                    continue
                self._add_field(box_form, spec)
        form_layout.addStretch(1)
        scroll.setWidget(form_host)
        right_layout.addWidget(scroll, stretch=1)

        self._test_output = QPlainTextEdit(right)
        self._test_output.setReadOnly(True)
        self._test_output.setMaximumHeight(120)
        self._test_output.setPlaceholderText("Test connection output appears here.")
        right_layout.addWidget(self._test_output)

        buttons = QHBoxLayout()
        self._new_btn = QPushButton("New")
        self._dup_btn = QPushButton("Duplicate")
        self._del_btn = QPushButton("Delete")
        self._import_btn = QPushButton("Import from .env")
        self._test_btn = QPushButton("Test connection")
        self._save_btn = QPushButton("Save")
        self._close_btn = QPushButton("Close")
        for b in (
            self._new_btn,
            self._dup_btn,
            self._del_btn,
            self._import_btn,
            self._test_btn,
            self._save_btn,
            self._close_btn,
        ):
            buttons.addWidget(b)
        right_layout.addLayout(buttons)

        self._new_btn.clicked.connect(self._on_new)
        self._dup_btn.clicked.connect(self._on_duplicate)
        self._del_btn.clicked.connect(self._on_delete)
        self._import_btn.clicked.connect(self._on_import_env)
        self._test_btn.clicked.connect(self._on_test_connection)
        self._save_btn.clicked.connect(self._on_save)
        self._close_btn.clicked.connect(self.accept)

        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

        outer = QVBoxLayout(self)
        outer.addWidget(splitter)

    def _add_field(self, form: QFormLayout, spec: FieldSpec) -> None:
        widget: QWidget
        if spec.kind in ("str", "path", "int"):
            widget = QLineEdit()
            if spec.placeholder:
                widget.setPlaceholderText(spec.placeholder)
            widget.textChanged.connect(self._update_visibility)
        elif spec.kind == "bool":
            widget = QCheckBox()
        elif spec.kind == "enum":
            widget = QComboBox()
            widget.addItems(list(spec.choices))
            widget.currentTextChanged.connect(self._update_visibility)
        elif spec.kind in ("list", "dict"):
            widget = QPlainTextEdit()
            widget.setMaximumHeight(70)
            if spec.placeholder:
                widget.setPlaceholderText(spec.placeholder)
        elif spec.kind == "secret":
            container = QWidget()
            row = QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            line = QLineEdit()
            line.setEchoMode(QLineEdit.EchoMode.Password)
            clear_btn = QToolButton()
            clear_btn.setText("Clear")
            row.addWidget(line)
            row.addWidget(clear_btn)
            container.line_edit = line  # type: ignore[attr-defined]
            container.clear_btn = clear_btn  # type: ignore[attr-defined]

            def make_clear(field_name: str, le: QLineEdit) -> Callable[[], None]:
                def _clear() -> None:
                    le.clear()
                    le.setPlaceholderText("")
                    self._secret_state[field_name] = True

                return _clear

            clear_btn.clicked.connect(make_clear(spec.name, line))
            line.textEdited.connect(lambda _text, fn=spec.name: self._secret_state.pop(fn, None))
            widget = container
        else:  # pragma: no cover - defensive
            raise ValueError(f"Unknown field kind: {spec.kind}")

        if spec.tooltip:
            widget.setToolTip(spec.tooltip)
        self._widgets[spec.name] = widget
        form.addRow(spec.label, widget)
        if spec.tooltip:
            label = form.labelForField(widget)
            if label is not None:
                label.setToolTip(spec.tooltip)

    # ----- list / selection ---------------------------------------------------
    def _reload_list(self, select: str | None = None) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        for profile in sorted(self._store.list(), key=lambda p: p.name):
            self._list.addItem(QListWidgetItem(profile.name))
        self._list.blockSignals(False)
        if select is not None:
            for i in range(self._list.count()):
                if self._list.item(i).text() == select:
                    self._list.setCurrentRow(i)
                    return
        if self._list.count():
            self._list.setCurrentRow(0)
        else:
            self._load_profile(None)

    def _on_selection_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            self._load_profile(None)
            return
        profile = self._store.get(current.text())
        self._load_profile(profile)

    # ----- form <-> profile ---------------------------------------------------
    def _load_profile(self, profile: Profile | None) -> None:
        self._current_name = profile.name if profile is not None else None
        self._secret_state.clear()
        self._test_output.clear()
        data = dataclasses.asdict(profile) if profile is not None else dataclasses.asdict(
            Profile(name="")
        )
        for spec in FIELDS:
            widget = self._widgets[spec.name]
            value = data.get(spec.name)
            self._set_field_value(spec, widget, value)
        self._refresh_secret_placeholders()
        self._update_visibility()

    def _set_field_value(self, spec: FieldSpec, widget: QWidget, value: Any) -> None:
        if spec.kind in ("str", "path"):
            widget.setText("" if value is None else str(value))
        elif spec.kind == "int":
            widget.setText("" if value is None else str(value))
        elif spec.kind == "bool":
            widget.setChecked(bool(value))
        elif spec.kind == "enum":
            widget.setCurrentText(str(value))
        elif spec.kind == "list":
            widget.setPlainText("\n".join(value or []))
        elif spec.kind == "dict":
            widget.setPlainText("\n".join(f"{k}={v}" for k, v in (value or {}).items()))
        elif spec.kind == "secret":
            widget.line_edit.clear()  # type: ignore[attr-defined]
            widget.line_edit.setPlaceholderText("")  # type: ignore[attr-defined]

    def _refresh_secret_placeholders(self) -> None:
        for field_name in SECRET_FIELDS:
            widget = self._widgets.get(field_name)
            if widget is None:
                continue
            line = widget.line_edit  # type: ignore[attr-defined]
            has_saved = (
                self._current_name is not None
                and self._store.has_secret(self._current_name, field_name)
            )
            line.setPlaceholderText(_SAVED_PLACEHOLDER if has_saved else "")

    def _current_values(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for spec in FIELDS:
            widget = self._widgets[spec.name]
            if spec.kind in ("str", "path"):
                values[spec.name] = widget.text().strip()
            elif spec.kind == "int":
                values[spec.name] = widget.text().strip()
            elif spec.kind == "bool":
                values[spec.name] = widget.isChecked()
            elif spec.kind == "enum":
                values[spec.name] = widget.currentText()
            elif spec.kind in ("list", "dict"):
                values[spec.name] = widget.toPlainText()
            elif spec.kind == "secret":
                values[spec.name] = widget.line_edit.text()  # type: ignore[attr-defined]
        return values

    def _update_visibility(self) -> None:
        values = self._current_values()
        for group, box in self._group_boxes.items():
            box.setVisible(not (group in REMOTE_ONLY_GROUPS and _mode_local(values)))
        for spec in FIELDS:
            widget = self._widgets[spec.name]
            row_label = None
            form: QFormLayout = widget.parentWidget().layout()  # type: ignore[assignment]
            if isinstance(form, QFormLayout):
                row_label = form.labelForField(widget)
            visible = spec.visible_when is None or spec.visible_when(values)
            widget.setVisible(visible)
            if row_label is not None:
                row_label.setVisible(visible)

    def _build_profile_from_form(self) -> Profile:
        values = self._current_values()

        def as_int(name: str, default: int | None) -> int | None:
            text = values[name]
            if not text:
                return default
            try:
                return int(text)
            except ValueError:
                return default

        kwargs: dict[str, Any] = {
            "name": values["name"].strip(),
            "mode": values["mode"],
            "local_fixture_dir": values["local_fixture_dir"] or None,
            "adls_account": values["adls_account"],
            "adls_auth": values["adls_auth"],
            "adls_tenant_id": values["adls_tenant_id"],
            "adls_client_id": values["adls_client_id"],
            "adls_ca_cert_file": values["adls_ca_cert_file"] or None,
            "nessie_uri": values["nessie_uri"],
            "nessie_auth": values["nessie_auth"],
            "nessie_token_endpoint": values["nessie_token_endpoint"],
            "nessie_client_id": values["nessie_client_id"],
            "nessie_scope": values["nessie_scope"],
            "nessie_main_ref": values["nessie_main_ref"] or "main",
            "nessie_head_ttl_seconds": as_int("nessie_head_ttl_seconds", 45),
            "shared_containers": _split_lines(values["shared_containers"]),
            "shared_namespaces": _split_lines(values["shared_namespaces"]),
            "tenant_registry_table": values["tenant_registry_table"] or None,
            "tenant_container_map": _parse_kv_lines(values["tenant_container_map"]),
            "tenant_data_source_map": _parse_kv_lines(values["tenant_data_source_map"]),
            "duckdb_memory_limit": values["duckdb_memory_limit"] or None,
            "duckdb_threads": as_int("duckdb_threads", None),
            "conn_cache_max": as_int("conn_cache_max", 16),
            "allow_export": values["allow_export"],
            "preview_row_limit": as_int("preview_row_limit", 1000),
            "restrict_file_access": values["restrict_file_access"],
        }
        return Profile(**kwargs)

    def _secrets_for_save(self) -> dict[str, str | None]:
        """None = leave unchanged, "" = delete, else = set to this value."""
        values = self._current_values()
        secrets: dict[str, str | None] = {}
        for field_name in SECRET_FIELDS:
            typed = values[field_name]
            if self._secret_state.get(field_name):  # explicitly cleared
                secrets[field_name] = ""
            elif typed:
                secrets[field_name] = typed
            else:
                secrets[field_name] = None
        return secrets

    def _secrets_for_test(self) -> dict[str, str | None]:
        """Actual values to use for "Test connection": saved value if untouched."""
        values = self._current_values()
        secrets: dict[str, str | None] = {}
        for field_name in SECRET_FIELDS:
            typed = values[field_name]
            if self._secret_state.get(field_name):
                secrets[field_name] = None
            elif typed:
                secrets[field_name] = typed
            elif self._current_name is not None:
                secrets[field_name] = self._store.get_secret(self._current_name, field_name)
            else:
                secrets[field_name] = None
        return secrets

    # ----- button handlers ------------------------------------------------
    def _on_new(self) -> None:
        self._list.setCurrentItem(None)
        self._load_profile(None)

    def _on_duplicate(self) -> None:
        if self._current_name is None:
            return
        new_name, ok = QInputDialog.getText(
            self, "Duplicate profile", "New profile name:", text=f"{self._current_name}-copy"
        )
        if not ok or not new_name.strip():
            return
        try:
            self._store.duplicate(self._current_name, new_name.strip())
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Duplicate profile", str(exc))
            return
        except FloeError as exc:
            QMessageBox.warning(self, "Duplicate profile", exc.user_message())
            return
        self._reload_list(select=new_name.strip())
        self.profilesChanged.emit()

    def _on_delete(self) -> None:
        if self._current_name is None:
            return
        confirm = QMessageBox.question(
            self,
            "Delete profile",
            f'Delete profile "{self._current_name}"? This cannot be undone.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._store.delete(self._current_name)
        except FloeError as exc:
            QMessageBox.warning(self, "Delete profile", exc.user_message())
            return
        self._reload_list()
        self.profilesChanged.emit()

    def _on_import_env(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import from .env", "", "Env files (*.env *)")
        if not path:
            return
        try:
            result = parse_env_file(path)
        except OSError as exc:
            QMessageBox.warning(self, "Import from .env", f"Could not read file: {exc}")
            return

        for field_name, value in result.fields.items():
            widget = self._widgets.get(field_name)
            if widget is None:
                continue
            spec = next(s for s in FIELDS if s.name == field_name)
            self._set_field_value(spec, widget, value)
        for field_name, value in result.secrets.items():
            widget = self._widgets.get(field_name)
            if widget is None:
                continue
            widget.line_edit.setText(value)  # type: ignore[attr-defined]
            self._secret_state.pop(field_name, None)
        self._update_visibility()

        if result.notes:
            QMessageBox.information(self, "Import from .env", "\n".join(result.notes))

    def _on_test_connection(self) -> None:
        profile = self._build_profile_from_form()
        try:
            secrets = self._secrets_for_test()
        except FloeError as exc:
            QMessageBox.warning(self, "Test connection", exc.user_message())
            return
        self._test_output.clear()
        self._test_btn.setEnabled(False)

        def run_steps(progress: Callable[[Any], None]) -> None:
            for step in run_connection_test(profile, secrets):
                progress(step)

        worker = Worker(run_steps)
        worker.signals.progress.connect(self._append_test_step)
        worker.signals.finished.connect(lambda: self._test_btn.setEnabled(True))
        worker.signals.error.connect(self._on_test_error)
        self._test_worker = worker
        run_in_background(worker)

    def _append_test_step(self, step: Any) -> None:
        status = "PASS" if step.ok else "FAIL"
        self._test_output.appendPlainText(f"[{status}] {step.name}: {step.message}")

    def _on_test_error(self, exc: Exception) -> None:
        self._test_output.appendPlainText(f"[ERROR] {user_error_text(exc)}")

    def _on_save(self) -> None:
        profile = self._build_profile_from_form()
        if not profile.name:
            QMessageBox.warning(self, "Save profile", "Profile name is required.")
            return
        existing_names = {p.name for p in self._store.list()}
        if self._current_name is None and profile.name in existing_names:
            QMessageBox.warning(self, "Save profile", f'Profile "{profile.name}" already exists.')
            return
        if profile.mode == "local" and not profile.local_fixture_dir:
            QMessageBox.warning(
                self, "Save profile", "Local fixture directory is required for local mode."
            )
            return
        if profile.mode == "remote" and not profile.nessie_uri:
            QMessageBox.warning(self, "Save profile", "Nessie URI is required.")
            return

        rename_from = (
            self._current_name
            if self._current_name is not None and self._current_name != profile.name
            else None
        )
        if rename_from is not None and profile.name in existing_names:
            QMessageBox.warning(self, "Save profile", f'Profile "{profile.name}" already exists.')
            return

        try:
            if rename_from is not None:
                self._store.rename(rename_from, profile.name)

            secrets = self._secrets_for_save()
            self._store.save(profile, secrets=secrets)
        except FloeError as exc:
            QMessageBox.warning(self, "Save profile", exc.user_message())
            return
        self._current_name = profile.name
        self._secret_state.clear()
        self._reload_list(select=profile.name)
        self.profilesChanged.emit()
