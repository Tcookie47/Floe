"""Tests for floe.core.context against fake Nessie + pyiceberg fixtures (SPEC §13.2)."""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path

import duckdb
import pytest

from floe.core import context as context_mod
from floe.core import diagnostics
from floe.core.context import (
    BranchInfo,
    CancelToken,
    ConnectionCache,
    ConnectionKey,
    Context,
    FloeSession,
    build_remote_setup,
    check_read_only,
    is_missing_metadata_error,
    is_storage_auth_error,
    quote_identifier,
    quote_literal,
    resolve_ca_bundle,
)
from floe.core.errors import (
    AdlsAuthError,
    AdlsTlsError,
    CorruptPointer,
    DisallowedFunction,
    MissingDataSourceColumn,
    QueryCancelled,
    QueryError,
    ReadOnlyViolation,
    TableNotFound,
    TenantScopeError,
    ViewNameCollision,
)
from floe.core.nessie import NessieClient, TableKey, local_path_of
from floe.core.profiles import Profile
from tests.fakes.fake_nessie import DEFAULT_CLIENT_SECRET
from tests.fakes.iceberg_fixtures import (
    REGISTRY_KEY,
    SHARED_CONTAINERS,
    SHARED_NAMESPACES,
    fixture_location,
    seed_fake_nessie,
)

ACCOUNT_KEY = "synthetic-account-key-VALUE-0123456789=="
SLOW_SQL = (
    "SELECT count(*) FROM range(1000000000) a, range(1000000) b "
    "WHERE a.range + b.range = -1"
)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def needs_iceberg(iceberg_unavailable_reason):
    if iceberg_unavailable_reason:
        pytest.skip(iceberg_unavailable_reason)


@pytest.fixture
def fx(iceberg_fixtures):
    return iceberg_fixtures


@pytest.fixture
def fake(fake_nessie, fx, needs_iceberg):
    seed_fake_nessie(fake_nessie, fx)
    return fake_nessie


@pytest.fixture
def make_session(fake, fx, storage_extensions_available):
    def make(
        *, cache: ConnectionCache | None = None, clock=time.monotonic, **overrides
    ) -> FloeSession:
        values = dict(
            shared_containers=list(SHARED_CONTAINERS),
            shared_namespaces=list(SHARED_NAMESPACES),
            tenant_registry_table=REGISTRY_KEY,
            nessie_head_ttl_seconds=0,
        )
        values.update(overrides)
        profile = fake.profile(**values)
        client = NessieClient(profile, DEFAULT_CLIENT_SECRET, timeout=5)
        return FloeSession(
            profile,
            {"adls_account_key": ACCOUNT_KEY, "nessie_client_secret": DEFAULT_CLIENT_SECRET},
            client,
            cache=cache,
            local_storage_root=fx.iceberg_root,
            skip_storage_setup=not storage_extensions_available,
            clock=clock,
        )

    return make


@pytest.fixture
def session(make_session):
    return make_session()


def count(session: FloeSession, ref: str, sql: str, **kw) -> int:
    return int(session.query(ref, sql, **kw).df.iloc[0, 0])


def contents_requests(fake, ref: str, key: str) -> int:
    return len(fake.requests(f"/api/v2/trees/{ref}/contents/{key}"))


def run_in_thread(fn):
    box: dict = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, box


