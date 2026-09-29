"""Profile dataclass, JSON load/save, keyring secrets, and .env import (SPEC §4)."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import keyring
import keyring.backends.chainer
import keyring.backends.fail
import keyring.backends.null
import keyring.errors

from floe.core import diagnostics, paths
from floe.core.errors import KeychainError

SERVICE_NAME = "Floe"

_ENV_SANITIZE_RE = re.compile(r"[^A-Za-z0-9]")


def secret_env_var_name(profile: str, field_name: str) -> str:
    """Env var Floe reads a secret from when no usable OS keyring exists (SPEC
    §15.2): FLOE_SECRET__<PROFILE>__<FIELD>, with the profile name and field
    upper-cased and every non-alphanumeric character turned into `_`."""
    profile_part = _ENV_SANITIZE_RE.sub("_", profile).upper()
    field_part = _ENV_SANITIZE_RE.sub("_", field_name).upper()
    return f"FLOE_SECRET__{profile_part}__{field_part}"


def _keyring_usable() -> bool:
    """False when the active keyring backend can't actually store anything:
    the `fail` backend (no recommended backend found), the `null` backend, or
    a `chainer` backend with nothing viable to chain to."""
    backend = keyring.get_keyring()
    if isinstance(backend, keyring.backends.fail.Keyring):
        return False
    if isinstance(backend, keyring.backends.null.Keyring):
        return False
    if isinstance(backend, keyring.backends.chainer.ChainerBackend):
        return len(backend.backends) > 0
    return True


def keyring_usable() -> bool:
    """Public form of `_keyring_usable` (the web UI explains the env-var fallback)."""
    return _keyring_usable()


log = logging.getLogger("floe.core.profiles")

Mode = Literal["remote", "local"]
AdlsAuth = Literal["account_key", "service_principal"]
NessieAuth = Literal["oauth2", "none"]

# Secret fields live only in the keyring, never in profiles.json.
SECRET_FIELDS = ("adls_account_key", "adls_client_secret", "nessie_client_secret")


@dataclass
class Profile:
    """A saved connection profile (SPEC §4.2). Secret fields are not stored here."""

    name: str
    mode: Mode = "remote"
    local_fixture_dir: str | None = None

    # ADLS
    adls_account: str = ""
    adls_auth: AdlsAuth = "account_key"
    adls_tenant_id: str = ""
    adls_client_id: str = ""
    adls_ca_cert_file: str | None = None

    # Nessie
    nessie_uri: str = ""
    nessie_auth: NessieAuth = "oauth2"
    nessie_token_endpoint: str = ""
    nessie_client_id: str = ""
    nessie_scope: str = ""
    nessie_main_ref: str = "main"
    nessie_head_ttl_seconds: int = 45

    # Tenant scoping
    shared_containers: list[str] = field(default_factory=list)
    shared_namespaces: list[str] = field(default_factory=list)
    tenant_registry_table: str | None = None
    tenant_container_map: dict[str, str] = field(default_factory=dict)
    tenant_data_source_map: dict[str, str] = field(default_factory=dict)

    # DuckDB tuning
    duckdb_memory_limit: str | None = None
    duckdb_threads: int | None = None
    conn_cache_max: int = 16

    # Safety
    allow_export: bool = False
    preview_row_limit: int = 1000
    # Escape hatch: when False, DuckDB file access is not restricted to the ref's
    # containers (the SQL table-function blocklist still applies).
    restrict_file_access: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Non-secret fields only, JSON-serializable."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        """Build a Profile from a dict, ignoring unknown keys and filling defaults."""
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)


@dataclass
class EnvImport:
    """The result of parsing a .env file (SPEC §4.3). Nothing is saved here."""

    fields: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_file(path: str | Path) -> EnvImport:
    """Parse a simple KEY=VALUE .env file per SPEC §4.3. Saves nothing."""
    return parse_env_text(Path(path).read_text(encoding="utf-8"))


def parse_env_text(text: str) -> EnvImport:
    """Parse .env file *contents* per SPEC §4.3 (e.g. an upload that must never be
    written to disk). Saves nothing."""
    result = EnvImport()
    raw: dict[str, str] = {}

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key:
            continue
        raw[key] = _strip_quotes(value)

    def get(*names: str) -> str | None:
        for n in names:
            if n in raw and raw[n] != "":
                return raw[n]
        return None

    account = get("ADLS_ACCOUNT")
    if account is not None:
        result.fields["adls_account"] = account

    account_key = get("ADLS_ACCOUNT_KEY", "AZURE_STORAGE_KEY")
    tenant_id = get("ADLS_TENANT_ID")
    client_id = get("ADLS_CLIENT_ID")
    client_secret = get("ADLS_CLIENT_SECRET")

    if tenant_id is not None:
        result.fields["adls_tenant_id"] = tenant_id
    if client_id is not None:
        result.fields["adls_client_id"] = client_id

    if account_key:
        result.fields["adls_auth"] = "account_key"
        result.secrets["adls_account_key"] = account_key
    elif client_id or client_secret or tenant_id:
        result.fields["adls_auth"] = "service_principal"

    if client_secret:
        result.secrets["adls_client_secret"] = client_secret

    ca_cert_file = get("ADLS_CA_CERT_FILE")
    if ca_cert_file is not None:
        result.fields["adls_ca_cert_file"] = ca_cert_file

    nessie_uri = get("NESSIE_URI")
    if nessie_uri is not None:
        result.fields["nessie_uri"] = nessie_uri

    nessie_client_id = get("NESSIE_CLIENT_ID")
    if nessie_client_id is not None:
        result.fields["nessie_client_id"] = nessie_client_id

    nessie_token_endpoint = get("NESSIE_TOKEN_ENDPOINT")
    if nessie_token_endpoint is not None:
        result.fields["nessie_token_endpoint"] = nessie_token_endpoint

    nessie_scope = get("NESSIE_SCOPE")
    if nessie_scope is not None:
        result.fields["nessie_scope"] = nessie_scope

    nessie_client_secret = get("NESSIE_CLIENT_SECRET")
    if nessie_client_secret is not None:
        result.secrets["nessie_client_secret"] = nessie_client_secret

    nessie_auth_mode = get("NESSIE_AUTH_MODE")
    if nessie_auth_mode is not None:
        result.fields["nessie_auth"] = nessie_auth_mode

    nessie_main_ref = get("NESSIE_MAIN_REF")
    if nessie_main_ref is not None:
        result.fields["nessie_main_ref"] = nessie_main_ref

    nessie_head_ttl_seconds = get("NESSIE_HEAD_TTL_SECONDS")
    if nessie_head_ttl_seconds is not None:
        result.fields["nessie_head_ttl_seconds"] = int(nessie_head_ttl_seconds)

    gold_ref_container = get("GOLD_REF_CONTAINER")
    if gold_ref_container is not None:
        result.fields.setdefault("shared_containers", [])
        result.fields["shared_containers"].append(gold_ref_container)

    duckdb_memory_limit = get("DUCKDB_MEMORY_LIMIT")
    if duckdb_memory_limit is not None:
        result.fields["duckdb_memory_limit"] = duckdb_memory_limit

    duckdb_threads = get("DUCKDB_THREADS")
    if duckdb_threads is not None:
        result.fields["duckdb_threads"] = int(duckdb_threads)

    conn_cache_max = get("CONN_CACHE_MAX")
    if conn_cache_max is not None:
        result.fields["conn_cache_max"] = int(conn_cache_max)

    if get("NESSIE_KEY_VAULT") is not None or get("NESSIE_SECRET_NAME") is not None:
        result.notes.append(
            "Key Vault auth isn't supported from a laptop; enter the Nessie "
            "client secret directly instead."
        )

    return result


# --------------------------------------------------------------------------- profile files
# Export / import of a profile as a shareable JSON file. Settings only: secrets are never
# written to, or read from, such a file.

EXPORT_FORMAT = "floe-profile"
EXPORT_VERSION = 1
MAX_EXPORT_BYTES = 256 * 1024
_MAX_NAME = 100
_MAX_TEXT = 4096
_MAX_INT = {"preview_row_limit": 1_000_000}

# Validation kind per Profile field (kept in sync with the dataclass by a test).
IMPORT_FIELD_KINDS: dict[str, Any] = {
    "name": "name",
    "mode": ("remote", "local"),
    "local_fixture_dir": "opt_str",
    "adls_account": "str",
    "adls_auth": ("account_key", "service_principal"),
    "adls_tenant_id": "str",
    "adls_client_id": "str",
    "adls_ca_cert_file": "opt_str",
    "nessie_uri": "str",
    "nessie_auth": ("oauth2", "none"),
    "nessie_token_endpoint": "str",
    "nessie_client_id": "str",
    "nessie_scope": "str",
    "nessie_main_ref": "str",
    "nessie_head_ttl_seconds": "int",
    "shared_containers": "str_list",
    "shared_namespaces": "str_list",
    "tenant_registry_table": "opt_str",
    "tenant_container_map": "str_map",
    "tenant_data_source_map": "str_map",
    "duckdb_memory_limit": "opt_str",
    "duckdb_threads": "opt_int",
    "conn_cache_max": "pos_int",
    "allow_export": "bool",
    "preview_row_limit": "pos_int",
    "restrict_file_access": "bool",
}

SECRETS_NOT_IMPORTED_NOTE = (
    "The file contained secret fields, which were ignored: secrets are never imported "
    "from files — enter them manually."
)


class ProfileImportError(ValueError):
    """A profile file was rejected. `errors` lists every problem found (never values)."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class ProfileImport:
    """The result of parsing a profile file. Nothing is saved here."""

    fields: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    missing_secrets: list[str] = field(default_factory=list)


