"""Tests for floe.core.profiles (SPEC §4, §13.2)."""

from __future__ import annotations

import json
import sys

import keyring
import keyring.backends.chainer
import keyring.backends.fail
import pytest

from floe.core.errors import KeychainError
from floe.core.profiles import (
    SECRET_FIELDS,
    Profile,
    ProfileStore,
    parse_env_file,
    secret_env_var_name,
)
from tests.conftest import RaisingKeyring


def _store(tmp_path) -> ProfileStore:
    return ProfileStore(tmp_path / "profiles.json")


def test_round_trip_defaults(tmp_path):
    store = _store(tmp_path)
    profile = Profile(name="eg-test1")
    store.save(profile)

    loaded = store.get("eg-test1")
    assert loaded is not None
    assert loaded == profile
    assert loaded.mode == "remote"
    assert loaded.nessie_main_ref == "main"
    assert loaded.nessie_head_ttl_seconds == 45
    assert loaded.conn_cache_max == 16
    assert loaded.preview_row_limit == 1000
    assert loaded.allow_export is False
    assert loaded.shared_containers == []
    assert loaded.shared_namespaces == []
    assert loaded.tenant_container_map == {}
    assert loaded.tenant_data_source_map == {}
    assert loaded.adls_ca_cert_file is None
    assert loaded.restrict_file_access is True


def test_restrict_file_access_persists(tmp_path):
    store = _store(tmp_path)
    store.save(Profile(name="eg-open", restrict_file_access=False))
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["eg-open"]["restrict_file_access"] is False
    assert store.get("eg-open").restrict_file_access is False


def test_from_dict_ignores_unknown_and_fills_missing():
    data = {"name": "eg-test1", "unknown_field": "x", "adls_account": "acct-a"}
    profile = Profile.from_dict(data)
    assert profile.name == "eg-test1"
    assert profile.adls_account == "acct-a"
    assert profile.mode == "remote"
    assert not hasattr(profile, "unknown_field")


def test_secrets_saved_to_keyring_and_never_in_json(tmp_path):
    store = _store(tmp_path)
    profile = Profile(name="eg-test1", adls_auth="account_key")
    secret_value = "SUPER-SECRET-ACCOUNT-KEY-VALUE"
    store.save(profile, secrets={"adls_account_key": secret_value})

    raw_text = store.path.read_text(encoding="utf-8")
    assert secret_value not in raw_text
    data = json.loads(raw_text)
    assert "adls_account_key" not in data["eg-test1"]

    assert store.get_secret("eg-test1", "adls_account_key") == secret_value
    assert store.has_secret("eg-test1", "adls_account_key")
    assert not store.has_secret("eg-test1", "adls_client_secret")


def test_save_secret_none_leaves_unchanged_and_empty_deletes(tmp_path):
    store = _store(tmp_path)
    profile = Profile(name="eg-test1")
    store.save(profile, secrets={"adls_account_key": "value-one-xyz"})
    store.save(profile, secrets={"adls_account_key": None})
    assert store.get_secret("eg-test1", "adls_account_key") == "value-one-xyz"

    store.save(profile, secrets={"adls_account_key": ""})
    assert store.get_secret("eg-test1", "adls_account_key") is None


def test_save_rolls_back_when_keyring_write_fails(tmp_path):
    """If the keyring write fails, profiles.json must not be written (or updated) either
    -- the two must stay consistent (SPEC secrets rule)."""
    store = _store(tmp_path)
    profile = Profile(name="eg-test1")

    keyring.set_keyring(RaisingKeyring())
    with pytest.raises(KeychainError) as excinfo:
        store.save(profile, secrets={"adls_account_key": "value-one-xyz"})

    assert not store.path.exists()
    assert "adls_account_key" in excinfo.value.user_message()
    assert "value-one-xyz" not in excinfo.value.user_message()


def test_save_keeps_json_unchanged_when_keyring_write_fails_on_existing_profile(tmp_path):
    store = _store(tmp_path)
    profile = Profile(name="eg-test1", adls_account="acct-a")
    store.save(profile)

    keyring.set_keyring(RaisingKeyring())
    updated = Profile(name="eg-test1", adls_account="acct-b")
    with pytest.raises(KeychainError):
        store.save(updated, secrets={"adls_account_key": "value-one-xyz"})

    # The JSON file must still reflect the last successful save, not the failed one.
    reloaded = store.get("eg-test1")
    assert reloaded is not None
    assert reloaded.adls_account == "acct-a"