def wait_until(pred, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


# --------------------------------------------------------------------------- listing


def test_list_tables_on_tenant_branch_hides_stale_shared_copies(session):
    tables = {t.view_name: t for t in session.list_tables("eg-test1")}
    assert set(tables) == {
        "bronze_medical",
        "silver_input_layer_medical_claim",
        "silver_input_layer_no_ds",
        "gold_summary",
        "gold_ref_codes",
        "registry_tenants",
    }
    assert tables["gold_ref_codes"].shared and tables["gold_ref_codes"].source_ref == "main"
    assert not tables["bronze_medical"].shared
    assert tables["bronze_medical"].source_ref == "eg-test1"
    assert all(t.status == "unknown" for t in tables.values())


def test_list_tables_on_main(session):
    names = {t.view_name for t in session.list_tables("main")}
    assert names == {"gold_ref_codes", "registry_tenants"}


# --------------------------------------------------------------------------- data_source filter


def test_filter_applied_when_on_and_omitted_when_off(session):
    df = session.query("eg-test1", "SELECT * FROM bronze_medical").df
    assert len(df) == 3 and set(df["data_source"]) == {"eg-test1"}
    df_off = session.query("eg-test1", "SELECT * FROM bronze_medical", tenant_filter=False).df
    assert len(df_off) == 5 and set(df_off["data_source"]) == {"eg-test1", "eg-test2"}
    # and back on again
    assert count(session, "eg-test1", "SELECT count(*) FROM bronze_medical") == 3
    status = {t.view_name: t.status for t in session.list_tables("eg-test1")}
    assert status["bronze_medical"] == "registered"


def test_filter_uses_data_source_override(make_session):
    session = make_session(tenant_data_source_map={"eg-test1": "eg-test2"})
    df = session.query("eg-test1", "SELECT data_source FROM bronze_medical").df
    assert len(df) == 2 and set(df["data_source"]) == {"eg-test2"}


def test_filter_never_on_main_or_shared_tables(session):
    # Shared table has no data_source column, yet the tenant filter being on is fine.
    assert count(session, "main", "SELECT count(*) FROM gold_ref_codes") == 4
    assert count(session, "eg-test1", "SELECT count(*) FROM gold_ref_codes") == 4


def test_shared_tables_resolved_from_main_on_tenant_branch(session, fake):
    fake.reset_log()
    # eg-test1 has a stale 1-row copy of gold_ref.codes; the main copy has 4 rows.
    assert count(session, "eg-test1", "SELECT count(*) FROM gold_ref_codes") == 4
    assert contents_requests(fake, "main", "gold_ref.codes") == 1
    assert contents_requests(fake, "eg-test1", "gold_ref.codes") == 0


def test_missing_data_source_column(session):
    with pytest.raises(MissingDataSourceColumn):
        session.register("eg-test1", "silver.input_layer.no_ds")
    status = {t.view_name: t for t in session.list_tables("eg-test1")}
    assert status["silver_input_layer_no_ds"].status == "missing_data_source"
    assert "data_source" in status["silver_input_layer_no_ds"].error
    # Works with the filter off...
    assert count(
        session, "eg-test1", "SELECT count(*) FROM silver_input_layer_no_ds", tenant_filter=False
    ) == 2
    # ...and the unfiltered view does not leak once the filter is back on.
    with pytest.raises(MissingDataSourceColumn):
        session.query("eg-test1", "SELECT * FROM silver_input_layer_no_ds")
    ctx = session.context("eg-test1")
    assert "silver_input_layer_no_ds" not in ctx.registered_views()
    with pytest.raises(duckdb.CatalogException):
        ctx.execute("SELECT * FROM silver_input_layer_no_ds")
    # Other tables on the same connection still work.
    assert count(session, "eg-test1", "SELECT count(*) FROM gold_summary") == 2


# --------------------------------------------------------------------------- pointer errors


def test_table_not_found(session, fake):
    session.list_tables("eg-test1")
    fake.remove_table("eg-test1", "gold.summary")
    with pytest.raises(TableNotFound):
        session.register("eg-test1", "gold.summary")
    status = {t.view_name: t.status for t in session.list_tables("eg-test1")}
    assert status["gold_summary"] == "not_found"
    with pytest.raises(TableNotFound):
        session.schema("eg-test1", "no_such_view")


def test_corrupt_pointer(session, fake, fx):
    fake.add_table(
        "eg-test1", "gold.broken", fx.location("eg-test1", "gold.summary"), snapshot_id=-1
    )
    with pytest.raises(CorruptPointer):
        session.query("eg-test1", "SELECT * FROM gold_broken")
    status = {t.view_name: t.status for t in session.list_tables("eg-test1")}
    assert status["gold_broken"] == "corrupt"
    assert count(session, "eg-test1", "SELECT count(*) FROM bronze_medical") == 3


def test_tenant_scope_error_cross_tenant_and_main(session, fake, fx):
    fake.add_table("eg-test1", "gold.foreign", fx.location("eg-test2", "gold.summary"))
    fake.add_table("main", "gold_ref.leak", fx.location("eg-test1", "gold.summary"))
    with pytest.raises(TenantScopeError) as info:
        session.register("eg-test1", "gold.foreign")
    assert info.value.location_container == "eg-test2"
    assert info.value.expected_containers == ["eg-test1"]
    with pytest.raises(TenantScopeError):
        session.register("main", "gold_ref.leak")
    # Shared lookup from a tenant branch is also checked against shared_containers.
    with pytest.raises(TenantScopeError):
        session.register("eg-test2", "gold_ref.leak")
    # The connection is not poisoned.
    assert count(session, "eg-test1", "SELECT count(*) FROM gold_summary") == 2


def test_registry_container_mapping(make_session):
    # eg-test3's tables live in container eg-c3: only the registry (or an override) allows it.
    session = make_session()
    assert count(session, "eg-test3", "SELECT count(*) FROM gold_summary") == 3
    no_registry = make_session(tenant_registry_table=None)
    with pytest.raises(TenantScopeError):
        no_registry.register("eg-test3", "gold.summary")
    override = make_session(tenant_registry_table=None, tenant_container_map={"eg-test3": "eg-c3"})
    assert count(override, "eg-test3", "SELECT count(*) FROM gold_summary") == 3


# --------------------------------------------------------------------------- branches


def test_branches_with_registry_active_flags(session):
    branches = {b.name: b.active for b in session.branches()}
    assert branches == {"main": None, "eg-test1": True, "eg-test2": False, "eg-test3": True}


def test_branches_without_or_with_unreadable_registry(make_session):
    assert all(b.active is None for b in make_session(tenant_registry_table=None).branches())
    broken = make_session(tenant_registry_table="registry.missing")
    assert all(b.active is None for b in broken.branches())
    assert BranchInfo("eg-test1", None) in broken.branches()


# --------------------------------------------------------------------------- connections


def test_head_change_builds_new_connection_and_clears_pointers(session, fake):
    fake.reset_log()
    assert count(session, "eg-test1", "SELECT count(*) FROM bronze_medical") == 3
    ctx1 = session.context("eg-test1")
    assert count(session, "eg-test1", "SELECT count(*) FROM bronze_medical") == 3
    assert session.context("eg-test1") is ctx1
    assert contents_requests(fake, "eg-test1", "bronze.medical") == 1  # pointer cached

    fake.move_head("eg-test1")
    ctx2 = session.context("eg-test1")
    assert ctx2 is not ctx1 and ctx2.key.ref_head != ctx1.key.ref_head
    assert count(session, "eg-test1", "SELECT count(*) FROM bronze_medical") == 3
    assert contents_requests(fake, "eg-test1", "bronze.medical") == 2  # pointer re-resolved
    # The old connection was not closed.
    assert ctx1.execute("SELECT 1") == [(1,)]


def test_main_head_change_also_changes_key(session, fake):
    ctx1 = session.context("eg-test1")
    fake.move_head("main")
    assert session.context("eg-test1") is not ctx1


def test_connection_cache_lru_unit():
    cache = ConnectionCache(2)
    ctxs = []
    for i in range(3):
        key = ConnectionKey("p", "remote", f"r{i}", "h" * 16, "m" * 16, "")
        ctx = Context(key, duckdb.connect())
        ctxs.append(ctx)
        evicted = cache.put(key, ctx)
        if i == 2:
            assert evicted == [ctxs[0]]
    assert len(cache) == 2 and ctxs[0].key not in cache
    assert cache.get(ctxs[1].key) is ctxs[1]
    ctx3 = Context(ConnectionKey("p", "remote", "r3", "x", "y", ""), duckdb.connect())
    assert cache.put(ctx3.key, ctx3) == [ctxs[2]]  # r1 was used more recently than r2
    for ctx in ctxs:
        assert ctx.execute("SELECT 42") == [(42,)]  # evicted, never closed
    assert "hhhhhhhh," in ctxs[0].key.short() and "h" * 9 not in ctxs[0].key.short()


def test_lru_eviction_does_not_close_connection_mid_query(make_session):
    session = make_session(cache=ConnectionCache(1))
    session.list_tables("eg-test1")
    slow = "SELECT count(*) FROM range(20000) a, range(10000) b WHERE a.range + b.range >= 0"
    token = CancelToken()
    thread, box = run_in_thread(lambda: session.query("eg-test1", slow, cancel_token=token))
    wait_until(lambda: token.executing)
    ctx1 = next(c for c in session.cache.contexts() if c.ref == "eg-test1")
    session.context("eg-test2")  # evicts eg-test1's connection while it's querying
    assert ctx1.key not in session.cache
    thread.join(30)
    assert "error" not in box, box.get("error")
    assert int(box["result"].df.iloc[0, 0]) == 20000 * 10000


# --------------------------------------------------------------------------- rebuild / retry


def _local_path(location: str) -> Path:
    path = local_path_of(location)
    assert path is not None, location
    return Path(path)


def _copy_metadata(src_location: str, name: str) -> str:
    src = _local_path(src_location)
    dst = src.with_name(name)
    shutil.copyfile(src, dst)
    return fixture_location(dst)


def test_missing_metadata_triggers_exactly_one_rebuild_and_retry(make_session, fake, fx):
    session = make_session(nessie_head_ttl_seconds=3600)
    base = fx.location("eg-test1", "gold.summary")
    loc_a = _copy_metadata(base, "00050-moving-a.metadata.json")
    fake.add_table("eg-test1", "gold.moving", loc_a)
    first = session.query("eg-test1", "SELECT count(*) FROM gold_moving")
    assert int(first.df.iloc[0, 0]) == 2 and not first.reloaded

    builds = []
    real_new = session._new_connection
    session._new_connection = lambda: builds.append(1) or real_new()

    # Catalog rebuilt: head moves to a new valid pointer, the old metadata.json is gone.
    loc_b = _copy_metadata(base, "00051-moving-b.metadata.json")
    fake.add_table("eg-test1", "gold.moving", loc_b)
    fake.move_head("eg-test1")
    _local_path(loc_a).unlink()

    result = session.query("eg-test1", "SELECT count(*) FROM gold_moving")
    assert int(result.df.iloc[0, 0]) == 2
    assert result.reloaded is True
    assert len(builds) == 1

    # If the retry fails again, the error surfaces after exactly one more rebuild.
    session2 = make_session(nessie_head_ttl_seconds=3600)
    assert count(session2, "eg-test1", "SELECT count(*) FROM gold_moving") == 2
    builds2 = []
    real_new2 = session2._new_connection
    session2._new_connection = lambda: builds2.append(1) or real_new2()
    _local_path(loc_b).unlink()
    with pytest.raises(QueryError) as info:
        session2.query("eg-test1", "SELECT count(*) FROM gold_moving")
    assert "metadata.json" in str(info.value)
    assert len(builds2) == 1


def test_missing_metadata_error_detection():
    assert is_missing_metadata_error(
        duckdb.IOException('Cannot open file "/x/metadata/00001-a.metadata.json": No such file')
    )
    assert is_missing_metadata_error(
        duckdb.HTTPException("HTTP GET error on '.../v3.metadata.json' (HTTP 404)")
    )
    assert not is_missing_metadata_error(duckdb.IOException('Cannot open file "/x/data.parquet"'))
    assert not is_missing_metadata_error(duckdb.BinderException("metadata.json no such file"))


# --------------------------------------------------------------------------- interrupt


def test_interrupt_cancels_long_query_from_another_thread(session):
    session.list_tables("eg-test1")
    token = CancelToken()
    thread, box = run_in_thread(lambda: session.query("eg-test1", SLOW_SQL, cancel_token=token))
    wait_until(lambda: token.executing)
    session.interrupt("eg-test1")  # once: the token keeps interrupting until the statement ends
    thread.join(20)
    assert not thread.is_alive()
    assert isinstance(box.get("error"), QueryCancelled)
    assert not session.is_running()
    assert count(session, "eg-test1", "SELECT count(*) FROM bronze_medical") == 3


def test_interrupt_all(session):
    session.list_tables("eg-test2")
    token = CancelToken()
    thread, box = run_in_thread(lambda: session.query("eg-test2", SLOW_SQL, cancel_token=token))
    wait_until(lambda: token.executing)
    session.interrupt_all()
    thread.join(20)
    assert isinstance(box.get("error"), QueryCancelled)


def test_cancel_token_before_start_and_while_waiting_for_the_lock(session):
    # Cancelled before it starts: nothing runs, not even view registration.
    token = CancelToken()
    token.cancel()
    with pytest.raises(QueryCancelled):
        session.query("eg-test1", "SELECT count(*) FROM bronze_medical", cancel_token=token)
    assert session.context("eg-test1").registered_views() == {}

    # A query waiting for the connection lock is cancelled without touching the one
    # holding it, and without running any of its own statements.
    ctx = session.context("eg-test1")
    waiting = CancelToken()
    with ctx.lock:
        thread, box = run_in_thread(
            lambda: session.query("eg-test1", "SELECT * FROM bronze_medical", cancel_token=waiting)
        )
        wait_until(lambda: session.is_running("eg-test1"))
        waiting.cancel()
        assert not waiting.executing
    thread.join(10)
    assert isinstance(box.get("error"), QueryCancelled)
    assert "bronze_medical" not in ctx.registered_views()


def test_cancel_targets_only_its_own_query(session):
    session.list_tables("eg-test1")
    slow_token, fast_token = CancelToken(), CancelToken()
    slow_thread, slow_box = run_in_thread(
        lambda: session.query("eg-test1", SLOW_SQL, cancel_token=slow_token)
    )
    wait_until(lambda: slow_token.executing)
    fast_thread, fast_box = run_in_thread(
        lambda: session.query(
            "eg-test1", "SELECT count(*) FROM bronze_medical", cancel_token=fast_token
        )
    )
    slow_token.cancel()  # the fast query is queued on the same connection lock
    slow_thread.join(20)
    fast_thread.join(20)
    assert isinstance(slow_box.get("error"), QueryCancelled)
    assert "error" not in fast_box, fast_box.get("error")
    assert int(fast_box["result"].df.iloc[0, 0]) == 3


def test_cancel_during_registration_statement(session, monkeypatch):
    session.list_tables("eg-test1")
    token = CancelToken()
    ctx = session.context("eg-test1")
    real_exec = session._exec

    def exec_and_cancel(ctx_, token_, fn):
        token_.cancel()  # cancelled before the first registration statement starts
        return real_exec(ctx_, token_, fn)

    monkeypatch.setattr(session, "_exec", exec_and_cancel)
    with pytest.raises(QueryCancelled):
        session.query("eg-test1", "SELECT * FROM bronze_medical", cancel_token=token)
    monkeypatch.undo()
    assert "bronze_medical" not in ctx.registered_views()
    status = {t.view_name: t.status for t in session.list_tables("eg-test1")}
    assert status["bronze_medical"] == "unknown"
    assert count(session, "eg-test1", "SELECT count(*) FROM bronze_medical") == 3


# --------------------------------------------------------------------------- filter-mode race


def test_filter_mode_switch_cannot_swap_view_mid_query(session):
    """A filter-off query can't replace the view between another query's resolve and run."""
    session.list_tables("eg-test1")
    real_execute_df = session._execute_df
    in_filtered = threading.Event()
    release = threading.Event()

    def execute_df(ctx, sql, token):
        if threading.current_thread().name == "filtered":
            in_filtered.set()
            release.wait(10)
        return real_execute_df(ctx, sql, token)

    session._execute_df = execute_df
    results: dict = {}

    def filtered():
        results["on"] = session.query("eg-test1", "SELECT * FROM bronze_medical").df

    def unfiltered():
        results["off"] = session.query(
            "eg-test1", "SELECT * FROM bronze_medical", tenant_filter=False
        ).df

    t_on = threading.Thread(target=filtered, name="filtered", daemon=True)
    t_on.start()
    assert in_filtered.wait(10)
    t_off = threading.Thread(target=unfiltered, name="unfiltered", daemon=True)
    t_off.start()
    wait_until(lambda: session.is_running("eg-test1") and len(session._active) == 2)
    time.sleep(0.2)  # give the filter-off query every chance to swap the view
    release.set()
    t_on.join(20)
    t_off.join(20)
    assert set(results["on"]["data_source"]) == {"eg-test1"} and len(results["on"]) == 3
    assert len(results["off"]) == 5


# --------------------------------------------------------------------------- SQL handling


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE t AS SELECT 1",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "COPY (SELECT 1) TO 'out.csv'",
        "ATTACH 'x.db'",
        "INSTALL spatial",
        "LOAD spatial",
        "SET threads = 1",
        "PRAGMA threads = 1",
        "PRAGMA version",
        "EXPORT DATABASE 'out'",
        "CALL pragma_version()",
        "DROP VIEW gold_summary",
        "CREATE OR REPLACE VIEW v AS SELECT 1",
        "EXPLAIN ANALYZE SELECT 1",
        "SELECT 1; SELECT 2",
        "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
    ],
)
def test_read_only_enforcement_rejects(sql):
    with pytest.raises(QueryError):
        check_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_parquet('x/*.parquet')",
        "SELECT * FROM iceberg_scan('abfss://eg-test2@acct.dfs.core.windows.net/t/m.json')",
        "SELECT * FROM read_csv_auto('/etc/passwd')",
        "SELECT * FROM read_json('x.json')",
        "SELECT * FROM read_text('/etc/hostname')",
        "SELECT * FROM read_blob('/etc/hostname')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT * FROM sniff_csv('/etc/passwd')",
        "SELECT * FROM parquet_metadata('x.parquet')",
        "SELECT * FROM main.read_parquet('x.parquet')",
        "SELECT * FROM query('SELECT 1')",
        "SELECT * FROM query_table('t')",
        "SELECT * FROM duckdb_secrets()",
        "FROM 'x.parquet'",
        'SELECT * FROM "s3://bucket/x.parquet"',
        "DESCRIBE 'x.parquet'",
        "SUMMARIZE 'x.csv'",
        "WITH a AS (SELECT * FROM read_parquet('x')) SELECT * FROM a",
        "SELECT (SELECT count(*) FROM glob('*'))",
        "SELECT * FROM bronze_medical WHERE id IN (SELECT id FROM read_parquet('x'))",
        "SELECT getenv('HOME')",
        "SELECT json_serialize_plan('SELECT 1')",
    ],
)
def test_disallowed_functions_and_file_references(sql):
    with pytest.raises(DisallowedFunction) as info:
        check_read_only(sql)
    assert "not allowed" in info.value.user_message()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM range(3) r JOIN generate_series(1, 2) g ON true",
        "SELECT unnest([1, 2])",
        "SELECT * FROM unnest([1, 2])",
        "SELECT * FROM main.gold_summary",
        "DESCRIBE gold_summary",
    ],
)
def test_allowed_table_functions(sql):
    assert check_read_only(sql)


