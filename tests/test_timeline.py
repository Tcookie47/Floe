"""Timeline core (SPEC §15.7): Nessie commit log, Iceberg snapshot history, freshness.

Synthetic data only: the fake Nessie + pyiceberg fixtures (tests/fakes)."""

from __future__ import annotations

import gzip
import json
import shutil
import time
from datetime import UTC, datetime

import pytest

from floe.core.context import CancelToken, FloeSession
from floe.core.errors import (
    CorruptPointer,
    QueryCancelled,
    TableNotFound,
    TenantScopeError,
    TimelineUnavailable,
)
from floe.core.nessie import NessieClient, NessieHTTPError, TableKey, parse_log_entry, parse_time
from floe.core.profiles import Profile
from floe.core.timeline import (
    METADATA_CACHE,
    SCOPE_ERROR_MESSAGE,
    branch_history,
    decode_metadata,
    freshness,
    parse_table_history,
    table_snapshots,
)
from tests.fakes.fake_nessie import DEFAULT_CLIENT_SECRET
from tests.fakes.iceberg_fixtures import (
    HISTORY_KEY,
    HISTORY_REF,
    REGISTRY_KEY,
    SHARED_CONTAINERS,
    SHARED_NAMESPACES,
    build_history_table,
    seed_fake_nessie,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    METADATA_CACHE.clear()
    yield
    METADATA_CACHE.clear()


@pytest.fixture
def needs_iceberg(iceberg_unavailable_reason):
    if iceberg_unavailable_reason:
        pytest.skip(iceberg_unavailable_reason)


@pytest.fixture(scope="session")
def history_table(iceberg_fixtures):
    return build_history_table(iceberg_fixtures.iceberg_root)


@pytest.fixture
def fake(fake_nessie, iceberg_fixtures, history_table):
    seed_fake_nessie(fake_nessie, iceberg_fixtures)
    fake_nessie.add_table(
        HISTORY_REF,
        HISTORY_KEY,
        metadata_location=history_table.location,
        snapshot_id=history_table.snapshot_ids[-1],
        commit_message="pipeline: load events\nrun=synthetic-42",
    )
    return fake_nessie


def make_session(fake, root, storage_extensions_available=False, **overrides) -> FloeSession:
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
        {"nessie_client_secret": DEFAULT_CLIENT_SECRET},
        client,
        local_storage_root=root,
        skip_storage_setup=True,
    )


@pytest.fixture
def session(fake, iceberg_fixtures, needs_iceberg):
    return make_session(fake, iceberg_fixtures.iceberg_root)


def history_client(fake) -> NessieClient:
    return NessieClient(fake.profile(), DEFAULT_CLIENT_SECRET, timeout=5)


# --------------------------------------------------------------------------- commit log


def test_parse_time_handles_nanoseconds_and_offsets():
    assert parse_time("2026-01-02T03:04:05.123456789Z") == datetime(
        2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC
    )
    assert parse_time("2026-01-02T05:04:05+02:00") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert parse_time("not a time") is None and parse_time(None) is None


def test_parse_log_entry_touched_keys_skip_namespaces():
    entry = {
        "commitMeta": {
            "hash": "h1",
            "message": "m",
            "committer": "c",
            "authors": ["a1", "a2"],
            "commitTime": "2026-01-01T00:00:00Z",
        },
        "operations": [
            {"type": "PUT", "key": {"elements": ["silver", "ns", "t"]},
             "content": {"type": "ICEBERG_TABLE"}},
            {"type": "PUT", "key": {"elements": ["silver", "ns"]},
             "content": {"type": "NAMESPACE"}},
            {"type": "DELETE", "key": {"elements": ["gold", "old"]}},
            {"type": "UNCHANGED", "key": {"elements": ["gold", "same"]}},
        ],
    }
    info = parse_log_entry(entry, with_operations=True)
    assert info.hash == "h1" and info.author == "a1, a2" and info.committer == "c"
    assert info.touched == (TableKey(("silver", "ns", "t")), TableKey(("gold", "old")))
    assert parse_log_entry(entry, with_operations=False).touched is None
    v1 = parse_log_entry({"commitMeta": {"hash": "x", "author": "solo"}}, True)
    assert v1.author == "solo" and v1.touched is None and v1.message == ""