def test_get_secret_raises_keychain_error_on_read_failure(tmp_path):
    store = _store(tmp_path)
    keyring.set_keyring(RaisingKeyring())
    with pytest.raises(KeychainError):
        store.get_secret("eg-test1", "adls_account_key")


def test_has_secret_returns_false_on_read_failure(tmp_path):
    store = _store(tmp_path)
    keyring.set_keyring(RaisingKeyring())
    assert store.has_secret("eg-test1", "adls_account_key") is False


def test_delete_raises_keychain_error_but_does_not_desync(tmp_path):
    store = _store(tmp_path)
    profile = Profile(name="eg-test1")
    store.save(profile)

    keyring.set_keyring(RaisingKeyring())
    with pytest.raises(KeychainError):
        store.delete("eg-test1")

    # The profile must still be listed: we didn't remove the JSON entry for a
    # profile whose secrets we failed to clear.
    assert store.get("eg-test1") is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits don't apply on Windows")
def test_profile_file_permissions(tmp_path):
    store = _store(tmp_path)
    store.save(Profile(name="eg-test1"))
    mode = store.path.stat().st_mode & 0o777
    assert mode == 0o600


def test_rename_moves_keyring_entries(tmp_path):
    store = _store(tmp_path)
    profile = Profile(name="eg-test1")
    store.save(profile, secrets={"adls_account_key": "key-value-abc"})

    store.rename("eg-test1", "eg-test2")

    assert store.get("eg-test1") is None
    renamed = store.get("eg-test2")
    assert renamed is not None
    assert renamed.name == "eg-test2"
    assert store.get_secret("eg-test1", "adls_account_key") is None
    assert store.get_secret("eg-test2", "adls_account_key") == "key-value-abc"


def test_rename_errors_if_new_name_exists(tmp_path):
    store = _store(tmp_path)
    store.save(Profile(name="eg-test1"))
    store.save(Profile(name="eg-test2"))
    with pytest.raises(ValueError):
        store.rename("eg-test1", "eg-test2")


def test_delete_removes_keyring_entries(tmp_path):
    store = _store(tmp_path)
    profile = Profile(name="eg-test1")
    store.save(
        profile,
        secrets={
            "adls_account_key": "key-value-abc",
            "nessie_client_secret": "nessie-secret-xyz",
        },
    )

    store.delete("eg-test1")

    assert store.get("eg-test1") is None
    for field_name in SECRET_FIELDS:
        assert store.get_secret("eg-test1", field_name) is None


def test_duplicate_copies_fields_and_secrets(tmp_path):
    store = _store(tmp_path)
    profile = Profile(name="eg-test1", adls_account="acct-a")
    store.save(profile, secrets={"adls_account_key": "key-value-abc"})

    duplicated = store.duplicate("eg-test1", "eg-test1-copy")

    assert duplicated.name == "eg-test1-copy"
    assert duplicated.adls_account == "acct-a"
    original = store.get("eg-test1")
    assert original is not None
    assert store.get_secret("eg-test1-copy", "adls_account_key") == "key-value-abc"
    # Original is untouched.
    assert store.get_secret("eg-test1", "adls_account_key") == "key-value-abc"


def test_duplicate_errors_if_new_name_exists(tmp_path):
    store = _store(tmp_path)
    store.save(Profile(name="eg-test1"))
    store.save(Profile(name="eg-test2"))
    with pytest.raises(ValueError):
        store.duplicate("eg-test1", "eg-test2")


def test_list_returns_all_profiles(tmp_path):
    store = _store(tmp_path)
    store.save(Profile(name="eg-test1"))
    store.save(Profile(name="eg-test2"))
    names = sorted(p.name for p in store.list())
    assert names == ["eg-test1", "eg-test2"]