def test_read_only_violation_type():
    with pytest.raises(ReadOnlyViolation) as info:
        check_read_only("insert into t values (1)")
    assert "read-only" in info.value.user_message()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "  -- comment\nselect 1;",
        "/* c */ WITH a AS (SELECT 1) SELECT * FROM a",
        "FROM range(3)",
        "VALUES (1), (2)",
        "(SELECT 1) UNION ALL (SELECT 2)",
        "DESCRIBE SELECT 1",
        "SHOW TABLES",
        "SUMMARIZE SELECT 1 AS a",
    ],
)
def test_read_only_enforcement_accepts(sql):
    assert check_read_only(sql)


def test_resolve_sql_references(session):
    sql = (
        "select * from GOLD_SUMMARY g join bronze_medical b using (id) "
        "-- not: xgold_ref_codes, gold_ref_codes_x, silver_input_layer_no_ds2"
    )
    registered = session.resolve_sql_references("eg-test1", sql)
    assert sorted(registered) == ["bronze_medical", "gold_summary"]
    assert session.resolve_sql_references("eg-test1", sql) == []
    ctx = session.context("eg-test1")
    assert set(ctx.registered_views()) == {"bronze_medical", "gold_summary"}
    result = session.query("eg-test1", "SELECT count(*) FROM gold_summary g JOIN bronze_medical b "
                           "USING (id)")
    assert int(result.df.iloc[0, 0]) == 2


