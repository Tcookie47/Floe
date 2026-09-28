"""Catalog endpoints, jobs (preview / schema / query / cancel), export, history, prefs,
diagnostics and about — over a synthetic local-mode profile."""

from __future__ import annotations

import csv
import io
import json
import time

import pytest

from floe import __commit__, __version__
from floe.core import diagnostics
from floe.core.context import FloeSession
from floe.core.nessie import NessieClient
from tests.fakes.fake_nessie import DEFAULT_CLIENT_SECRET
from tests.fakes.iceberg_fixtures import seed_fake_nessie
from tests.web.conftest import (
    SLOW_SQL,
    local_profile,
    start_job,
    wait_job,
    wait_status,
)

# --------------------------------------------------------------------------- catalog


def test_branches_tables_head_refresh(local_client):
    r = local_client.get("/api/profiles/local-p/branches")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {b["name"]: b["active"] for b in body["branches"]} == {
        "eg-c3": None,
        "eg-test1": True,
        "eg-test2": False,
    }
    assert body["is_local"] is True and body["catalog_status"] == "local"

    r = local_client.get("/api/profiles/local-p/tables", params={"ref": "eg-test1"})
    assert r.status_code == 200, r.text
    body = r.json()
    tables = {t["view_name"]: t for t in body["tables"]}
    assert set(tables) == {
        "bronze_medical",
        "silver_input_layer_medical_claim",
        "silver_input_layer_no_ds",
        "gold_summary",
        "gold_ref_codes",
        "registry_tenants",
    }
    codes = tables["gold_ref_codes"]
    assert codes["shared"] is True and codes["key"] == "gold_ref.codes"
    assert codes["elements"] == ["gold_ref", "codes"] and codes["status"] == "unknown"
    assert body["head"] is None and body["tenant_filter_applicable"] is True

    head = local_client.get("/api/profiles/local-p/head", params={"ref": "eg-test1"}).json()
    assert head == {
        "ref": "eg-test1",
        "head": None,
        "catalog_status": "local",
        "is_local": True,
        "tenant_filter_applicable": True,
    }
    r = local_client.post("/api/profiles/local-p/refresh", json={"ref": "eg-test1"})
    assert r.status_code == 200 and len(r.json()["tables"]) == 6
    assert local_client.post("/api/profiles/local-p/refresh").json()["refreshed"] is True
    assert local_client.get("/api/profiles/nope/branches").status_code == 404
    assert local_client.post("/api/profiles/local-p/disconnect").status_code == 200


def test_remote_branches_and_head(make_client, store, fake_nessie, fx):
    seed_fake_nessie(fake_nessie, fx)
    profile = fake_nessie.profile(name="remote-p", nessie_head_ttl_seconds=0)
    store.save(profile)

    def factory(p):
        return FloeSession(p, {}, NessieClient(p, DEFAULT_CLIENT_SECRET, timeout=5))

    client = make_client(session_factory=factory)
    body = client.get("/api/profiles/remote-p/branches").json()
    assert {b["name"] for b in body["branches"]} >= {"main", "eg-test1", "eg-test2"}
    assert body["main_ref"] == "main" and body["is_local"] is False
    head = client.get("/api/profiles/remote-p/head", params={"ref": "eg-test1"}).json()
    assert head["head"] == fake_nessie.state.branches["eg-test1"]
    assert head["catalog_status"] == "ok" and head["tenant_filter_applicable"] is True
    main = client.get("/api/profiles/remote-p/head", params={"ref": "main"}).json()
    assert main["tenant_filter_applicable"] is False


# --------------------------------------------------------------------------- jobs


def test_preview_job_with_paging(local_client):
    view = "silver_input_layer_medical_claim"  # 4 rows for eg-test1, preview limit 2
    job_id = start_job(local_client, kind="preview", ref="eg-test1", view_name=view)
    job = wait_job(local_client, job_id)
    assert job["status"] == "done", job
    result = job["result"]
    assert [c["name"] for c in result["columns"]] == ["id", "amount", "code", "data_source"]
    assert result["total_rows"] == 2 and result["truncated"] is True  # preview_row_limit=2
    assert result["reloaded"] is False
    assert all(row[3] == "eg-test1" for row in result["rows"])
    assert job["row_limit"] == 2 and job["view_name"] == view
    page = wait_job(local_client, job_id, offset=1, limit=1)["result"]
    assert page["offset"] == 1 and page["rows"] == result["rows"][1:2]
    assert local_client.get(f"/api/jobs/{job_id}", params={"limit": 0}).status_code == 422
    assert local_client.get("/api/jobs/nope").status_code == 404