_ENV_TEXT = """
# a comment
export ADLS_ACCOUNT=acct-a
ADLS_ACCOUNT_KEY="key-value-abc"
ADLS_TENANT_ID=tenant-a
ADLS_CLIENT_ID=client-a
ADLS_CLIENT_SECRET=client-secret-a
ADLS_CA_CERT_FILE=/tmp/eg-ca.pem
NESSIE_URI=https://nessie.example.test
NESSIE_CLIENT_ID=nessie-client-a
NESSIE_TOKEN_ENDPOINT=https://auth.example.test/token
NESSIE_SCOPE=eg-scope
NESSIE_CLIENT_SECRET=nessie-secret-a
NESSIE_AUTH_MODE=oauth2
NESSIE_MAIN_REF=main
NESSIE_HEAD_TTL_SECONDS=60
GOLD_REF_CONTAINER=container-gold
DUCKDB_MEMORY_LIMIT=4GB
DUCKDB_THREADS=4
CONN_CACHE_MAX=32
NESSIE_KEY_VAULT=some-vault
NESSIE_SECRET_NAME=some-secret
"""


def test_parse_env_file_maps_every_variable(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(_ENV_TEXT, encoding="utf-8")

    result = parse_env_file(env_path)

    assert result.fields["adls_account"] == "acct-a"
    assert result.fields["adls_auth"] == "account_key"
    assert result.fields["adls_tenant_id"] == "tenant-a"
    assert result.fields["adls_client_id"] == "client-a"
    assert result.fields["adls_ca_cert_file"] == "/tmp/eg-ca.pem"
    assert result.fields["nessie_uri"] == "https://nessie.example.test"
    assert result.fields["nessie_client_id"] == "nessie-client-a"
    assert result.fields["nessie_token_endpoint"] == "https://auth.example.test/token"
    assert result.fields["nessie_scope"] == "eg-scope"
    assert result.fields["nessie_auth"] == "oauth2"
    assert result.fields["nessie_main_ref"] == "main"
    assert result.fields["nessie_head_ttl_seconds"] == 60
    assert result.fields["shared_containers"] == ["container-gold"]
    assert result.fields["duckdb_memory_limit"] == "4GB"
    assert result.fields["duckdb_threads"] == 4
    assert result.fields["conn_cache_max"] == 32

    assert result.secrets["adls_account_key"] == "key-value-abc"
    assert result.secrets["adls_client_secret"] == "client-secret-a"
    assert result.secrets["nessie_client_secret"] == "nessie-secret-a"

    assert any("key vault" in note.lower() for note in result.notes)

    # Nothing is written anywhere.
    assert not (tmp_path / "profiles.json").exists()


def test_parse_env_file_account_key_alias(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "ADLS_ACCOUNT=acct-a\nAZURE_STORAGE_KEY=alias-key-value\n", encoding="utf-8"
    )

    result = parse_env_file(env_path)

    assert result.secrets["adls_account_key"] == "alias-key-value"
    assert result.fields["adls_auth"] == "account_key"


def test_parse_env_file_service_principal_without_account_key(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "ADLS_TENANT_ID=tenant-a\nADLS_CLIENT_ID=client-a\nADLS_CLIENT_SECRET=secret-a\n",
        encoding="utf-8",
    )

    result = parse_env_file(env_path)

    assert result.fields["adls_auth"] == "service_principal"
    assert "adls_account_key" not in result.secrets
    assert result.secrets["adls_client_secret"] == "secret-a"


def test_secret_env_var_name_sanitizes_and_upcases():
    assert (
        secret_env_var_name("eg-test 1!", "adls_account_key")
        == "FLOE_SECRET__EG_TEST_1___ADLS_ACCOUNT_KEY"
    )


def test_get_secret_falls_back_to_env_var_when_no_keyring(tmp_path, monkeypatch):
    """SPEC §15.2: with no usable OS keyring, secrets come from env vars."""
    store = _store(tmp_path)
    keyring.set_keyring(keyring.backends.fail.Keyring())
    var = secret_env_var_name("eg-test1", "adls_account_key")
    monkeypatch.setenv(var, "env-secret-value")

    assert store.get_secret("eg-test1", "adls_account_key") == "env-secret-value"
    assert store.has_secret("eg-test1", "adls_account_key")


def test_get_secret_no_keyring_and_no_env_var_is_none(tmp_path, monkeypatch):
    store = _store(tmp_path)
    keyring.set_keyring(keyring.backends.fail.Keyring())
    monkeypatch.delenv(secret_env_var_name("eg-test1", "adls_account_key"), raising=False)

    assert store.get_secret("eg-test1", "adls_account_key") is None
    assert not store.has_secret("eg-test1", "adls_account_key")


def test_save_secret_with_no_keyring_raises_naming_env_var(tmp_path):
    store = _store(tmp_path)
    keyring.set_keyring(keyring.backends.fail.Keyring())
    profile = Profile(name="eg-test1")

    with pytest.raises(KeychainError) as excinfo:
        store.save(profile, secrets={"adls_account_key": "super-secret-value"})

    msg = excinfo.value.user_message()
    assert secret_env_var_name("eg-test1", "adls_account_key") in msg
    assert "super-secret-value" not in msg
    assert not store.path.exists()


def test_save_non_secret_fields_still_works_with_no_keyring(tmp_path):
    """Only saving a *secret* requires a keyring; plain fields must still save."""
    store = _store(tmp_path)
    keyring.set_keyring(keyring.backends.fail.Keyring())
    store.save(Profile(name="eg-test1", adls_account="acct-a"))

    loaded = store.get("eg-test1")
    assert loaded is not None
    assert loaded.adls_account == "acct-a"


def test_save_deleting_secret_with_no_keyring_is_a_noop(tmp_path):
    store = _store(tmp_path)
    keyring.set_keyring(keyring.backends.fail.Keyring())
    store.save(Profile(name="eg-test1"), secrets={"adls_account_key": ""})
    assert store.get("eg-test1") is not None


def test_env_var_is_a_fallback_even_when_keyring_is_usable(tmp_path, monkeypatch):
    """When the keyring is usable but has no entry for this field, the env var
    is still consulted (SPEC §15.2)."""
    store = _store(tmp_path)
    var = secret_env_var_name("eg-test1", "nessie_client_secret")
    monkeypatch.setenv(var, "env-fallback-secret")

    assert store.get_secret("eg-test1", "nessie_client_secret") == "env-fallback-secret"

    # A keyring entry takes precedence over the env var once one is saved.
    store.save(Profile(name="eg-test1"), secrets={"nessie_client_secret": "keyring-secret"})
    assert store.get_secret("eg-test1", "nessie_client_secret") == "keyring-secret"


def test_chainer_backend_with_no_backends_is_unusable(tmp_path, monkeypatch):
    # `ChainerBackend.backends` auto-discovers every constructible KeyringBackend
    # subclass, which in this test session includes the fakes above -- so force
    # the "nothing to chain to" case directly rather than relying on discovery.
    monkeypatch.setattr(keyring.backends.chainer.ChainerBackend, "backends", [])
    store = _store(tmp_path)
    keyring.set_keyring(keyring.backends.chainer.ChainerBackend())

    with pytest.raises(KeychainError) as excinfo:
        store.save(Profile(name="eg-test1"), secrets={"adls_account_key": "value-a"})
    assert secret_env_var_name("eg-test1", "adls_account_key") in excinfo.value.user_message()


def test_parse_env_file_ignores_comments_and_blank_lines(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n# comment line\n\nADLS_ACCOUNT=acct-a\n\n# trailing comment\n",
        encoding="utf-8",
    )

    result = parse_env_file(env_path)
    assert result.fields["adls_account"] == "acct-a"


# --------------------------------------------------------------------------- profile files

from floe.core.profiles import (  # noqa: E402
    IMPORT_FIELD_KINDS,
    MAX_EXPORT_BYTES,
    ProfileImportError,
    export_profile,
    parse_profile_export,
)


def _full_profile(name: str = "eg-test1") -> Profile:
    return Profile(
        name=name,
        adls_account="acctsynthetic",
        nessie_uri="https://nessie.invalid/api/v2",
        nessie_token_endpoint="https://auth.invalid/token",
        nessie_client_id="client-synthetic",
        shared_containers=["ref-a"],
        shared_namespaces=["ns-a"],
        tenant_container_map={"eg-test1": "ref-a"},
        duckdb_threads=4,
        allow_export=True,
    )


def _file(**profile_overrides) -> dict:
    data = export_profile(_full_profile())
    data["profile"].update(profile_overrides)
    return data


def test_import_field_kinds_cover_profile_fields():
    import dataclasses

    assert set(IMPORT_FIELD_KINDS) == {f.name for f in dataclasses.fields(Profile)}


def test_export_round_trip(tmp_path):
    profile = _full_profile()
    data = export_profile(profile)
    assert data["format"] == "floe-profile" and data["version"] == 1
    assert data["exported_at"].endswith("Z") and data["floe_version"]
    result = parse_profile_export(json.dumps(data))
    assert Profile.from_dict(result.fields) == profile
    assert result.notes == []


def test_export_never_contains_secrets(tmp_path):
    from floe.core import diagnostics

    store = _store(tmp_path)
    profile = _full_profile()
    secrets = {
        "adls_account_key": "synthetic-key-AAAA-123456",
        "adls_client_secret": "synthetic-sp-BBBB-123456",
        "nessie_client_secret": "synthetic-nessie-CCCC-123456",
    }
    store.save(profile, secrets=secrets)
    for value in secrets.values():
        diagnostics.register_secret(value)
    text = json.dumps(export_profile(store.get(profile.name)))
    for name in SECRET_FIELDS:
        assert name not in text
    for value in secrets.values():
        assert value not in text
    assert diagnostics.redact(text) == text  # nothing registered was found


def test_import_ignores_secret_keys_with_warning():
    data = _file(adls_account_key="synthetic-key-AAAA-123456", nessie_client_secret="x" * 12)
    data["adls_client_secret"] = "top-level-secret-123"
    result = parse_profile_export(json.dumps(data))
    assert not set(result.fields) & set(SECRET_FIELDS)
    assert any("never imported from files" in n and "manually" in n for n in result.notes)
    assert "synthetic-key-AAAA-123456" not in json.dumps(result.__dict__)


def test_import_unknown_fields_dropped_with_note():
    result = parse_profile_export(json.dumps(_file(surprise=1)))
    assert "surprise" not in result.fields
    assert any("surprise" in n for n in result.notes)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"format": "other", "version": 1, "profile": {"name": "x"}}),
        json.dumps({"format": "floe-profile", "version": 2, "profile": {"name": "x"}}),
        json.dumps({"format": "floe-profile", "version": True, "profile": {"name": "x"}}),
        json.dumps({"format": "floe-profile", "version": 1, "profile": []}),
        json.dumps({"format": "floe-profile", "version": 1, "profile": {}}),
    ],
)
def test_import_rejects_bad_envelope(text):
    with pytest.raises(ProfileImportError):
        parse_profile_export(text)