def test_query_limit_and_truncation(session):
    r = session.query("eg-test1", "SELECT * FROM bronze_medical ORDER BY id", limit=2)
    assert r.truncated and r.row_count == 2 and list(r.df["id"]) == [1, 2]
    r = session.query("eg-test1", "SELECT * FROM bronze_medical;", limit=3)
    assert not r.truncated and r.row_count == 3
    r = session.query("eg-test1", "SELECT * FROM bronze_medical", limit=None)
    assert not r.truncated and r.row_count == 3 and r.elapsed >= 0
    assert session.query("eg-test1", "DESCRIBE bronze_medical", limit=100).row_count == 4


def test_schema(session):
    cols = session.schema("eg-test1", "silver_input_layer_medical_claim")
    assert [c.name for c in cols] == ["id", "amount", "code", "data_source"]
    assert cols[0].type == "BIGINT" and isinstance(cols[0].nullable, bool)
    with pytest.raises(MissingDataSourceColumn):
        session.schema("eg-test1", "silver_input_layer_no_ds")
    cols = session.schema("eg-test1", "silver_input_layer_no_ds", tenant_filter=False)
    assert [c.name for c in cols] == ["id", "note"]


def test_query_errors_are_redacted(session):
    with pytest.raises(QueryError) as info:
        session.query("eg-test1", f"SELECT CAST('{ACCOUNT_KEY}' AS INTEGER)")
    assert ACCOUNT_KEY not in str(info.value)
    assert ACCOUNT_KEY not in info.value.user_message()


