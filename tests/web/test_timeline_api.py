"""Timeline job kinds (SPEC §15.7): freshness, history, table_history — over the fake
Nessie + pyiceberg fixtures, and refused in local mode."""

from __future__ import annotations

import pytest

from floe.core.context import FloeSession
from floe.core.nessie import NessieClient
from floe.core.timeline import METADATA_CACHE, SCOPE_ERROR_MESSAGE
from tests.fakes.fake_nessie import DEFAULT_CLIENT_SECRET
from tests.fakes.iceberg_fixtures import (
    REGISTRY_KEY,
    SHARED_CONTAINERS,
    SHARED_NAMESPACES,
    build_history_table,
    seed_fake_nessie,
)
from tests.web.conftest import start_job, wait_job


@pytest.fixture(autouse=True)
def _fresh_cache():
    METADATA_CACHE.clear()


@pytest.fixture(scope="module")
def history_table(iceberg_fixtures):
    return build_history_table(iceberg_fixtures.iceberg_root, container="eg-test1",
                               key="silver.input_layer.events_api")


@pytest.fixture
def remote(make_client, store, fake_nessie, fx, history_table, iceberg_unavailable_reason):
    if iceberg_unavailable_reason:
        pytest.skip(iceberg_unavailable_reason)
    seed_fake_nessie(fake_nessie, fx)
    fake_nessie.add_table(
        "eg-test1", history_table.key, metadata_location=history_table.location,
        commit_message="load events\nbatch=synthetic",
    )
    fake_nessie.add_table(
        "eg-test2", "gold.stolen", metadata_location=history_table.location
    )
    profile = fake_nessie.profile(
        name="remote-p",
        nessie_head_ttl_seconds=0,
        shared_containers=list(SHARED_CONTAINERS),
        shared_namespaces=list(SHARED_NAMESPACES),
        tenant_registry_table=REGISTRY_KEY,
    )
    store.save(profile)

    def factory(p):
        return FloeSession(
            p, {}, NessieClient(p, DEFAULT_CLIENT_SECRET, timeout=5),
            local_storage_root=fx.iceberg_root, skip_storage_setup=True,
        )

    client = make_client(session_factory=factory)
    client.fake = fake_nessie  # type: ignore[attr-defined]
    return client


def run(client, **payload):
    payload.setdefault("profile", "remote-p")
    return wait_job(client, start_job(client, **payload))


def assert_no_locations(client, fx):
    text = "\n".join(client.bodies)
    for needle in ("file:", str(fx.iceberg_root), ".metadata.json", "abfss:", "acct", "/data/"):
        assert needle not in text, needle


def test_freshness_job(remote, fx, history_table):
    job = run(remote, kind="freshness", ref="eg-test1")
    assert job["status"] == "done", job
    assert job["channel"] == "freshness" and job["ref"] == "eg-test1"
    rows = {r["key"]: r for r in job["result"]["rows"]}
    events = rows[history_table.key]
    assert events["status"] == "ok" and events["operation"] == "append"
    assert events["total_records"] == 4 and events["added_records"] == 4
    assert events["view_name"] == "silver_input_layer_events_api"
    assert events["last_write_time"].endswith("Z") and isinstance(events["last_write_time_ms"], int)
    assert events["last_commit_message"] == "load events\nbatch=synthetic"
    assert isinstance(events["last_published_time_ms"], int)
    codes = rows["gold_ref.codes"]
    assert codes["shared"] is True and codes["source_ref"] == "main"
    assert codes["layer"] == "gold_ref"
    assert_no_locations(remote, fx)


def test_freshness_scope_error_row_has_no_container_names(remote, fx):
    job = run(remote, kind="freshness", ref="eg-test2")
    stolen = next(r for r in job["result"]["rows"] if r["key"] == "gold.stolen")
    assert stolen["status"] == "scope_error" and stolen["error"] == SCOPE_ERROR_MESSAGE
    assert stolen["last_write_time"] is None
    assert_no_locations(remote, fx)


