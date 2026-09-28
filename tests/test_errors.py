"""Tests for floe.core.errors (SPEC §7)."""

from floe.core.errors import (
    AdlsAuthError,
    CorruptPointer,
    FloeError,
    MissingDataSourceColumn,
    NessieAuthError,
    NessieUnreachable,
    QueryCancelled,
    TableNotFound,
    TenantScopeError,
)


def test_all_are_floe_errors():
    for exc in (
        NessieUnreachable("https://nessie.example.test", "timeout"),
        NessieAuthError("https://nessie.example.test/token", 401),
        TableNotFound("ref-a", "silver.ns.table"),
        CorruptPointer("ref-a", "gold.table"),
        TenantScopeError("ref-a", "silver.ns.table", "container-b", ["container-a"]),
        MissingDataSourceColumn("ref-a", "silver.ns.table"),
        AdlsAuthError("acct-a", "account_key"),
        QueryCancelled(),
    ):
        assert isinstance(exc, FloeError)
        assert isinstance(exc.user_message(), str)
        assert exc.user_message()


def test_nessie_unreachable_message():
    exc = NessieUnreachable("https://nessie.example.test", "connection refused")
    assert exc.uri == "https://nessie.example.test"
    assert exc.reason == "connection refused"
    assert "VPN" in exc.user_message()


def test_nessie_auth_error_fields_and_message():
    exc = NessieAuthError("https://nessie.example.test/token", 401)
    assert exc.endpoint == "https://nessie.example.test/token"
    assert exc.status == 401
    msg = exc.user_message()
    assert "https://nessie.example.test/token" in msg
    assert "401" in msg


def test_table_not_found_fields():
    exc = TableNotFound("ref-a", "silver.input_layer.table_x")
    assert exc.ref == "ref-a"
    assert exc.key == "silver.input_layer.table_x"
    assert "silver.input_layer.table_x" in exc.user_message()


def test_corrupt_pointer_fields():
    exc = CorruptPointer("ref-a", "gold.table_x")
    assert exc.ref == "ref-a"
    assert exc.key == "gold.table_x"
    assert "gold.table_x" in exc.user_message()


def test_tenant_scope_error_fields():
    exc = TenantScopeError("ref-a", "silver.ns.table_x", "container-b", ["container-a"])
    assert exc.ref == "ref-a"
    assert exc.key == "silver.ns.table_x"
    assert exc.location_container == "container-b"
    assert exc.expected_containers == ["container-a"]
    msg = exc.user_message()
    assert "container-b" in msg
    assert "container-a" in msg


def test_missing_data_source_column_message_offers_disable():
    exc = MissingDataSourceColumn("ref-a", "silver.ns.table_x")
    assert "disable" in exc.user_message().lower()


def test_adls_auth_error_fields():
    exc = AdlsAuthError("acct-a", "service_principal")
    assert exc.account == "acct-a"
    assert exc.auth_mode == "service_principal"
    msg = exc.user_message()
    assert "acct-a" in msg
    assert "service_principal" in msg


def test_query_cancelled_is_quiet():
    exc = QueryCancelled()
    assert exc.user_message() == "Query cancelled."


def test_errors_never_carry_secrets_by_construction():
    # NessieAuthError and AdlsAuthError only accept structured, non-secret fields.
    exc = NessieAuthError("https://nessie.example.test/token", 401)
    assert not hasattr(exc, "secret")
    assert not hasattr(exc, "token")
    assert not hasattr(exc, "password")
