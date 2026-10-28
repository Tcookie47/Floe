"""Web API for paged Preview (SPEC §8.5): one job per page, count jobs, version pinning."""

from __future__ import annotations

import pytest

from floe.core.context import FloeSession
from floe.core.nessie import NessieClient
from floe.web.jobs import result_rows
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
from tests.web.conftest import start_job, wait_job

TENANT_VIEW = "silver_paging_events"
SHARED_VIEW = "gold_ref_paging_codes"
OWN_TOTAL = len(paging_own_ids())


@pytest.fixture
def remote_client(make_client, store, fake_nessie, fx, paging_tables, iceberg_unavailable_reason):
    if iceberg_unavailable_reason:
        pytest.skip(iceberg_unavailable_reason)
    seed_fake_nessie(fake_nessie, fx)
    seed_paging_tables(fake_nessie, paging_tables)
    store.save(fake_nessie.profile(
        name="remote-p",
        nessie_head_ttl_seconds=0,
        shared_containers=list(SHARED_CONTAINERS),
        shared_namespaces=list(SHARED_NAMESPACES),
        tenant_registry_table=REGISTRY_KEY,
        allow_export=True,
    ))

    def factory(p):
        return FloeSession(
            p, {}, NessieClient(p, DEFAULT_CLIENT_SECRET, timeout=5),
            local_storage_root=fx.iceberg_root, skip_storage_setup=True,
        )

    client = make_client(session_factory=factory)
    client.fake = fake_nessie
    return client


def page(client, ref, view, offset=0, **payload) -> dict:
    job = wait_job(
        client,
        start_job(client, profile="remote-p", kind="preview", ref=ref, view_name=view,
                  offset=offset, **payload),
        limit=5000,
    )
    assert job["status"] == "done", job
    return job


def test_page_jobs_read_one_page_each_from_the_server(remote_client):
    first = page(remote_client, "eg-test1", TENANT_VIEW)
    r = first["result"]
    assert first["row_limit"] == 500 and first["page_offset"] == 0
    assert r["page_offset"] == 0 and r["page_size"] == 500 and len(r["rows"]) == 500
    assert r["has_more"] is True and r["filtered"] is True
    assert r["table_total"] is None and r["total_source"] is None  # tenant filter on
    assert r["version_changed"] is False and r["data_version"]
    # Each page job holds at most one page of rows (memory budget).
    job = remote_client.app.state.floe.jobs.get(first["id"])
    assert result_rows(job.result) == 500

    second = page(remote_client, "eg-test1", TENANT_VIEW, 500, expect_version=r["data_version"])
    r2 = second["result"]
    assert r2["page_offset"] == 500 and len(r2["rows"]) == 500
    ids1 = [row[0] for row in r["rows"]]
    ids2 = [row[0] for row in r2["rows"]]
    assert not set(ids1) & set(ids2)
    last = page(remote_client, "eg-test1", TENANT_VIEW, 1500, expect_version=r["data_version"])
    assert len(last["result"]["rows"]) == OWN_TOTAL - 1500
    assert last["result"]["has_more"] is False and last["result"]["table_total"] == OWN_TOTAL


def test_totals_count_job_and_version_flag(remote_client):
    shared = page(remote_client, "main", SHARED_VIEW)["result"]
    assert shared["table_total"] == PAGING_TOTAL and shared["total_source"] == "snapshot"
    off = page(remote_client, "eg-test1", TENANT_VIEW, tenant_filter=False)["result"]
    assert off["table_total"] == PAGING_TOTAL and off["filtered"] is False

    first = page(remote_client, "eg-test1", TENANT_VIEW)["result"]
    count_id = start_job(remote_client, profile="remote-p", kind="preview_count",
                         ref="eg-test1", view_name=TENANT_VIEW)
    counted = wait_job(remote_client, count_id)
    assert counted["status"] == "done" and counted["channel"] == "preview_count"
    assert counted["result"]["count"] == OWN_TOTAL
    assert counted["result"]["data_version"] == first["data_version"]
    again = page(remote_client, "eg-test1", TENANT_VIEW, 500,
                 expect_version=first["data_version"])["result"]
    assert again["table_total"] == OWN_TOTAL and again["total_source"] == "count"

    remote_client.fake.move_head("eg-test1")
    moved = page(remote_client, "eg-test1", TENANT_VIEW, 1000,
                 expect_version=first["data_version"])["result"]
    assert moved["version_changed"] is True and moved["rows"] == []
    assert moved["data_version"] != first["data_version"]
    fresh = page(remote_client, "eg-test1", TENANT_VIEW)["result"]
    assert fresh["table_total"] is None  # the count was for the old data version


def test_page_job_validation_and_csv_of_one_page(remote_client):
    def post(**payload):
        return remote_client.post("/api/jobs", json={
            "profile": "remote-p", "kind": "preview", "ref": "main",
            "view_name": SHARED_VIEW, **payload,
        })

    for bad in (-1, "5", True, 1.5):
        assert post(offset=bad).status_code == 400
    assert post(expect_version=5).status_code == 400
    assert post(expect_version="").status_code == 400
    job = page(remote_client, "main", SHARED_VIEW, 500)
    r = remote_client.get(f"/api/jobs/{job['id']}/csv")
    assert r.status_code == 200
    assert r.headers["x-row-count"] == "500"
    assert 'filename="gold_ref_paging_codes_rows_501-1000.csv"' in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert lines[0] == "id,label,data_source" and len(lines) == 501


def test_local_preview_page_and_count(local_client):
    view = "silver_input_layer_medical_claim"  # 4 own rows on eg-test1, page size 2
    first = wait_job(local_client, start_job(local_client, kind="preview", ref="eg-test1",
                                             view_name=view))["result"]
    assert first["has_more"] is True and first["table_total"] is None
    assert first["data_version"] == "local"
    second = wait_job(local_client, start_job(local_client, kind="preview", ref="eg-test1",
                                              view_name=view, offset=2,
                                              expect_version="local"))["result"]
    assert second["has_more"] is False and second["table_total"] == 4
    assert [r[0] for r in first["rows"]] + [r[0] for r in second["rows"]] == [1, 2, 3, 4]
    count = wait_job(local_client, start_job(local_client, kind="preview_count",
                                             ref="eg-test1", view_name=view))
    assert count["result"]["count"] == 4