def test_history_newest_first_with_operations_and_paging(fake_nessie):
    fake_nessie.set_branch("eg-test1")
    base = time.time() - 3600
    for i in range(5):
        fake_nessie.commit(
            "eg-test1", f"commit {i}\nline two", author=f"bot{i}",
            puts=((f"silver.ns.t{i}"),), commit_time=base + i,
        )
    client = history_client(fake_nessie)
    first = client.history("eg-test1", max_records=2)
    assert [c.message.splitlines()[0] for c in first.entries] == ["commit 4", "commit 3"]
    assert first.entries[0].touched == (TableKey(("silver", "ns", "t4")),)
    assert first.entries[0].author == "bot4" and first.operations_available is True
    assert abs(first.entries[0].time.timestamp() - (base + 4)) < 0.01
    assert first.next_token
    second = client.history("eg-test1", page_token=first.next_token, max_records=2)
    third = client.history("eg-test1", page_token=second.next_token, max_records=2)
    assert [c.message[:8] for c in second.entries + third.entries] == [
        "commit 2", "commit 1", "commit 0"
    ]
    assert third.next_token is None
    queries = [r.query for r in fake_nessie.requests("/api/v2/trees/eg-test1/history")]
    assert all("fetch=ALL" in q and "max-records=2" in q for q in queries)
    assert "page-token=" in queries[1]


def test_history_fetch_all_rejected_degrades_and_is_remembered(fake_nessie):
    fake_nessie.set_branch("main")
    for i in range(3):
        fake_nessie.commit("main", f"c{i}", puts=(f"gold.t{i}",))
    fake_nessie.state.reject_fetch_all = True
    client = history_client(fake_nessie)
    page = client.history("main", max_records=2)
    assert [c.message for c in page.entries] == ["c2", "c1"]
    assert all(c.touched is None for c in page.entries)
    assert page.operations_available is False
    assert client.history_operations_supported is False
    client.history("main", page_token=page.next_token, max_records=2)
    queries = [r.query for r in fake_nessie.requests("/api/v2/trees/main/history")]
    # 1 rejected fetch=ALL, then message-only for that page and the next (no retry).
    assert ["fetch=ALL" in q for q in queries] == [True, False, False]
    assert [r.status for r in fake_nessie.requests("/api/v2/trees/main/history")] == [
        400, 200, 200
    ]


def test_history_operations_omitted_degrades(fake_nessie):
    fake_nessie.set_branch("main")
    fake_nessie.commit("main", "only message", puts=("gold.t",))
    fake_nessie.state.omit_operations = True
    client = history_client(fake_nessie)
    page = client.history("main")
    assert page.entries[0].message == "only message" and page.entries[0].touched is None
    assert page.operations_available is False
    client.history("main")
    queries = [r.query for r in fake_nessie.requests("/api/v2/trees/main/history")]
    assert ["fetch=ALL" in q for q in queries] == [True, False]


def test_history_server_error_raises_and_does_not_degrade(fake_nessie):
    fake_nessie.set_branch("main")
    fake_nessie.fail_next(1, 503, "/api/v2/trees/main/history")
    client = history_client(fake_nessie)
    with pytest.raises(NessieHTTPError):
        client.history("main")
    assert client.history_operations_supported is None


def test_history_logs_counts_not_messages(fake_nessie, caplog):
    fake_nessie.set_branch("main")
    fake_nessie.commit("main", "secret-ish synthetic message body", puts=("gold.t",))
    with caplog.at_level("INFO", logger="floe.nessie"):
        history_client(fake_nessie).history("main")
    assert "1 commit(s)" in caplog.text
    assert "synthetic message body" not in caplog.text


# --------------------------------------------------------------------------- snapshots