def test_query_job_limit_filter_and_json_safe_types(local_client):
    sql = "SELECT id, code FROM bronze_medical ORDER BY id"
    job = wait_job(local_client, start_job(local_client, kind="query", ref="eg-test1", sql=sql))
    assert job["result"]["total_rows"] == 3 and job["result"]["truncated"] is False
    off = start_job(local_client, kind="query", ref="eg-test1", sql=sql, tenant_filter=False)
    assert wait_job(local_client, off)["result"]["total_rows"] == 5
    capped = start_job(local_client, kind="query", ref="eg-test1", sql=sql, limit=2)
    capped_job = wait_job(local_client, capped)
    assert capped_job["result"]["truncated"] is True and capped_job["result"]["total_rows"] == 2

    typed = (
        "SELECT 1.25::DECIMAL(10,2) AS dec, DATE '2024-01-02' AS d, "
        "TIMESTAMP '2024-01-02 03:04:05' AS ts, NULL::DOUBLE AS n, 'nan'::DOUBLE AS nan_, "
        "'inf'::DOUBLE AS inf_, 9007199254740993::BIGINT AS big, 42 AS small, "
        "'\\xAA'::BLOB AS b, [1, 2] AS l, true AS flag, 'x' AS s, "
        "NULL::TIMESTAMP AS nat, INTERVAL 1 DAY AS iv, {'k': 1} AS st"
    )
    job = wait_job(local_client, start_job(local_client, kind="query", ref="eg-test1", sql=typed))
    assert job["status"] == "done", job
    row = dict(zip([c["name"] for c in job["result"]["columns"]], job["result"]["rows"][0],
                   strict=True))
    assert row["dec"] == 1.25
    assert row["d"].startswith("2024-01-02") and row["ts"] == "2024-01-02T03:04:05"
    assert row["n"] is None and row["nan_"] is None and row["nat"] is None
    assert row["inf_"] == "Infinity"
    assert row["big"] == "9007199254740993" and row["small"] == 42
    assert row["b"] == "\\xAA" and row["l"] == [1, 2] and row["flag"] is True
    assert row["s"] == "x" and isinstance(row["iv"], str) and row["st"] == {"k": 1}


def test_schema_job(local_client):
    job_id = start_job(local_client, kind="schema", ref="eg-test1", view_name="gold_summary")
    job = wait_job(local_client, job_id)
    assert [c["name"] for c in job["result"]["columns"]] == ["id", "amount", "code", "data_source"]
    assert set(job["result"]["columns"][0]) == {"name", "type", "nullable"}


def test_job_validation(local_client):
    def post(**payload):
        payload.setdefault("profile", "local-p")
        return local_client.post("/api/jobs", json=payload)

    assert post(kind="drop").status_code == 400
    assert post(kind="export").status_code == 400
    assert post(kind="query", ref="eg-test1").status_code == 400
    assert post(kind="query", ref="eg-test1", sql="SELECT 1", limit=0).status_code == 400
    too_big = post(kind="query", ref="eg-test1", sql="SELECT 1", limit=1_000_001)
    assert too_big.status_code == 400
    assert post(kind="query", ref="eg-test1", sql="SELECT 1", tenant_filter="no").status_code == 400
    assert post(kind="preview", ref="eg-test1", view_name='x"; --').status_code == 400
    assert post(kind="preview", view_name="gold_summary").status_code == 400
    assert post(kind="query", profile="nope", ref="r", sql="SELECT 1").status_code == 404


def test_cancel_long_query(local_client):
    job_id = start_job(local_client, kind="query", ref="eg-test1", sql=SLOW_SQL)
    assert wait_status(local_client, job_id, "running")["status"] == "running"
    time.sleep(0.2)
    started = time.monotonic()
    r = local_client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["cancel_requested"] is True
    job = wait_job(local_client, job_id, timeout=15)
    assert job["status"] == "cancelled" and "result" not in job
    assert time.monotonic() - started < 10
    # A cancelled query is not recorded in history.
    assert local_client.get("/api/profiles/local-p/history").json()["entries"] == []