def required_secrets(profile: Profile) -> list[str]:
    """The secret fields the profile's chosen auth modes need (none in local mode)."""
    if profile.mode == "local":
        return []
    needed = {"adls_account_key" if profile.adls_auth == "account_key" else "adls_client_secret"}
    if profile.nessie_auth == "oauth2":
        needed.add("nessie_client_secret")
    return [f for f in SECRET_FIELDS if f in needed]


def export_profile(profile: Profile) -> dict[str, Any]:
    """The shareable form of `profile`: non-secret fields only, no secret keys at all."""
    from floe import __version__

    data = {k: v for k, v in profile.to_dict().items() if k not in SECRET_FIELDS}
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "floe_version": __version__,
        "profile": data,
    }


def _check_field(key: str, kind: Any, value: object, errors: list[str]) -> Any:
    def bad(msg: str) -> None:
        errors.append(f"{key}: {msg}")

    def text(v: object) -> str | None:
        if not isinstance(v, str) or len(v) > _MAX_TEXT:
            return None
        return v.strip()

    def integer(v: object, minimum: int) -> int | None:
        if not isinstance(v, int) or isinstance(v, bool) or v < minimum:
            return None
        return v

    if kind == "name":
        v = text(value)
        if not v:
            bad("must be a non-empty string.")
        elif len(v) > _MAX_NAME:
            bad(f"is too long (max {_MAX_NAME} characters).")
        elif any(ord(c) < 32 or c in "\x7f:" for c in v):
            bad("may not contain control characters or ':'.")
        return v
    if isinstance(kind, tuple):
        if value not in kind:
            bad(f"must be one of {', '.join(kind)}.")
        return value
    if kind == "str":
        v = text(value)
        if v is None:
            bad(f"must be a string (max {_MAX_TEXT} characters).")
        return v
    if kind == "opt_str":
        if value is None:
            return None
        v = text(value)
        if v is None:
            bad(f"must be a string or null (max {_MAX_TEXT} characters).")
        return v or None
    if kind in ("int", "pos_int", "opt_int"):
        if value is None and kind == "opt_int":
            return None
        minimum = 0 if kind == "int" else 1
        n = integer(value, minimum)
        if n is None:
            suffix = " or null." if kind == "opt_int" else "."
            bad(f"must be a whole number >= {minimum}{suffix}")
        elif key in _MAX_INT and n > _MAX_INT[key]:
            bad(f"must be at most {_MAX_INT[key]:,}.")
        return n
    if kind == "bool":
        if not isinstance(value, bool):
            bad("must be true or false.")
        return value
    if kind == "str_list":
        if not isinstance(value, list):
            bad("must be a list of strings.")
            return None
        items = [text(v) for v in value]
        if any(i is None for i in items):
            bad("must be a list of strings.")
            return None
        return [i for i in items if i]
    if kind == "str_map":
        if not isinstance(value, dict):
            bad("must be an object of strings.")
            return None
        out: dict[str, str] = {}
        for k, v in value.items():
            tv = text(v)
            if tv is None:
                bad("must be an object of strings.")
                return None
            if k.strip():
                out[k.strip()] = tv
        return out
    return value  # pragma: no cover