def test_parse_table_history_and_gzip():
    meta = {
        "current-snapshot-id": 2,
        "snapshots": [
            {"snapshot-id": 2, "parent-snapshot-id": 1, "timestamp-ms": 2000,
             "summary": {"operation": "overwrite", "added-records": "4", "deleted-records": "5",
                         "added-data-files": "1", "deleted-data-files": "2",
                         "total-records": "4"}},
            {"snapshot-id": 1, "timestamp-ms": 1000, "summary": {"operation": "append"}},
            {"no-id": True},
        ],
    }
    history = parse_table_history(decode_metadata(gzip.compress(json.dumps(meta).encode())))
    assert [s.snapshot_id for s in history.snapshots] == [1, 2]
    cur = history.current
    assert cur.operation == "overwrite" and cur.added_records == 4 and cur.deleted_records == 5
    assert cur.added_files == 1 and cur.removed_files == 2 and cur.total_records == 4
    assert cur.parent_id == 1 and cur.time == datetime.fromtimestamp(2, tz=UTC)
    assert history.snapshots[0].total_records is None
    assert parse_table_history({"current-snapshot-id": -1}).current is None


def test_table_snapshots_from_real_metadata(session, history_table):
    result = table_snapshots(session, HISTORY_REF, HISTORY_KEY)
    history = result.history
    assert [s.operation for s in history.snapshots] == ["append", "append", "delete", "append"]
    assert [s.total_records for s in history.snapshots] == [3, 5, 0, 4]
    assert [s.added_records for s in history.snapshots] == [3, 2, None, 4]
    assert history.snapshots[2].deleted_records == 5 and history.snapshots[2].removed_files == 2
    assert [s.snapshot_id for s in history.snapshots] == history_table.snapshot_ids
    assert history.current_snapshot_id == history_table.snapshot_ids[-1]
    assert history.snapshots[1].parent_id == history.snapshots[0].snapshot_id
    assert result.info.source_ref == HISTORY_REF and result.info.shared is False


def test_table_snapshots_cached_by_metadata_location(session, monkeypatch):
    reads: list[str] = []
    real = FloeSession._read_metadata_file

    def spy(self, ctx, info, location, token):
        reads.append(location)
        return real(self, ctx, info, location, token)

    monkeypatch.setattr(FloeSession, "_read_metadata_file", spy)
    table_snapshots(session, HISTORY_REF, HISTORY_KEY)
    table_snapshots(session, HISTORY_REF, HISTORY_KEY)
    assert len(reads) == 1 and METADATA_CACHE.hits >= 1


def test_shared_table_resolves_from_main(session, fake, iceberg_fixtures):
    result = table_snapshots(session, "eg-test1", "gold_ref.codes")
    assert result.info.source_ref == "main" and result.info.shared is True
    assert result.history.current.total_records == 4  # main's copy, not the tenant's stale one


def test_scope_error_corrupt_and_not_found(session, fake, iceberg_fixtures):
    # eg-test2 pointing into eg-test1's container → refused before anything is read.
    fake.add_table(
        "eg-test2", "silver.input_layer.stolen",
        metadata_location=iceberg_fixtures.location("eg-test1", "bronze.medical"),
    )
    with pytest.raises(TenantScopeError):
        table_snapshots(session, "eg-test2", "silver.input_layer.stolen")
    fake.add_table("eg-test1", "gold.broken", snapshot_id=-1)
    with pytest.raises(CorruptPointer):
        table_snapshots(session, "eg-test1", "gold.broken")
    with pytest.raises(TableNotFound):
        table_snapshots(session, "eg-test1", "gold.nope")


def test_only_metadata_json_is_read_never_data_files(
    fake_nessie, tmp_path, needs_iceberg, monkeypatch
):
    """Build a table, delete every data and manifest file, and read its history: only
    the metadata.json is touched (and the spy sees nothing else being read)."""
    root = tmp_path / "iceberg"
    table = build_history_table(root, container="eg-test1")
    removed = [p for p in table.table_dir.rglob("*") if p.is_file()
               and not p.name.endswith(".metadata.json")]
    assert any(p.suffix == ".parquet" for p in removed)
    assert any(p.suffix == ".avro" for p in removed)
    for path in removed:
        path.unlink()
    shutil.rmtree(table.table_dir / "data")
    fake_nessie.set_branch("main")
    fake_nessie.add_table("eg-test1", HISTORY_KEY, metadata_location=table.location)
    session = make_session(
        fake_nessie, root, shared_containers=["ref-a"], shared_namespaces=[],
        tenant_registry_table="",
    )
    reads: list[str] = []
    real = FloeSession._read_metadata_file

    def spy(self, ctx, info, location, token):
        reads.append(location)
        return real(self, ctx, info, location, token)

    monkeypatch.setattr(FloeSession, "_read_metadata_file", spy)
    result = table_snapshots(session, "eg-test1", HISTORY_KEY)
    assert [s.total_records for s in result.history.snapshots] == [3, 5, 0, 4]
    assert reads == [table.location] and reads[0].endswith(".metadata.json")
    rows = freshness(session, "eg-test1")
    assert [(r.status, r.total_records) for r in rows] == [("ok", 4)]