def test_import_per_field_type_errors():
    data = _file(
        mode="cloud",
        nessie_head_ttl_seconds="45",
        shared_containers="ref-a",
        allow_export="yes",
        preview_row_limit=0,
        tenant_container_map={"a": 1},
    )
    with pytest.raises(ProfileImportError) as exc:
        parse_profile_export(json.dumps(data))
    joined = " | ".join(exc.value.errors)
    for key in ("mode", "nessie_head_ttl_seconds", "shared_containers", "allow_export",
                "preview_row_limit", "tenant_container_map"):
        assert any(e.startswith(f"{key}:") for e in exc.value.errors), joined
    assert "45" not in joined.replace("nessie_head_ttl_seconds", "")


def test_import_size_cap():
    data = _file(nessie_scope="s")
    data["padding"] = "x" * MAX_EXPORT_BYTES
    with pytest.raises(ProfileImportError, match="too large"):
        parse_profile_export(json.dumps(data))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, ["adls_account_key", "nessie_client_secret"]),
        ({"adls_auth": "service_principal"}, ["adls_client_secret", "nessie_client_secret"]),
        ({"nessie_auth": "none"}, ["adls_account_key"]),
        ({"adls_auth": "service_principal", "nessie_auth": "none"}, ["adls_client_secret"]),
        ({"mode": "local", "local_fixture_dir": "/x"}, []),
    ],
)
def test_import_missing_secrets_per_auth_mode(overrides, expected):
    assert parse_profile_export(json.dumps(_file(**overrides))).missing_secrets == expected