def test_sql_quoting():
    assert quote_literal("o'delta") == "'o''delta'"
    assert quote_identifier('a"b') == '"a""b"'
    con = duckdb.connect()
    assert con.execute(f"SELECT {quote_literal(chr(39) * 3)}").fetchone()[0] == "'''"


# --------------------------------------------------------------------------- auth errors


def test_storage_auth_errors_map_to_adls_auth_error(session):
    exc = duckdb.IOException("AuthenticationFailed: Server failed to authenticate the request")
    assert is_storage_auth_error(exc)
    mapped = session._map_duckdb_error(exc)
    assert isinstance(mapped, AdlsAuthError)
    assert mapped.account == "acct" and mapped.auth_mode == "account_key"
    assert not is_storage_auth_error(duckdb.IOException("Cannot open file x.parquet"))


# --------------------------------------------------------------------------- remote setup SQL


def _profile(**kw) -> Profile:
    values = dict(name="p", adls_account="acct", duckdb_memory_limit="4GB", duckdb_threads=4)
    values.update(kw)
    return Profile(**values)


def test_remote_setup_sql_account_key(monkeypatch):
    # macOS/Linux behaviour (curl + CA bundle); Windows is covered separately.
    monkeypatch.setattr(context_mod.sys, "platform", "linux")
    stmts = build_remote_setup(_profile(), {"adls_account_key": ACCOUNT_KEY}, "/ca/bundle.pem")
    sqls = [s.sql for s in stmts]
    assert sqls[:6] == [
        "INSTALL azure", "LOAD azure", "INSTALL iceberg", "LOAD iceberg",
        "INSTALL httpfs", "LOAD httpfs",
    ]
    assert "SET memory_limit = '4GB'" in sqls
    assert "SET threads = 4" in sqls
    assert "SET azure_transport_option_type = 'curl'" in sqls
    assert "SET ca_cert_file = '/ca/bundle.pem'" in sqls
    secret = stmts[-1]
    assert secret.sql == (
        "CREATE OR REPLACE SECRET adls (TYPE azure, PROVIDER config, CONNECTION_STRING "
        f"'DefaultEndpointsProtocol=https;AccountName=acct;AccountKey={ACCOUNT_KEY};"
        "EndpointSuffix=core.windows.net')"
    )
    assert ACCOUNT_KEY not in secret.loggable and "AccountKey=***" in secret.loggable
    for s in stmts:
        assert ACCOUNT_KEY not in s.loggable
    assert {s.extension for s in stmts[:6]} == {"azure", "iceberg", "httpfs"}


def test_remote_setup_sql_service_principal_and_quoting():
    client_secret = "sp-secret-with-'quote'-0123"
    stmts = build_remote_setup(
        _profile(adls_auth="service_principal", adls_tenant_id="tid-0", adls_client_id="cid-0"),
        {"adls_client_secret": client_secret},
        None,
    )
    secret = stmts[-1]
    assert secret.sql == (
        "CREATE OR REPLACE SECRET adls (TYPE azure, PROVIDER service_principal, "
        "TENANT_ID 'tid-0', CLIENT_ID 'cid-0', CLIENT_SECRET 'sp-secret-with-''quote''-0123', "
        "ACCOUNT_NAME 'acct')"
    )
    assert "sp-secret" not in secret.loggable and "CLIENT_SECRET '***'" in secret.loggable
    assert not any("ca_cert_file" in s.sql for s in stmts)
    duckdb.extract_statements(secret.sql)  # parses


def test_remote_setup_sql_without_secret_or_storage():
    stmts = build_remote_setup(_profile(duckdb_memory_limit=None, duckdb_threads=None), {}, None)
    assert not any("SECRET" in s.sql for s in stmts)
    minimal = build_remote_setup(_profile(), {"adls_account_key": ACCOUNT_KEY}, "/x", storage=False)
    assert [s.sql for s in minimal] == [
        "INSTALL iceberg", "LOAD iceberg", "SET memory_limit = '4GB'", "SET threads = 4",
    ]


def test_resolve_ca_bundle(monkeypatch):
    # macOS/Linux resolution order; Windows is covered separately.
    monkeypatch.setattr(context_mod.sys, "platform", "linux")
    assert resolve_ca_bundle(_profile(adls_ca_cert_file="/my/ca.pem")) == "/my/ca.pem"
    monkeypatch.setattr(context_mod.os.path, "exists", lambda p: p == "/etc/ssl/cert.pem")
    assert resolve_ca_bundle(_profile()) == "/etc/ssl/cert.pem"
    monkeypatch.setattr(context_mod.os.path, "exists", lambda p: False)
    result = resolve_ca_bundle(_profile())
    try:
        import certifi
    except ImportError:
        assert result is None
    else:
        assert result == certifi.where()