def test_non_metadata_location_is_refused(fake_nessie, iceberg_fixtures, needs_iceberg):
    fake_nessie.set_branch("main")
    data_file = next((iceberg_fixtures.iceberg_root / "eg-test1").rglob("*.parquet"))
    fake_nessie.add_table("eg-test1", "gold.weird", metadata_location=data_file.as_uri())
    session = make_session(
        fake_nessie, iceberg_fixtures.iceberg_root, shared_namespaces=[], tenant_registry_table=""
    )
    rows = freshness(session, "eg-test1")
    assert rows[0].status == "error" and "not a metadata.json" in rows[0].error


def test_local_mode_is_unavailable(iceberg_fixtures):
    profile = Profile(name="lp", mode="local", local_fixture_dir=str(iceberg_fixtures.local_root))
    session = FloeSession(profile, {})
    for call in (
        lambda: freshness(session, "eg-test1"),
        lambda: branch_history(session, "eg-test1"),
        lambda: table_snapshots(session, "eg-test1", "bronze.medical"),
    ):
        with pytest.raises(TimelineUnavailable):
            call()


# --------------------------------------------------------------------------- freshness


def test_freshness_rows_shared_and_last_published(session, fake, history_table):
    fake.add_table(
        "eg-test2", "silver.input_layer.stolen",
        metadata_location=history_table.location,  # eg-test1's container: out of scope
    )
    fake.add_table("eg-test1", "gold.broken", snapshot_id=-1)
    rows = {r.key.dotted: r for r in freshness(session, "eg-test1")}
    assert set(rows) == {
        "bronze.medical", "silver.input_layer.medical_claim", "silver.input_layer.no_ds",
        "gold.summary", HISTORY_KEY, "gold.broken", "gold_ref.codes", REGISTRY_KEY,
    }
    events = rows[HISTORY_KEY]
    assert events.status == "ok" and events.operation == "append"
    assert events.total_records == 4 and events.added_records == 4
    assert events.snapshot_count == 4
    assert events.last_write_time is not None and events.last_write_time.tzinfo is not None
    assert events.last_commit_message == "pipeline: load events\nrun=synthetic-42"
    assert events.last_published_time is not None
    codes = rows["gold_ref.codes"]
    assert codes.shared and codes.source_ref == "main" and codes.status == "ok"
    assert codes.total_records == 4
    assert codes.last_commit_message == "Update table gold_ref.codes"  # from main's log
    assert rows["gold.broken"].status == "corrupt" and rows["gold.broken"].error
    assert rows["bronze.medical"].total_records == 5  # the snapshot total, unfiltered

    scoped = {r.key.dotted: r for r in freshness(session, "eg-test2")}
    assert scoped["silver.input_layer.stolen"].status == "scope_error"
    assert scoped["silver.input_layer.stolen"].error == SCOPE_ERROR_MESSAGE
    assert scoped["bronze.medical"].status == "ok"


def test_freshness_without_operations_has_no_published_info(session, fake):
    fake.state.reject_fetch_all = True
    rows = freshness(session, "eg-test1")
    assert rows and all(r.last_published_time is None for r in rows)
    assert all(r.last_commit_message is None for r in rows)
    assert {r.status for r in rows} == {"ok"}


def test_freshness_publish_scan_is_bounded(session, fake):
    fake.state.history_page_size = 1
    for i in range(20):
        fake.commit("eg-test1", f"noise {i}", puts=("gold.other",), move_head=False)
    rows = {r.key.dotted: r for r in freshness(session, "eg-test1", publish_pages=3)}
    assert rows[HISTORY_KEY].last_commit_message is None
    assert len(fake.requests("/api/v2/trees/eg-test1/history")) == 3


def test_freshness_cancel(session):
    token = CancelToken()
    token.cancel()
    with pytest.raises(QueryCancelled):
        freshness(session, "eg-test1", cancel_token=token)
