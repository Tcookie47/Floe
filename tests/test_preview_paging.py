"""Preview paging (SPEC §8.5): page order stability, has-more, totals, counts, versions.

Uses the multi-file / multi-snapshot paging tables (tests/fakes/iceberg_fixtures.py):
four appends of 700 / 650 / 600 / 450 rows with 128-row row groups, so a page crosses
row-group and data-file boundaries. Every 4th row belongs to another tenant.
"""

from __future__ import annotations

import pandas as pd
import pytest

from floe.core.context import CancelToken, FloeSession
from floe.core.errors import QueryCancelled, TableNotFound
from floe.core.nessie import NessieClient
from floe.core.profiles import Profile
from floe.core.timeline import TableHistory, current_row_total, parse_table_history
from tests.fakes.fake_nessie import DEFAULT_CLIENT_SECRET
from tests.fakes.iceberg_fixtures import (
    PAGING_TOTAL,
    REGISTRY_KEY,
    SHARED_CONTAINERS,
    SHARED_NAMESPACES,
    paging_own_ids,
    seed_fake_nessie,
    seed_paging_tables,
)

TENANT_VIEW = "silver_paging_events"
SHARED_VIEW = "gold_ref_paging_codes"
OWN_TOTAL = len(paging_own_ids())
ALL_IDS = list(range(PAGING_TOTAL))


@pytest.fixture
def fake(fake_nessie, iceberg_fixtures, paging_tables, iceberg_unavailable_reason):
    if iceberg_unavailable_reason:
        pytest.skip(iceberg_unavailable_reason)
    seed_fake_nessie(fake_nessie, iceberg_fixtures)
    seed_paging_tables(fake_nessie, paging_tables)
    return fake_nessie


@pytest.fixture
def make_remote(fake, iceberg_fixtures):
    def make(**overrides) -> FloeSession:
        values = dict(
            shared_containers=list(SHARED_CONTAINERS),
            shared_namespaces=list(SHARED_NAMESPACES),
            tenant_registry_table=REGISTRY_KEY,
            nessie_head_ttl_seconds=0,
            duckdb_threads=4,
        )
        values.update(overrides)
        profile = fake.profile(**values)
        return FloeSession(
            profile,
            {},
            NessieClient(profile, DEFAULT_CLIENT_SECRET, timeout=5),
            local_storage_root=iceberg_fixtures.iceberg_root,
            skip_storage_setup=True,
        )

    return make


@pytest.fixture
def remote(make_remote):
    return make_remote()


@pytest.fixture
def local(paging_tables):
    profile = Profile(
        name="local-paging",
        mode="local",
        local_fixture_dir=str(paging_tables.local_root),
        shared_containers=["ref-a"],
        shared_namespaces=list(SHARED_NAMESPACES),
        duckdb_threads=4,
    )
    return FloeSession(profile)


def page_through(session: FloeSession, ref: str, view: str, size: int, **kw) -> pd.DataFrame:
    """Concatenate every page, checking has_more / offsets / the pinned version."""
    frames = []
    offset = 0
    version = None
    while True:
        page = session.preview_page(
            ref, view, offset, page_size=size, expect_version=version, **kw
        )
        assert not page.version_changed
        assert page.offset == offset and page.row_count <= size
        version = version or page.data_version
        assert page.data_version == version
        frames.append(page.df)
        if not page.has_more:
            assert page.total_rows == offset + page.row_count
            assert page.total_source in ("end", "count", "snapshot")
            break
        assert page.row_count == size
        offset += size
    return pd.concat(frames, ignore_index=True)


def full_read(session: FloeSession, ref: str, view: str, order_by_id: bool = False, **kw):
    sql = f'SELECT * FROM "{view}"' + (" ORDER BY id" if order_by_id else "")
    return session.query(ref, sql, **kw).df


def check_pages(session, ref, view, expected_ids, size, **kw) -> None:
    paged = page_through(session, ref, view, size, **kw)
    ids = list(paged["id"])
    assert len(ids) == len(set(ids)), "pages overlap"
    assert sorted(ids) == expected_ids, "pages miss rows"
    # Same rows in the same order as one page holding the whole table ...
    whole = session.preview_page(ref, view, 0, page_size=10_000, **kw)
    assert not whole.has_more
    pd.testing.assert_frame_equal(paged, whole.df)
    # ... (local read_parquet: also the order of a plain unpaged scan) ...
    if session.is_local:
        pd.testing.assert_frame_equal(paged, full_read(session, ref, view, **kw))
    # ... and, sorted, the same as a full ORDER BY read.
    ordered = paged.sort_values("id", ignore_index=True)
    pd.testing.assert_frame_equal(ordered, full_read(session, ref, view, True, **kw))


CASES = [
    ("eg-test1", TENANT_VIEW, True, paging_own_ids()),
    ("eg-test1", TENANT_VIEW, False, ALL_IDS),
    ("eg-test1", SHARED_VIEW, True, ALL_IDS),  # shared table from main on a tenant branch
    ("main", SHARED_VIEW, True, ALL_IDS),
]