_TLS_MESSAGE = (
    "IO Error: AzureStorageFileSystem could not open file: "
    "'abfss://ctr@acct.dfs.core.windows.net/t/metadata/v1.metadata.json', unknown error "
    "occurred, this could mean the credentials used were wrong. Original error message: "
    "'Fail to get a new connection for: https://acct.blob.core.windows.net. SSL peer "
    "certificate or SSH remote key was not OK'"
)


def test_tls_errors_map_to_adls_tls_error_not_auth(session):
    exc = duckdb.IOException(_TLS_MESSAGE)
    assert context_mod.is_storage_tls_error(exc)
    mapped = session._map_duckdb_error(exc)
    assert isinstance(mapped, AdlsTlsError)
    assert mapped.account == "acct"
    text = mapped.user_message()
    assert "acct" in text and "CA cert file" in text and "credentials" not in text
    for msg in (
        "SSL certificate problem: unable to get local issuer certificate",
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
    ):
        assert context_mod.is_storage_tls_error(duckdb.IOException(msg))
    # Real auth failures keep their own error.
    auth = duckdb.IOException("AuthenticationFailed: Server failed to authenticate the request")
    assert not context_mod.is_storage_tls_error(auth)
    assert isinstance(session._map_duckdb_error(auth), AdlsAuthError)


def test_certifi_is_importable():
    import certifi

    assert os.path.exists(certifi.where())


def _storage_sql(profile: Profile) -> list[str]:
    bundle = resolve_ca_bundle(profile)
    return [s.sql for s in build_remote_setup(profile, {"adls_account_key": ACCOUNT_KEY}, bundle)]


def test_remote_setup_sql_on_windows_keeps_default_transport(monkeypatch):
    """Windows: curl has no trust store there, WinHTTP uses the Windows cert store."""
    monkeypatch.setattr(context_mod.sys, "platform", "win32")
    monkeypatch.setattr(context_mod.os.path, "exists", lambda p: True)
    assert resolve_ca_bundle(_profile()) is None
    sqls = _storage_sql(_profile())
    assert not any("azure_transport_option_type" in s for s in sqls)
    assert not any("ca_cert_file" in s for s in sqls)
    assert any("CREATE OR REPLACE SECRET adls" in s for s in sqls)


def test_remote_setup_sql_on_windows_with_explicit_ca_uses_curl(monkeypatch):
    monkeypatch.setattr(context_mod.sys, "platform", "win32")
    sqls = _storage_sql(_profile(adls_ca_cert_file="C:/certs/corp-root.pem"))
    assert "SET azure_transport_option_type = 'curl'" in sqls
    assert "SET ca_cert_file = 'C:/certs/corp-root.pem'" in sqls


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_remote_setup_sql_on_posix_uses_curl_and_a_bundle(monkeypatch, platform):
    monkeypatch.setattr(context_mod.sys, "platform", platform)
    monkeypatch.setattr(context_mod.os.path, "exists", lambda p: p == "/etc/ssl/cert.pem")
    sqls = _storage_sql(_profile())
    assert "SET azure_transport_option_type = 'curl'" in sqls
    assert "SET ca_cert_file = '/etc/ssl/cert.pem'" in sqls
    import certifi

    monkeypatch.setattr(context_mod.os.path, "exists", lambda p: False)
    sqls = _storage_sql(_profile())
    assert "SET azure_transport_option_type = 'curl'" in sqls
    assert f"SET ca_cert_file = {quote_literal(certifi.where())}" in sqls
    sqls = _storage_sql(_profile(adls_ca_cert_file="/my/ca.pem"))
    assert "SET ca_cert_file = '/my/ca.pem'" in sqls


def test_windows_leaves_curl_ca_info_alone(monkeypatch):
    monkeypatch.setattr(context_mod.sys, "platform", "win32")
    monkeypatch.delenv("CURL_CA_INFO", raising=False)
    monkeypatch.setattr(context_mod, "_ORIGINAL_CURL_CA_INFO", None)
    context_mod.apply_curl_ca_bundle(resolve_ca_bundle(_profile()))
    assert "CURL_CA_INFO" not in os.environ
    context_mod.apply_curl_ca_bundle(resolve_ca_bundle(_profile(adls_ca_cert_file="C:/ca.pem")))
    assert os.environ["CURL_CA_INFO"] == "C:/ca.pem"
    context_mod.apply_curl_ca_bundle(None)
    assert "CURL_CA_INFO" not in os.environ


def test_connection_setup_and_logs_never_contain_secrets(make_session, caplog):
    caplog.set_level(logging.DEBUG, logger="floe")
    session = make_session()
    assert count(session, "eg-test1", "SELECT count(*) FROM bronze_medical") == 3
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert ACCOUNT_KEY not in text
    assert "Built connection" in text and "Registered bronze.medical" in text
    assert "Query on eg-test1" in text
    assert ACCOUNT_KEY not in diagnostics.redact(f"x {ACCOUNT_KEY} y")


# --------------------------------------------------------------------------- local mode


@pytest.fixture
def local_session(fx):
    profile = Profile(
        name="local-p",
        mode="local",
        local_fixture_dir=str(fx.local_root),
        shared_containers=list(SHARED_CONTAINERS),
        shared_namespaces=list(SHARED_NAMESPACES),
        tenant_registry_table=REGISTRY_KEY,
        preview_row_limit=2,
    )
    return FloeSession(profile)