def test_new_job_in_channel_cancels_previous(local_client):
    slow = start_job(local_client, kind="query", ref="eg-test1", sql=SLOW_SQL)
    wait_status(local_client, slow, "running")
    quick = start_job(local_client, kind="query", ref="eg-test2", sql="SELECT 1 AS one")
    assert wait_job(local_client, slow, timeout=15)["status"] == "cancelled"
    assert wait_job(local_client, quick)["status"] == "done"
    # Different channels don't interfere; cancel-channels cancels a whole channel.
    slow = start_job(local_client, kind="query", ref="eg-test1", sql=SLOW_SQL, channel="other")
    preview = start_job(local_client, kind="preview", ref="eg-test2", view_name="gold_summary")
    assert wait_job(local_client, preview)["status"] == "done"
    r = local_client.post("/api/jobs/cancel-channels", json={"channels": ["other"]})
    assert slow in r.json()["cancelled"]
    assert wait_job(local_client, slow, timeout=15)["status"] == "cancelled"


def test_too_many_unfinished_jobs_is_429(make_client, store, fx):
    store.save(local_profile(fx))
    client = make_client(max_unfinished_jobs=2)
    slow = [
        start_job(client, kind="query", ref="eg-test1", sql=SLOW_SQL, channel=f"c{i}")
        for i in range(2)
    ]
    r = client.post("/api/jobs", json={"kind": "query", "profile": "local-p", "ref": "eg-test1",
                                       "sql": "SELECT 1", "channel": "c9"})
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "TooManyJobs"
    for job_id in slow:
        client.post(f"/api/jobs/{job_id}/cancel")
    for job_id in slow:
        wait_job(client, job_id, timeout=15)
    assert start_job(client, kind="query", ref="eg-test1", sql="SELECT 1", channel="c9")


def test_preview_row_limit_is_capped(local_client, fx):
    profile = local_profile(fx, preview_row_limit=1_000_001).to_dict()
    r = local_client.put("/api/profiles/local-p", json={"profile": profile})
    assert r.status_code == 400 and "preview_row_limit" in r.json()["error"]["message"]


def test_read_only_violation(local_client):
    job_id = start_job(
        local_client, kind="query", ref="eg-test1", sql="CREATE TABLE x AS SELECT 1"
    )
    job = wait_job(local_client, job_id)
    assert job["status"] == "error"
    assert job["error"]["type"] == "ReadOnlyViolation"
    assert "read-only" in job["error"]["message"]


def test_missing_data_source_offer(local_client):
    job_id = start_job(
        local_client, kind="preview", ref="eg-test1", view_name="silver_input_layer_no_ds"
    )
    job = wait_job(local_client, job_id)
    assert job["status"] == "error"
    err = job["error"]
    assert err["type"] == "MissingDataSourceColumn"
    assert err["offer_disable_tenant_filter"] is True
    assert err["table_key"] == "silver.input_layer.no_ds" and err["ref"] == "eg-test1"
    assert err["table_status"] == "missing_data_source"
    job_id = start_job(
        local_client,
        kind="preview",
        ref="eg-test1",
        view_name="silver_input_layer_no_ds",
        tenant_filter=False,
    )
    job = wait_job(local_client, job_id)
    assert job["status"] == "done" and job["tenant_filter"] is False
    assert job["result"]["total_rows"] == 2


def test_csv_download_gated_by_allow_export(make_client, store, fx):
    store.save(local_profile(fx))
    store.save(local_profile(fx, name="export-p", allow_export=True))
    client = make_client()
    sql = "SELECT id, code FROM bronze_medical ORDER BY id"

    denied = wait_job(client, start_job(client, kind="query", ref="eg-test1", sql=sql))
    r = client.get(f"/api/jobs/{denied['id']}/csv")
    assert r.status_code == 403 and r.json()["error"]["type"] == "ExportNotAllowed"

    job = wait_job(
        client, start_job(client, kind="query", profile="export-p", ref="eg-test1", sql=sql)
    )
    r = client.get(f"/api/jobs/{job['id']}/csv")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["x-row-count"] == "3"
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == ["id", "code"]
    assert rows[1:] == [[str(a), b] for a, b in job["result"]["rows"]]
    schema = wait_job(
        client,
        start_job(client, kind="schema", profile="export-p", ref="eg-test1",
                  view_name="gold_summary"),
    )
    assert client.get(f"/api/jobs/{schema['id']}/csv").status_code == 409


def test_finished_jobs_are_capped_per_profile(make_client, store, fx):
    store.save(local_profile(fx))
    client = make_client(keep_jobs_per_profile=3)
    ids = []
    for i in range(5):
        ids.append(start_job(client, kind="query", ref="eg-test1", sql=f"SELECT {i}", channel=None))
        wait_job(client, ids[-1])
    assert client.get(f"/api/jobs/{ids[0]}").status_code == 404
    assert client.get(f"/api/jobs/{ids[-1]}").status_code == 200
    assert len(client.app.state.floe.jobs.jobs("local-p")) == 3