# --------------------------------------------------------------------------- order stability


@pytest.mark.parametrize("ref,view,tenant_filter,expected", CASES)
@pytest.mark.parametrize("size", [50, 333, 500])
def test_iceberg_pages_disjoint_complete_and_ordered(
    remote, ref, view, tenant_filter, expected, size
):
    check_pages(remote, ref, view, expected, size, tenant_filter=tenant_filter)


@pytest.mark.parametrize("ref,view,tenant_filter,expected", CASES[:3])
@pytest.mark.parametrize("size", [50, 333, 500])
def test_local_parquet_pages_disjoint_complete_and_ordered(
    local, ref, view, tenant_filter, expected, size
):
    check_pages(local, ref, view, expected, size, tenant_filter=tenant_filter)


def test_page_order_is_repeatable_across_runs_and_connections(make_remote, local):
    remote = make_remote()
    pages = [remote.preview_page("eg-test1", TENANT_VIEW, 700, page_size=300).df
             for _ in range(5)]
    pages.append(make_remote().preview_page("eg-test1", TENANT_VIEW, 700, page_size=300).df)
    for other in pages[1:]:
        pd.testing.assert_frame_equal(pages[0], other)
    # Iceberg pages come from the single-threaded sibling connection; SQL keeps the
    # parallel one. Both keep DuckDB's default preserve_insertion_order.
    ctx = remote.context("eg-test1")
    assert ctx.pager is not None and ctx.pager is not ctx
    assert ctx.pager.execute("SELECT current_setting('threads')") == [(1,)]
    assert ctx.execute("SELECT current_setting('threads')") == [(4,)]
    for c in (ctx, ctx.pager):
        assert c.execute("SELECT current_setting('preserve_insertion_order')") == [(True,)]
    # The pager is as restricted as the main connection.
    assert ctx.pager.allowed_containers == ctx.allowed_containers is not None
    local.preview_page("eg-test1", TENANT_VIEW, 0)
    assert local.context("eg-test1").pager is None  # read_parquet keeps order in parallel


# --------------------------------------------------------------------------- has more


def test_has_more_detection(remote):
    page = remote.preview_page("eg-test1", TENANT_VIEW, 0, page_size=600)
    assert page.has_more and page.row_count == 600 and page.truncated
    # OWN_TOTAL (1800) is a multiple of 600: the last full page has no more rows.
    last = remote.preview_page("eg-test1", TENANT_VIEW, OWN_TOTAL - 600, page_size=600)
    assert not last.has_more and last.row_count == 600
    assert last.total_rows == OWN_TOTAL and last.total_source == "end"
    beyond = remote.preview_page("eg-test1", TENANT_VIEW, OWN_TOTAL, page_size=600)
    assert beyond.row_count == 0 and not beyond.has_more
    small = remote.preview_page("eg-test1", "bronze_medical", 0, page_size=50)
    assert small.row_count == 3 and not small.has_more
    assert small.total_rows == 3 and small.total_source == "end"
    with pytest.raises(ValueError):
        remote.preview_page("eg-test1", TENANT_VIEW, -1)
    with pytest.raises(TableNotFound):
        remote.preview_page("eg-test1", "silver_nope", 0)


def test_page_size_defaults_to_profile_and_old_values_are_clamped(make_remote):
    assert make_remote().preview_page_size == 500
    page = make_remote().preview_page("main", SHARED_VIEW)
    assert page.page_size == 500 and page.row_count == 500 and page.has_more
    old = make_remote(preview_row_limit=1_000_000)  # saved by an older version
    assert old.preview_page_size == 5_000
    page = old.preview_page("main", SHARED_VIEW)
    assert page.page_size == 5_000 and page.row_count == PAGING_TOTAL and not page.has_more
    first = old.preview("main", SHARED_VIEW)  # the first page, as a QueryResult
    assert first.row_count == PAGING_TOTAL and not first.truncated


# --------------------------------------------------------------------------- totals


def test_total_from_snapshot_summary_without_row_filter(remote, fake):
    fake.reset_log()
    for ref, view, tenant_filter in (
        ("main", SHARED_VIEW, True),
        ("eg-test1", SHARED_VIEW, True),  # shared: never filtered
        ("eg-test1", TENANT_VIEW, False),  # filter off
    ):
        page = remote.preview_page(ref, view, 0, tenant_filter=tenant_filter, page_size=100)
        assert page.has_more and not page.filtered
        assert page.total_rows == PAGING_TOTAL and page.total_source == "snapshot"