def parse_profile_export(text: str) -> ProfileImport:
    """Strictly parse the contents of a profile file (see `export_profile`). Saves nothing.
    Raises `ProfileImportError` (messages never contain file values). Secret-named keys
    are ignored with a warning; unknown fields are dropped with a note."""
    if not isinstance(text, str):
        raise ProfileImportError(["The profile file must be text."])
    if len(text.encode("utf-8", errors="replace")) > MAX_EXPORT_BYTES:
        raise ProfileImportError(
            [f"The profile file is too large (max {MAX_EXPORT_BYTES // 1024} KB)."]
        )
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        raise ProfileImportError(["The file is not valid JSON."]) from None
    if not isinstance(data, dict):
        raise ProfileImportError(["The file must contain a JSON object."])
    if data.get("format") != EXPORT_FORMAT:
        raise ProfileImportError([f'Not a Floe profile file (format must be "{EXPORT_FORMAT}").'])
    version = data.get("version")
    if isinstance(version, bool) or version != EXPORT_VERSION or not isinstance(version, int):
        raise ProfileImportError([f"Unsupported profile file version (expected {EXPORT_VERSION})."])
    body = data.get("profile")
    if not isinstance(body, dict):
        raise ProfileImportError(['"profile" must be an object.'])

    result = ProfileImport()
    secret_keys = (set(body) | set(data)) & set(SECRET_FIELDS)
    if secret_keys:
        result.notes.append(SECRETS_NOT_IMPORTED_NOTE)
    unknown = sorted(str(k) for k in body if k not in IMPORT_FIELD_KINDS and k not in SECRET_FIELDS)
    if unknown:
        result.notes.append(f"Ignored unknown field(s): {', '.join(unknown)}.")

    errors: list[str] = []
    if "name" not in body:
        errors.append("name: is required.")
    for key, kind in IMPORT_FIELD_KINDS.items():
        if key in body:
            result.fields[key] = _check_field(key, kind, body[key], errors)
    if errors:
        raise ProfileImportError(errors)
    profile = Profile.from_dict(result.fields)
    if profile.mode == "local" and not profile.local_fixture_dir:
        result.notes.append("Local mode needs a local fixture directory; set one before saving.")
    result.missing_secrets = required_secrets(profile)
    return result


