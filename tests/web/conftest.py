"""Fixtures for the web-app tests: a TestClient on a synthetic local-mode profile."""

from __future__ import annotations

import re
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from floe.core.profiles import Profile, ProfileStore
from floe.web.app import create_app
from tests.fakes.iceberg_fixtures import REGISTRY_KEY, SHARED_CONTAINERS, SHARED_NAMESPACES

TOKEN = "test-token-" + "k" * 32
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
FINISHED = ("done", "error", "cancelled")
SLOW_SQL = (
    "SELECT count(*) FROM range(1000000000) a, range(1000000) b "
    "WHERE a.range + b.range = -1"
)


@pytest.fixture
def fx(iceberg_fixtures):
    return iceberg_fixtures


def local_profile(fx, name: str = "local-p", **overrides: Any) -> Profile:
    values: dict[str, Any] = dict(
        name=name,
        mode="local",
        local_fixture_dir=str(fx.local_root),
        shared_containers=list(SHARED_CONTAINERS),
        shared_namespaces=list(SHARED_NAMESPACES),
        tenant_registry_table=REGISTRY_KEY,
        preview_row_limit=2,
    )
    values.update(overrides)
    return Profile(**values)


def signed_in_key(response) -> str:
    """The API key from the post-sign-in page's same-origin meta refresh to `/#k=<key>`."""
    assert response.status_code == 200, response.text
    m = re.search(r'<meta http-equiv="refresh" content="0;url=/#k=([A-Za-z0-9_-]+)">',
                  response.text)
    assert m, response.text
    return m.group(1)


def login(client: TestClient) -> TestClient:
    """Exchange the launch token for the session cookie, and send the per-launch API key
    (delivered in the sign-in page's refresh URL fragment) on every request, as `api.js` does."""
    r = client.get(f"/?token={TOKEN}", follow_redirects=False)
    client.headers["X-Floe-Auth"] = signed_in_key(r)
    client.headers["Origin"] = BASE
    return client


@pytest.fixture
def store() -> ProfileStore:
    return ProfileStore()


@pytest.fixture
def make_client(store):
    """`make_client(**create_app_kwargs)` → a logged-in TestClient whose response bodies
    are all recorded in `client.bodies` (to search them for secrets). Every client is
    closed (and its job threads shut down) at teardown."""
    clients: list[TestClient] = []

    def make(*, logged_in: bool = True, **kwargs: Any) -> TestClient:
        kwargs.setdefault("store", store)
        app = create_app(token=TOKEN, port=PORT, **kwargs)
        client = TestClient(app, base_url=BASE)
        client.__enter__()
        clients.append(client)
        bodies: list[str] = []

        def record(response) -> None:
            response.read()
            bodies.append(response.text)
            bodies.append(str(response.headers))

        client.event_hooks["response"].append(record)
        client.bodies = bodies  # type: ignore[attr-defined]
        return login(client) if logged_in else client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def local_client(make_client, store, fx):
    store.save(local_profile(fx))
    return make_client()


def wait_job(client: TestClient, job_id: str, timeout: float = 30.0, **params: Any) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        r = client.get(f"/api/jobs/{job_id}", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in FINISHED:
            return body
        if time.monotonic() > deadline:
            pytest.fail(f"job {job_id} still {body['status']} after {timeout} s")
        time.sleep(0.02)


def wait_status(client: TestClient, job_id: str, status: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] == status or body["status"] in FINISHED:
            return body
        if time.monotonic() > deadline:
            pytest.fail(f"job {job_id} never reached {status}")
        time.sleep(0.02)


def start_job(client: TestClient, **payload: Any) -> str:
    payload.setdefault("profile", "local-p")
    r = client.post("/api/jobs", json=payload)
    assert r.status_code == 202, r.text
    return r.json()["id"]