def test_local_mode_end_to_end(local_session):
    s = local_session
    branches = {b.name: b.active for b in s.branches()}
    assert branches == {"eg-c3": None, "eg-test1": True, "eg-test2": False}
    tables = {t.view_name: t for t in s.list_tables("eg-test1")}
    assert set(tables) == {
        "bronze_medical",
        "silver_input_layer_medical_claim",
        "silver_input_layer_no_ds",
        "gold_summary",
        "gold_ref_codes",
        "registry_tenants",
    }
    assert tables["gold_ref_codes"].shared and tables["gold_ref_codes"].container == "ref-a"
    # Filter on / off
    assert count(s, "eg-test1", "SELECT count(*) FROM bronze_medical") == 3
    assert count(s, "eg-test1", "SELECT count(*) FROM bronze_medical", tenant_filter=False) == 5
    # Shared table from ref-a, not the stale tenant copy
    assert count(s, "eg-test1", "SELECT count(*) FROM gold_ref_codes") == 4
    with pytest.raises(MissingDataSourceColumn):
        s.query("eg-test1", "SELECT * FROM silver_input_layer_no_ds")
    preview = s.preview("eg-test2", "silver_input_layer_medical_claim")
    assert preview.truncated and preview.row_count == 2
    assert set(preview.df["data_source"]) == {"eg-test2"}
    assert [c.name for c in s.schema("eg-test1", "gold_summary")] == [
        "id", "amount", "code", "data_source",
    ]
    joined = s.query(
        "eg-test1",
        "SELECT c.label FROM bronze_medical m JOIN gold_ref_codes c USING (code) ORDER BY 1",
    )
    assert list(joined.df["label"]) == ["alpha", "beta", "gamma"]
    with pytest.raises(ReadOnlyViolation):
        s.query("eg-test1", "CREATE TABLE x AS SELECT 1")
    with pytest.raises(TableNotFound):
        s.register("eg-test1", "gold.nope")
    assert s.context("eg-test1").key.mode == "local"


# --------------------------------------------------------------------------- tenant file access


def test_user_sql_cannot_read_another_tenants_files(session, fx):
    other = fx.location("eg-test2", "bronze.medical")
    with pytest.raises(DisallowedFunction):
        session.query("eg-test1", f"SELECT * FROM iceberg_scan({quote_literal(other)})")
    # Defence in depth: the connection itself can't read outside its containers...
    ctx = session.context("eg-test1")
    assert ctx.allowed_containers == {"eg-test1", *SHARED_CONTAINERS}
    with pytest.raises(duckdb.PermissionException):
        ctx.execute(f"SELECT count(*) FROM iceberg_scan({quote_literal(other)})")
    with pytest.raises(duckdb.Error):
        ctx.execute("SET enable_external_access = true")  # configuration is locked
    own = fx.location("eg-test1", "bronze.medical")
    assert ctx.execute(f"SELECT count(*) FROM iceberg_scan({quote_literal(own)})") == [(5,)]
    # ...and lazy registration after the restriction still works, shared tables included.
    assert count(session, "eg-test1", "SELECT count(*) FROM bronze_medical") == 3
    assert count(session, "eg-test1", "SELECT count(*) FROM gold_ref_codes") == 4
    main_ctx = session.context("main")
    assert main_ctx.allowed_containers == set(SHARED_CONTAINERS)
    with pytest.raises(duckdb.PermissionException):
        main_ctx.execute(f"SELECT count(*) FROM iceberg_scan({quote_literal(own)})")


def test_local_connection_is_restricted_to_its_containers(local_session, fx):
    s = local_session
    other = (fx.local_root / "eg-test2" / "bronze" / "medical" / "*.parquet").as_posix()
    with pytest.raises(DisallowedFunction):
        s.query("eg-test1", f"SELECT * FROM read_parquet({quote_literal(other)})")
    with pytest.raises(duckdb.PermissionException):
        s.context("eg-test1").execute(f"SELECT * FROM read_parquet({quote_literal(other)})")
    assert count(s, "eg-test1", "SELECT count(*) FROM gold_ref_codes") == 4


def test_restrict_file_access_escape_hatch(fx, caplog):
    other = (fx.local_root / "eg-test2" / "bronze" / "medical" / "*.parquet").as_posix()
    profile = Profile(
        name="local-open",
        mode="local",
        local_fixture_dir=str(fx.local_root),
        shared_containers=list(SHARED_CONTAINERS),
        shared_namespaces=list(SHARED_NAMESPACES),
        restrict_file_access=False,
    )
    s = FloeSession(profile)
    with caplog.at_level(logging.WARNING, logger="floe.context"):
        ctx = s.context("eg-test1")
        s.context("eg-test1")  # cached: no second warning
    warnings = [r for r in caplog.records if "restriction is turned off" in r.getMessage()]
    assert len(warnings) == 1
    assert ctx.allowed_containers is None
    # The connection itself is no longer restricted...
    assert ctx.execute(f"SELECT count(*) FROM read_parquet({quote_literal(other)})") == [(5,)]
    # ...but the SQL table-function blocklist still applies to user SQL.
    with pytest.raises(DisallowedFunction):
        s.query("eg-test1", f"SELECT * FROM read_parquet({quote_literal(other)})")
    assert count(s, "eg-test1", "SELECT count(*) FROM bronze_medical") == 3


def test_restrict_file_access_default_on(local_session):
    assert local_session.profile.restrict_file_access is True
    assert local_session.context("eg-test1").allowed_containers is not None


def test_current_head_and_refresh(session, fake, local_session):
    assert session.current_head("eg-test1") == fake.state.branches["eg-test1"]
    assert session.current_head("no-such-branch") is None
    assert local_session.current_head("eg-test1") is None
    # Refresh keeps the last known head as a fallback when Nessie is down.
    fake.fail_next(5, 503, path_prefix="/api/v2/trees/eg-test1")
    session.refresh()
    assert session.current_head("eg-test1") == fake.state.branches["eg-test1"]
    assert session.catalog_status == "stale"


def test_local_refresh_rescans_tables(tmp_path, fx):
    root = tmp_path / "local"
    shutil.copytree(fx.local_root, root)
    s = FloeSession(Profile(name="local-r", mode="local", local_fixture_dir=str(root)))
    before = {t.view_name for t in s.list_tables("eg-test1")}
    shutil.copytree(root / "eg-test1" / "gold" / "summary", root / "eg-test1" / "gold" / "extra")
    assert {t.view_name for t in s.list_tables("eg-test1")} == before
    s.refresh()
    assert {t.view_name for t in s.list_tables("eg-test1")} == before | {"gold_extra"}