def test_history_job_paging_and_touched(remote, fx, history_table):
    first = run(remote, kind="history", ref="eg-test1", max_records=2)
    assert first["status"] == "done" and first["channel"] == "timeline"
    result = first["result"]
    assert result["operations_available"] is True and result["next_token"]
    newest = result["entries"][0]
    assert newest["message"] == "load events\nbatch=synthetic"
    assert newest["touched"] == [{
        "key": history_table.key, "elements": history_table.key.split("."),
        "view_name": "silver_input_layer_events_api",
    }]
    assert newest["short_hash"] == newest["hash"][:10] and newest["time"].endswith("Z")
    more = run(remote, kind="history", ref="eg-test1", max_records=2,
               page_token=result["next_token"])
    assert more["status"] == "done"
    seen = {e["hash"] for e in result["entries"]}
    assert not seen & {e["hash"] for e in more["result"]["entries"]}
    assert_no_locations(remote, fx)


def test_history_job_degrades_when_fetch_all_rejected(remote):
    remote.fake.state.reject_fetch_all = True
    job = run(remote, kind="history", ref="eg-test1")
    assert job["status"] == "done"
    assert job["result"]["operations_available"] is False
    assert all(e["touched"] is None for e in job["result"]["entries"])
    assert job["result"]["entries"][0]["message"]


def test_table_history_job(remote, fx, history_table):
    job = run(remote, kind="table_history", ref="eg-test1", key=history_table.key.split("."))
    assert job["status"] == "done", job
    assert job["channel"] == "table_history" and job["key"] == history_table.key
    result = job["result"]
    ops = [s["operation"] for s in result["snapshots"]]
    assert ops == ["append", "delete", "append", "append"]  # newest first
    assert [s["total_records"] for s in result["snapshots"]] == [4, 0, 5, 3]
    assert result["snapshots"][0]["current"] is True
    assert result["current_snapshot_id"] == str(history_table.snapshot_ids[-1])
    assert all(isinstance(s["snapshot_id"], str) for s in result["snapshots"])
    assert isinstance(result["snapshots"][0]["time_ms"], int)
    # Dotted keys work too; a shared table resolves from main.
    shared = run(remote, kind="table_history", ref="eg-test1", key="gold_ref.codes")
    assert shared["result"]["source_ref"] == "main" and shared["result"]["shared"] is True
    assert_no_locations(remote, fx)


def test_table_history_errors_are_safe(remote, fx):
    scoped = run(remote, kind="table_history", ref="eg-test2", key=["gold", "stolen"])
    assert scoped["status"] == "error"
    assert scoped["error"]["type"] == "TenantScopeError"
    assert scoped["error"]["table_status"] == "scope_error"
    assert scoped["error"]["message"] == SCOPE_ERROR_MESSAGE
    missing = run(remote, kind="table_history", ref="eg-test1", key=["gold", "nope"])
    assert missing["error"]["table_status"] == "not_found"
    assert_no_locations(remote, fx)


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"kind": "table_history", "ref": "eg-test1"}, "key is required"),
        ({"kind": "table_history", "ref": "eg-test1", "key": []}, "key must be"),
        ({"kind": "table_history", "ref": "eg-test1", "key": ["a", 3]}, "key must be"),
        ({"kind": "table_history", "ref": "eg-test1", "key": ["a\nb"]}, "key must be"),
        ({"kind": "history", "ref": "eg-test1", "page_token": 5}, "page_token"),
        ({"kind": "history", "ref": "eg-test1", "max_records": 0}, "max_records"),
        ({"kind": "history", "ref": "eg-test1", "max_records": True}, "max_records"),
        ({"kind": "freshness"}, "ref"),
    ],
)
def test_timeline_job_validation(remote, payload, message):
    r = remote.post("/api/jobs", json={"profile": "remote-p", **payload})
    assert r.status_code == 400, r.text
    assert message in r.json()["error"]["message"]


def test_timeline_jobs_refused_in_local_mode(local_client):
    for payload in (
        {"kind": "freshness"},
        {"kind": "history"},
        {"kind": "table_history", "key": ["bronze", "medical"]},
    ):
        job = wait_job(local_client, start_job(local_client, ref="eg-test1", **payload))
        assert job["status"] == "error"
        assert job["error"]["type"] == "TimelineUnavailable"
        assert "needs a Nessie catalog" in job["error"]["message"]


def test_timeline_tab_pref(local_client):
    r = local_client.put("/api/prefs", json={"last_tab": "timeline"})
    assert r.status_code == 200 and r.json()["last_tab"] == "timeline"
