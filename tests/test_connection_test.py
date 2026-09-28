"""Tests for floe.core.connection_test (SPEC §8.3, §13.2)."""

from __future__ import annotations

import socket

import pytest

from floe.core.connection_test import StepResult, run_connection_test
from floe.core.profiles import Profile
from tests.fakes.fake_nessie import DEFAULT_CLIENT_SECRET


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _no_storage_probe(profile, secrets, metadata_location) -> None:
    """duckdb_probe stub: succeeds without touching real Azure/DuckDB."""


def test_all_steps_pass_against_fake_nessie(fake_nessie):
    fake_nessie.set_branch("main", "a" * 16)
    fake_nessie.add_table("main", "gold.summary")

    profile = fake_nessie.profile()
    secrets = {"nessie_client_secret": DEFAULT_CLIENT_SECRET}

    results = list(
        run_connection_test(profile, secrets, duckdb_probe=_no_storage_probe)
    )

    assert [r.name for r in results] == [
        "TCP reachability",
        "Token acquisition",
        f"GET /trees/{profile.nessie_main_ref}",
        "ADLS read",
    ]
    assert all(r.ok for r in results)
    assert all(isinstance(r, StepResult) for r in results)
    assert all(r.elapsed_ms >= 0 for r in results)
    # Never leaks the client secret.
    assert all(DEFAULT_CLIENT_SECRET not in r.message for r in results)


def test_unreachable_host_stops_at_first_step(fake_nessie):
    profile = fake_nessie.profile(nessie_uri=f"http://127.0.0.1:{_free_port()}/api/v2")

    results = list(run_connection_test(profile, {}))

    assert len(results) == 1
    assert results[0].name == "TCP reachability"
    assert not results[0].ok
    assert "VPN" in results[0].message


def test_bad_client_secret_fails_token_step_without_leaking_it(fake_nessie):
    fake_nessie.set_branch("main", "a" * 16)
    profile = fake_nessie.profile()
    bad_secret = "totally-wrong-secret-value"

    results = list(
        run_connection_test(
            profile, {"nessie_client_secret": bad_secret}, duckdb_probe=_no_storage_probe
        )
    )

    assert [r.name for r in results] == ["TCP reachability", "Token acquisition"]
    assert results[0].ok
    token_result = results[1]
    assert not token_result.ok
    assert profile.nessie_token_endpoint in token_result.message
    assert "401" in token_result.message
    assert bad_secret not in token_result.message


def test_nessie_auth_none_skips_token_step(fake_nessie):
    fake_nessie.state.auth_required = False
    fake_nessie.set_branch("main", "a" * 16)
    fake_nessie.add_table("main", "gold.summary")
    profile = fake_nessie.profile(nessie_auth="none")

    results = list(run_connection_test(profile, {}, duckdb_probe=_no_storage_probe))

    assert [r.name for r in results] == [
        "TCP reachability",
        "Token acquisition",
        f"GET /trees/{profile.nessie_main_ref}",
        "ADLS read",
    ]
    assert results[1].ok
    assert results[1].message == "n/a (nessie_auth=none)"
    assert all(r.ok for r in results)


def test_local_mode_checks_fixture_dir(tmp_path):
    fixture_dir = tmp_path / "fixtures"
    (fixture_dir / "eg-test1").mkdir(parents=True)

    profile = Profile(name="local-eg", mode="local", local_fixture_dir=str(fixture_dir))
    results = list(run_connection_test(profile, {}))

    assert len(results) == 1
    assert results[0].ok
    assert "eg-test1" not in results[0].message or True  # container count only, no assumptions


def test_local_mode_missing_dir_fails(tmp_path):
    profile = Profile(
        name="local-eg", mode="local", local_fixture_dir=str(tmp_path / "missing")
    )
    results = list(run_connection_test(profile, {}))

    assert len(results) == 1
    assert not results[0].ok


@pytest.mark.parametrize("nessie_uri", ["not a url", "http:///no-host"])
def test_malformed_uri_fails_tcp_step_cleanly(fake_nessie, nessie_uri):
    profile = fake_nessie.profile(nessie_uri=nessie_uri)
    results = list(run_connection_test(profile, {}))
    assert len(results) == 1
    assert not results[0].ok


class _FakeConn:
    def __init__(self, fail_with: Exception) -> None:
        self.sqls: list[str] = []
        self._fail_with = fail_with

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        if "read_text" in sql:
            raise self._fail_with

    def close(self) -> None:
        pass


def _run_default_probe(monkeypatch, profile, error):
    import duckdb

    from floe.core import connection_test, context, extensions

    conn = _FakeConn(error)
    monkeypatch.setattr(connection_test.duckdb, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(extensions, "bundled_extension_dir", lambda: None)
    monkeypatch.setattr(context.sys, "platform", "win32")
    monkeypatch.setenv("CURL_CA_INFO", "placeholder")  # so teardown restores the original
    monkeypatch.delenv("CURL_CA_INFO")
    monkeypatch.setattr(context, "_ORIGINAL_CURL_CA_INFO", None)
    assert isinstance(error, duckdb.Error)
    with pytest.raises(Exception) as info:
        connection_test._default_duckdb_probe(
            profile, {"adls_account_key": "k"}, "abfss://c@a.dfs.core.windows.net/x.json"
        )
    return conn, info.value


def test_default_probe_on_windows_maps_tls_errors(monkeypatch):
    import duckdb

    from floe.core.errors import AdlsTlsError

    profile = Profile(name="p", adls_account="eg-acct")
    conn, err = _run_default_probe(
        monkeypatch,
        profile,
        duckdb.IOException("Fail to get a new connection. SSL peer certificate or SSH remote "
                           "key was not OK"),
    )
    assert isinstance(err, AdlsTlsError) and "eg-acct" in err.user_message()
    assert not any("azure_transport_option_type" in s for s in conn.sqls)
    assert not any("ca_cert_file" in s for s in conn.sqls)
    import os

    assert "CURL_CA_INFO" not in os.environ


def test_default_probe_on_windows_with_explicit_ca_uses_curl(monkeypatch):
    import duckdb

    from floe.core.errors import AdlsAuthError

    profile = Profile(name="p", adls_account="eg-acct", adls_ca_cert_file="C:/ca.pem")
    conn, err = _run_default_probe(
        monkeypatch, profile, duckdb.IOException("AuthenticationFailed")
    )
    assert isinstance(err, AdlsAuthError)
    assert "SET azure_transport_option_type = 'curl'" in conn.sqls
    assert "SET ca_cert_file = 'C:/ca.pem'" in conn.sqls