class ProfileStore:
    """JSON store for non-secret profile fields, plus keyring-backed secrets."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else paths.app_support_dir() / "profiles.json"

    @property
    def path(self) -> Path:
        return self._path

    def _read_all(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _write_all(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".profiles-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._path)
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        os.chmod(self._path, 0o600)

    def list(self) -> list[Profile]:
        data = self._read_all()
        return [Profile.from_dict(v) for v in data.values()]

    def get(self, name: str) -> Profile | None:
        data = self._read_all()
        entry = data.get(name)
        if entry is None:
            return None
        return Profile.from_dict(entry)

    def save(self, profile: Profile, secrets: dict[str, str | None] | None = None) -> None:
        """Update keyring secrets, then save non-secret fields to JSON.

        In `secrets`, a value of None leaves the existing keyring entry
        unchanged; an empty string ("") deletes it; any other value sets it.

        Secrets are written first so that, if a keyring write fails, the JSON
        file is left untouched rather than getting out of sync with what's
        actually in the Keychain.
        """
        if secrets:
            keyring_usable = _keyring_usable()
            for field_name, value in secrets.items():
                if field_name not in SECRET_FIELDS:
                    continue
                if value is None:
                    continue
                if not keyring_usable:
                    if value == "":
                        # Nothing was ever stored in the keyring to delete; an
                        # env-var secret (if any) is managed outside Floe.
                        continue
                    raise KeychainError(
                        field_name, "save", secret_env_var_name(profile.name, field_name)
                    )
                username = f"{profile.name}:{field_name}"
                if value == "":
                    try:
                        keyring.delete_password(SERVICE_NAME, username)
                    except keyring.errors.PasswordDeleteError:
                        pass
                    except keyring.errors.KeyringError as exc:
                        log.warning(
                            "Keychain delete failed for field=%s error=%s",
                            field_name,
                            type(exc).__name__,
                        )
                        raise KeychainError(field_name, "delete") from exc
                else:
                    try:
                        keyring.set_password(SERVICE_NAME, username, value)
                    except keyring.errors.KeyringError as exc:
                        log.warning(
                            "Keychain save failed for field=%s error=%s",
                            field_name,
                            type(exc).__name__,
                        )
                        raise KeychainError(field_name, "save") from exc
                    diagnostics.register_secret(value)

        data = self._read_all()
        data[profile.name] = profile.to_dict()
        self._write_all(data)

    def get_secret(self, name: str, field_name: str) -> str | None:
        value: str | None = None
        if _keyring_usable():
            try:
                value = keyring.get_password(SERVICE_NAME, f"{name}:{field_name}")
            except keyring.errors.KeyringError as exc:
                log.warning(
                    "Keychain read failed for field=%s error=%s", field_name, type(exc).__name__
                )
                raise KeychainError(field_name, "read") from exc
        if not value:
            # No usable keyring, or the keyring has no entry: fall back to the
            # env var (SPEC §15.2).
            value = os.environ.get(secret_env_var_name(name, field_name)) or None
        if value:
            diagnostics.register_secret(value)
        return value

    def has_secret(self, name: str, field_name: str) -> bool:
        try:
            return self.get_secret(name, field_name) is not None
        except KeychainError as exc:
            log.warning(
                "Keychain has_secret check failed for field=%s error=%s",
                field_name,
                type(exc.__cause__).__name__ if exc.__cause__ else "unknown",
            )
            return False

    def rename(self, old: str, new: str) -> None:
        data = self._read_all()
        if old not in data:
            raise KeyError(f"No such profile: {old}")
        if new in data:
            raise ValueError(f"Profile already exists: {new}")

        moved: list[str] = []
        try:
            for field_name in SECRET_FIELDS:
                value = keyring.get_password(SERVICE_NAME, f"{old}:{field_name}")
                if value is not None:
                    keyring.set_password(SERVICE_NAME, f"{new}:{field_name}", value)
                    moved.append(field_name)
        except keyring.errors.KeyringError as exc:
            # Roll back anything already copied to `new` before this field failed.
            for field_name in moved:
                try:
                    keyring.delete_password(SERVICE_NAME, f"{new}:{field_name}")
                except keyring.errors.KeyringError:
                    pass
            log.warning("Keychain rename failed error=%s", type(exc).__name__)
            raise KeychainError("secret", "rename") from exc

        entry = data.pop(old)
        entry["name"] = new
        data[new] = entry
        self._write_all(data)

        for field_name in moved:
            try:
                keyring.delete_password(SERVICE_NAME, f"{old}:{field_name}")
            except keyring.errors.PasswordDeleteError:
                pass

    def delete(self, name: str) -> None:
        errors: list[str] = []
        for field_name in SECRET_FIELDS:
            try:
                keyring.delete_password(SERVICE_NAME, f"{name}:{field_name}")
            except keyring.errors.PasswordDeleteError:
                pass
            except keyring.errors.KeyringError as exc:
                log.warning(
                    "Keychain delete failed for field=%s error=%s",
                    field_name,
                    type(exc).__name__,
                )
                errors.append(field_name)
        if errors:
            raise KeychainError(errors[0], "delete")

        data = self._read_all()
        data.pop(name, None)
        self._write_all(data)

    def duplicate(self, name: str, new_name: str) -> Profile:
        data = self._read_all()
        if name not in data:
            raise KeyError(f"No such profile: {name}")
        if new_name in data:
            raise ValueError(f"Profile already exists: {new_name}")

        copied: list[str] = []
        try:
            for field_name in SECRET_FIELDS:
                value = keyring.get_password(SERVICE_NAME, f"{name}:{field_name}")
                if value is not None:
                    keyring.set_password(SERVICE_NAME, f"{new_name}:{field_name}", value)
                    copied.append(field_name)
        except keyring.errors.KeyringError as exc:
            for field_name in copied:
                try:
                    keyring.delete_password(SERVICE_NAME, f"{new_name}:{field_name}")
                except keyring.errors.KeyringError:
                    pass
            log.warning("Keychain duplicate failed error=%s", type(exc).__name__)
            raise KeychainError("secret", "duplicate") from exc

        entry = dict(data[name])
        entry["name"] = new_name
        data[new_name] = entry
        self._write_all(data)

        return Profile.from_dict(entry)