def test_total_unknown_with_tenant_filter_until_counted(remote, fake):
    page = remote.preview_page("eg-test1", TENANT_VIEW, 0, page_size=100)
    assert page.filtered and page.has_more
    assert page.total_rows is None and page.total_source is None

    counted = remote.count_rows("eg-test1", TENANT_VIEW)
    assert counted.count == OWN_TOTAL and counted.data_version == page.data_version
    again = remote.preview_page("eg-test1", TENANT_VIEW, 100, page_size=100,
                                expect_version=page.data_version)
    assert again.total_rows == OWN_TOTAL and again.total_source == "count"
    # The other filter mode is a different row set: not covered by that count.
    unfiltered = remote.preview_page("eg-test1", TENANT_VIEW, 0, tenant_filter=False,
                                     page_size=100)
    assert unfiltered.total_source == "snapshot"

    # A new data version invalidates the count.
    fake.move_head("eg-test1")
    fresh = remote.preview_page("eg-test1", TENANT_VIEW, 0, page_size=100)
    assert fresh.data_version != page.data_version
    assert fresh.total_rows is None
    recount = remote.count_rows("eg-test1", TENANT_VIEW)
    assert recount.data_version == fresh.data_version and recount.count == OWN_TOTAL


def test_snapshot_total_ignored_when_summary_has_row_deletes():
    def history(summary):
        return parse_table_history({
            "current-snapshot-id": 1,
            "snapshots": [{"snapshot-id": 1, "timestamp-ms": 1, "summary": summary}],
        })

    assert current_row_total(history({"total-records": "10"})) == 10
    assert current_row_total(history({"total-records": "10", "total-position-deletes": "0",
                                      "total-equality-deletes": "0"})) == 10
    assert current_row_total(history({"total-records": "10",
                                      "total-position-deletes": "2"})) is None
    assert current_row_total(history({"operation": "append"})) is None
    assert current_row_total(TableHistory(None, ())) is None


def test_local_mode_has_no_snapshot_total_but_counts(local):
    page = local.preview_page("eg-test1", SHARED_VIEW, 0, page_size=100)
    assert page.has_more and page.total_rows is None and page.data_version == "local"
    assert local.count_rows("eg-test1", SHARED_VIEW).count == PAGING_TOTAL
    page = local.preview_page("eg-test1", SHARED_VIEW, 0, page_size=100)
    assert page.total_rows == PAGING_TOTAL and page.total_source == "count"
    filtered = local.preview_page("eg-test1", TENANT_VIEW, 0, page_size=100)
    assert filtered.filtered and filtered.total_rows is None


# --------------------------------------------------------------------------- versions


def test_head_change_returns_flag_instead_of_rows(remote, fake):
    first = remote.preview_page("eg-test1", TENANT_VIEW, 0, page_size=100)
    shared = remote.preview_page("eg-test1", SHARED_VIEW, 0, page_size=100)
    fake.move_head("eg-test1")
    moved = remote.preview_page("eg-test1", TENANT_VIEW, 100, page_size=100,
                                expect_version=first.data_version)
    assert moved.version_changed and moved.row_count == 0
    assert moved.data_version != first.data_version
    # A shared table follows main's head only.
    same = remote.preview_page("eg-test1", SHARED_VIEW, 100, page_size=100,
                               expect_version=shared.data_version)
    assert not same.version_changed and same.row_count == 100
    fake.move_head("main")
    moved = remote.preview_page("eg-test1", SHARED_VIEW, 100, page_size=100,
                                expect_version=shared.data_version)
    assert moved.version_changed
    # Page 1 with the new version works again.
    restart = remote.preview_page("eg-test1", TENANT_VIEW, 0, page_size=100)
    assert not restart.version_changed and restart.row_count == 100


def test_page_cancellation(remote):
    token = CancelToken()
    token.cancel()
    with pytest.raises(QueryCancelled):
        remote.preview_page("eg-test1", TENANT_VIEW, 0, cancel_token=token)
    token = CancelToken()
    token.cancel()
    with pytest.raises(QueryCancelled):
        remote.count_rows("eg-test1", TENANT_VIEW, cancel_token=token)
    assert not remote.is_running()


def test_page_after_catalog_rebuild_is_retried(make_remote, fake, iceberg_fixtures):
    """Reload-and-retry on a missing metadata.json still applies to page loads."""
    session = make_remote(nessie_head_ttl_seconds=3600)
    page = session.preview_page("eg-test1", TENANT_VIEW, 0, page_size=100)
    ctx = session.context("eg-test1")
    calls = []
    real = session._execute_df

    def flaky(ctx_, sql, token):
        calls.append(ctx_)
        if len(calls) == 1:
            from floe.core.context import _MetadataGone

            raise _MetadataGone("metadata.json gone")
        return real(ctx_, sql, token)

    session._execute_df = flaky
    again = session.preview_page("eg-test1", TENANT_VIEW, 100, page_size=100,
                                 expect_version=page.data_version)
    assert again.reloaded and not again.version_changed and again.row_count == 100
    assert calls[0] is ctx.pager and calls[1] is not ctx.pager  # rebuilt connection