# --------------------------------------------------------------------------- history


def test_history_records_successful_queries_only(local_client):
    ok_sql = "SELECT count(*) FROM gold_summary"
    wait_job(local_client, start_job(local_client, kind="query", ref="eg-test1", sql=ok_sql))
    wait_job(local_client, start_job(local_client, kind="query", ref="eg-test1", sql="DROP x"))
    wait_job(
        local_client,
        start_job(local_client, kind="preview", ref="eg-test1", view_name="gold_summary"),
    )
    entries = local_client.get("/api/profiles/local-p/history").json()["entries"]
    assert [(e["sql"], e["branch"]) for e in entries] == [(ok_sql, "eg-test1")]
    assert set(entries[0]) == {"sql", "branch", "timestamp"}
    assert local_client.delete("/api/profiles/local-p/history").json() == {"entries": []}
    assert local_client.get("/api/profiles/local-p/history").json()["entries"] == []


# --------------------------------------------------------------------------- prefs


def test_prefs_round_trip_and_validation(client):
    assert client.get("/api/prefs").json() == {
        "last_profile": None,
        "last_tab": None,
        "last_branch": {},
        "editor_text": {},
    }
    r = client.put(
        "/api/prefs",
        json={
            "last_profile": "p1",
            "last_tab": "sql",
            "last_branch": {"p1": "eg-test1"},
            "editor_text": {"p1": "SELECT 1", "p2": "SELECT 2"},
        },
    )
    assert r.status_code == 200, r.text
    r = client.put("/api/prefs", json={"editor_text": {"p2": None, "p1": "SELECT 3"}})
    prefs = client.get("/api/prefs").json()
    assert prefs == {
        "last_profile": "p1",
        "last_tab": "sql",
        "last_branch": {"p1": "eg-test1"},
        "editor_text": {"p1": "SELECT 3"},
    }
    for bad in (
        {"bogus": 1},
        {"last_tab": "results"},
        {"last_profile": 5},
        {"editor_text": "x"},
        {"editor_text": {"p1": 5}},
        {"editor_text": {"p1": "x" * 300_000}},
        {"last_branch": {"": "x"}},
    ):
        assert client.put("/api/prefs", json=bad).status_code == 400, bad
    assert client.put("/api/prefs", json=["x"]).status_code == 422
    assert client.get("/api/prefs").json() == prefs
    on_disk = json.loads((client.app.state.floe.prefs.path).read_text())
    assert on_disk["editor_text"] == {"p1": "SELECT 3"}


# --------------------------------------------------------------------------- diagnostics


def test_diagnostics_and_about_have_no_secrets(client, store, fx):
    secret = "synthetic-diag-secret-ZZZZ-4242"
    store.save(local_profile(fx), secrets={"adls_account_key": secret})
    diagnostics.setup_logging()
    import logging

    logging.getLogger("floe.test").warning("about to leak %s", secret)
    r = client.get("/api/diagnostics", params={"profile": "local-p"})
    assert r.status_code == 200
    text = r.json()["text"]
    assert "local-p" in text and "Floe version" in text
    assert secret not in text
    about = client.get("/api/about").json()
    assert about == {"name": "Floe", "version": __version__, "commit": __commit__}
    for body in client.bodies:
        assert secret not in body


@pytest.mark.parametrize("value, expected", [
    (float("nan"), None),
    (float("-inf"), "-Infinity"),
    (2**60, str(2**60)),
    (-(2**53), -(2**53)),
    (b"a\\b", "a\\x5Cb"),
])
def test_json_safe_scalars(value, expected):
    from floe.web.serialize import json_safe

    assert json_safe(value) == expected


def test_json_safe_pandas_and_numpy():
    import numpy as np
    import pandas as pd

    from floe.web.serialize import json_safe

    assert json_safe(pd.NaT) is None and json_safe(pd.NA) is None
    assert json_safe(np.int64(5)) == 5 and json_safe(np.float32(1.5)) == 1.5
    assert json_safe(np.datetime64("NaT")) is None
    assert json_safe(pd.Timestamp("2024-01-02")) == "2024-01-02T00:00:00"
    assert json_safe(np.array([1, np.nan])) == [1.0, None]
    json.dumps([json_safe(v) for v in (pd.Timedelta("1D"), object())])
