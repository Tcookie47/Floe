"""Ask endpoints (SPEC §15.4): settings, preview body, and the Ask job with a fake
OpenRouter. No row data is ever sent, and generated SQL is never executed."""

from __future__ import annotations

import json

import keyring
import pytest

from floe.core import ask
from floe.core.context import FloeSession
from tests.web.conftest import local_profile, wait_job

API_KEY = "sk-or-synthetic-KEY-0123456789"
# Values that exist only in the fixture tables' rows (never in names or types).
ROW_SENTINELS = ("C001", "C003", "alpha", "o'delta", "synthetic-a", "10.5", "wh-synthetic")


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        body = {"model": "fake/model", "choices": [{"message": {"content": self.content}}]}
        return FakeResponse(200, json.dumps(body).encode())


@pytest.fixture
def query_calls():
    return []


@pytest.fixture
def spy_factory(query_calls):
    def factory(profile):
        session = FloeSession(profile)
        original = session.query

        def spy(*args, **kwargs):
            query_calls.append(args)
            return original(*args, **kwargs)

        session.query = spy
        return session

    return factory


def configure(client, **overrides):
    body = {"enabled": True, "model": "fake/model", "api_key": API_KEY, **overrides}
    r = client.put("/api/ask/settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_settings_api_key_is_write_only(client):
    got = client.get("/api/ask/settings").json()
    assert got["enabled"] is False and got["api_key"] == {"saved": False}
    got = configure(client, timeout_s=30, base_url="https://example.invalid/api/v1")
    assert got["api_key"] == {"saved": True}
    assert got["timeout_s"] == 30 and got["base_url"] == "https://example.invalid/api/v1"
    assert keyring.get_password(ask.KEYRING_SERVICE, ask.KEYRING_USERNAME) == API_KEY
    # Omitted key = unchanged; "" clears.
    assert client.put("/api/ask/settings", json={"model": "m2"}).json()["api_key"]["saved"]
    assert client.put("/api/ask/settings", json={"api_key": ""}).json()["api_key"] == {
        "saved": False
    }
    for bad in (
        {"enabled": "yes"},
        {"timeout_s": 0},
        {"base_url": "file:///etc/passwd"},
        {"base_url": "http://example.invalid/api/v1"},  # the API key would go in clear
        {"base_url": "http://127.0.0.1.example.invalid/v1"},
        {"model": 5},
        {"bogus": 1},
    ):
        assert client.put("/api/ask/settings", json=bad).status_code == 400, bad
    for body in client.bodies:
        assert API_KEY not in body
    assert API_KEY not in ask.settings_path().read_text()


def test_base_url_http_only_for_loopback(client):
    for url in ("http://127.0.0.1:9/v1", "http://localhost:9/v1", "http://[::1]:9/v1",
                "https://example.invalid/v1"):
        assert configure(client, base_url=url)["base_url"] == url
    r = client.put("/api/ask/settings", json={"base_url": "http://example.invalid/v1"})
    assert r.status_code == 400 and "https://" in r.json()["error"]["message"]


def test_ask_refuses_a_hand_edited_http_base_url(make_client, store, fx):
    store.save(local_profile(fx))
    client = make_client()
    configure(client)
    settings = ask.load_settings()
    settings.base_url = "http://example.invalid/api/v1"
    ask.save_settings(settings)
    r = client.post(
        "/api/ask", json={"profile": "local-p", "question": "q", "ref": "main"}
    )
    assert r.status_code == 400 and "base_url" in r.json()["error"]["message"]


def test_preview_body_has_schema_but_no_rows(make_client, store, fx, spy_factory, query_calls):
    store.save(local_profile(fx))
    client = make_client(session_factory=spy_factory)
    configure(client)
    r = client.post(
        "/api/ask/preview",
        json={
            "profile": "local-p",
            "ref": "eg-test1",
            "question": "total amount per code in gold_summary",
            "selected_view": "bronze_medical",
            "editor_sql": "SELECT * FROM gold_ref_codes",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()["body"]
    sent = json.loads(body)
    assert sent["model"] == "fake/model"
    assert r.json()["focus_views"] == ["bronze_medical", "gold_summary", "gold_ref_codes"]
    user = sent["messages"][1]["content"]
    assert "gold_summary" in user and "amount" in user and "label" in user
    assert "silver_input_layer_medical_claim" in user  # other view names
    for sentinel in ROW_SENTINELS:
        assert sentinel not in body
    assert str(fx.local_root) not in body and API_KEY not in body
    assert query_calls == []
    assert client.post(
        "/api/ask/preview", json={"profile": "local-p", "ref": "eg-test1", "question": " "}
    ).status_code == 400


def test_ask_job_returns_sql_and_never_executes_it(
    make_client, store, fx, spy_factory, query_calls
):
    store.save(local_profile(fx))
    opener = FakeOpener("Here you go:\n```sql\nSELECT code, sum(amount) FROM gold_summary "
                        "GROUP BY code;\n```")
    client = make_client(session_factory=spy_factory, ask_opener=opener)
    configure(client)
    r = client.post(
        "/api/ask",
        json={"profile": "local-p", "ref": "eg-test1", "question": "sum by code in gold_summary"},
    )
    assert r.status_code == 202, r.text
    job = wait_job(client, r.json()["id"])
    assert job["status"] == "done", job
    assert job["result"]["sql"] == "SELECT code, sum(amount) FROM gold_summary GROUP BY code"
    assert job["result"]["warnings"] == []
    assert job["result"]["focus_views"] == ["gold_summary"]
    assert query_calls == []  # generated SQL is never executed

    (req,) = opener.requests
    sent = req.data.decode()
    assert "gold_summary" in sent
    for sentinel in ROW_SENTINELS:
        assert sentinel not in sent
    assert req.get_header("Authorization") == f"Bearer {API_KEY}"
    for body in client.bodies:
        assert API_KEY not in body

    # A write statement comes back with a warning, still unexecuted.
    opener.content = "DROP TABLE gold_summary"
    job_id = client.post(
        "/api/jobs",
        json={"kind": "ask", "profile": "local-p", "ref": "eg-test1", "question": "drop it"},
    ).json()["id"]
    job = wait_job(client, job_id)
    assert job["result"]["sql"] == "DROP TABLE gold_summary"
    assert job["result"]["warnings"] and "read-only" in job["result"]["warnings"][0]
    assert query_calls == []


def test_ask_not_configured(make_client, store, fx):
    store.save(local_profile(fx))
    client = make_client(ask_opener=FakeOpener("SELECT 1"))
    payload = {"profile": "local-p", "ref": "eg-test1", "question": "anything"}
    r = client.post("/api/ask", json=payload)
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "AskNotConfigured"
    configure(client, api_key="")
    r = client.post("/api/ask", json=payload)
    assert r.status_code == 400 and "API key" in r.json()["error"]["message"]