def test_registry_recovery_rebuilds_a_restricted_connection(make_session):
    now = [0.0]
    session = make_session(clock=lambda: now[0])
    real_resolve = session._resolve_source
    calls = {"registry": 0}

    def flaky(info):
        if info.dotted == REGISTRY_KEY:
            calls["registry"] += 1
            if calls["registry"] == 1:
                raise QueryError("synthetic transient failure")
        return real_resolve(info)

    session._resolve_source = flaky
    # Registry down: eg-test3 falls back to container "eg-test3", so its table is refused.
    with pytest.raises(TenantScopeError):
        session.register("eg-test3", "gold.summary")
    assert session.context("eg-test3").allowed_containers == {"eg-test3", *SHARED_CONTAINERS}
    now[0] += context_mod.REGISTRY_RETRY_SECONDS + 1
    result = session.query("eg-test3", "SELECT count(*) FROM gold_summary")
    assert int(result.df.iloc[0, 0]) == 3 and result.reloaded
    assert session.context("eg-test3").allowed_containers == {"eg-c3", *SHARED_CONTAINERS}


# --------------------------------------------------------------------------- view-name collisions


def test_view_name_collisions_are_refused(session, fake, fx):
    loc = fx.location("eg-test1", "gold.summary")
    fake.add_table("eg-test1", "a.b_c", loc)
    fake.add_table("eg-test1", "a_b.c", loc)
    tables = [t for t in session.list_tables("eg-test1") if t.view_name == "a_b_c"]
    assert sorted(t.dotted for t in tables) == ["a.b_c", "a_b.c"]
    for t in tables:
        assert t.status == "error" and "a.b_c" in t.error and "a_b.c" in t.error
    for key in ("a.b_c", "a_b.c"):
        with pytest.raises(ViewNameCollision):
            session.register("eg-test1", key)
    with pytest.raises(ViewNameCollision):
        session.query("eg-test1", "SELECT * FROM a_b_c")
    with pytest.raises(ViewNameCollision):
        session.schema("eg-test1", "a_b_c")
    ctx = session.context("eg-test1")
    assert "a_b_c" not in ctx.registered_views()
    # A key outside the catalog whose view name is taken by a catalog table is refused.
    with pytest.raises(ViewNameCollision):
        session.register("eg-test1", TableKey(("gold_summary",)))
    # Keys added after the listing: the registered key is remembered and compared.
    fake.add_table("eg-test1", "x.y_z", loc)
    fake.add_table("eg-test1", "x_y.z", loc)
    assert session.register("eg-test1", "x.y_z", tenant_filter=False) == "x_y_z"
    with pytest.raises(ViewNameCollision):
        session.register("eg-test1", "x_y.z", tenant_filter=False)
    assert ctx.registered_key("x_y_z") == TableKey(("x", "y_z"))
    # Other tables are unaffected.
    assert count(session, "eg-test1", "SELECT count(*) FROM gold_summary") == 2


# --------------------------------------------------------------------------- local key paths


@pytest.mark.parametrize(
    "elements",
    [
        ("..", "eg-test2", "bronze", "medical"),
        ("bronze", "..", "..", "eg-test2", "bronze", "medical"),
        ("bronze", "", "medical"),
        ("bronze", ".", "medical"),
        ("bronze/medical",),
        ("bronze\\medical",),
        ("bronze", "*"),
        ("ABSOLUTE",),
    ],
)
def test_local_mode_rejects_unsafe_key_elements(local_session, fx, elements):
    if elements == ("ABSOLUTE",):
        elements = (str(fx.local_root / "eg-test2" / "bronze" / "medical"),)
    with pytest.raises(TenantScopeError):
        local_session.register("eg-test1", TableKey(elements), tenant_filter=False)
    assert local_session.context("eg-test1").registered_views() == {}


def test_local_mode_symlink_to_another_container_is_refused(fx, tmp_path):
    root = tmp_path / "local"
    shutil.copytree(fx.local_root, root)
    (root / "eg-test1" / "gold" / "leak").symlink_to(root / "eg-test2" / "gold" / "summary")
    profile = Profile(
        name="local-link",
        mode="local",
        local_fixture_dir=str(root),
        shared_containers=list(SHARED_CONTAINERS),
        shared_namespaces=list(SHARED_NAMESPACES),
    )
    s = FloeSession(profile)
    with pytest.raises(TenantScopeError) as info:
        s.register("eg-test1", "gold.leak", tenant_filter=False)
    assert info.value.location_container == "eg-test2"
    assert count(s, "eg-test1", "SELECT count(*) FROM gold_summary") == 2


# --------------------------------------------------------------------------- CA bundle env


def test_curl_ca_bundle_env_follows_the_profile(make_session, monkeypatch, tmp_path):
    monkeypatch.delenv("CURL_CA_INFO", raising=False)
    monkeypatch.setattr(context_mod, "_ORIGINAL_CURL_CA_INFO", None)
    default = resolve_ca_bundle(_profile())
    if default is None:
        pytest.skip("no default CA bundle on this machine")
    custom = tmp_path / "custom-ca.pem"
    shutil.copyfile(default, custom)

    make_session(adls_ca_cert_file=str(custom)).context("eg-test1")
    assert os.environ["CURL_CA_INFO"] == str(custom)
    # A profile without a custom bundle gets the default back (not the previous custom one).
    make_session().context("eg-test1")
    assert os.environ["CURL_CA_INFO"] == default
    make_session(adls_ca_cert_file=str(custom)).context("eg-test2")
    assert os.environ["CURL_CA_INFO"] == str(custom)
    context_mod.apply_curl_ca_bundle(None)  # restores the value the process started with
    assert "CURL_CA_INFO" not in os.environ


# --------------------------------------------------------------------------- registry retry


def test_transient_registry_failure_is_retried_after_backoff(make_session):
    now = [1000.0]
    session = make_session(clock=lambda: now[0])
    real_resolve = session._resolve_source
    calls = {"registry": 0}

    def flaky(info):
        if info.dotted == REGISTRY_KEY:
            calls["registry"] += 1
            if calls["registry"] == 1:
                raise QueryError("synthetic transient failure")
        return real_resolve(info)

    session._resolve_source = flaky
    assert all(b.active is None for b in session.branches())
    assert all(b.active is None for b in session.branches())  # within the back-off
    assert calls["registry"] == 1
    now[0] += context_mod.REGISTRY_RETRY_SECONDS + 1
    assert {b.name: b.active for b in session.branches()}["eg-test2"] is False
    assert calls["registry"] == 2
    session.branches()  # cached for this main head now
    assert calls["registry"] == 2
