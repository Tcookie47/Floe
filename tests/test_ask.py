"""Tests for floe.core.ask (SPEC §15.4). No network: fake openers and a local http.server."""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import keyring
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from floe.core import ask, diagnostics
from floe.core.ask import (
    MODEL_CHAIN,
    AskAuthError,
    AskError,
    AskNotConfigured,
    AskQuotaError,
    AskResult,
    AskSettings,
    AskUnavailable,
    ColumnSchema,
    OpenRouterClient,
    TableSchema,
    build_prompt,
    extract_sql,
    get_api_key,
    get_model_chain,
    has_api_key,
    load_settings,
    pick_focus_views,
    preview_request,
    save_settings,
    set_api_key,
    validate_generated_sql,
)
from floe.core.context import ColumnInfo, FloeSession
from floe.core.profiles import Profile

FAKE_KEY = "sk-or-synthetic-TEST-KEY-0123456789abcdef"
SENTINELS = [
    "SENTINEL_ROW_VALUE_7f3a",
    "SENTINEL_ROW_VALUE_b19c",
    "SENTINEL_ROW_VALUE_e44d",
]
_KEY_B = FAKE_KEY.encode()
SENTINEL_NUMBERS = ["987654321987", "123456.789125"]


def _settings(**kw) -> AskSettings:
    values = dict(enabled=True, base_url="http://fake.invalid/api/v1")
    values.update(kw)
    return AskSettings(**values)


def _ok_body(content: str, model: str = "synthetic/model-a") -> bytes:
    return json.dumps(
        {"model": model, "choices": [{"message": {"role": "assistant", "content": content}}]}
    ).encode()


class _Resp(io.BytesIO):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status


class FakeOpener:
    """Records chat-completions requests; returns a canned response or raises a canned
    exception. `GET .../models` calls (the dynamic-slot fetch) are recorded separately in
    `model_requests` and, unless `models_body` is given, fail (HTTP 404) — so by default
    the model chain is just the 4 fixed models, no dynamic slot."""

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        exc: BaseException | None = None,
        models_status: int = 404,
        models_body: bytes = b"",
    ):
        self.status, self.body, self.exc = status, body, exc
        self.models_status, self.models_body = models_status, models_body
        self.requests: list = []
        self.model_requests: list = []

    def open(self, req, timeout=None):
        if req.get_method() == "GET":
            self.model_requests.append((req, timeout))
            if self.models_status >= 400:
                raise urllib.error.HTTPError(
                    req.full_url, self.models_status, "err", {}, io.BytesIO(self.models_body)
                )
            return _Resp(self.models_status, self.models_body)
        self.requests.append((req, timeout))
        if self.exc is not None:
            raise self.exc
        if self.status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, self.status, "err", {}, io.BytesIO(self.body)
            )
        return _Resp(self.status, self.body)


class CapturingServer:
    """A local HTTP server that captures every request (path, headers, body)."""

    def __init__(self, reply: bytes, status: int = 200) -> None:
        self.captured: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.captured.append(
                    {"path": self.path, "headers": dict(self.headers.items()), "body": body}
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/api/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(ask.API_KEY_ENV, raising=False)
    ask._model_chain_mem_cache.clear()
    yield
    diagnostics.clear_secrets()
    ask._model_chain_mem_cache.clear()


# --------------------------------------------------------------------------- no-data guarantee


@pytest.fixture
def sentinel_session(tmp_path):
    root = tmp_path / "local"
    table_dir = root / "eg-test1" / "gold" / "sentinel_facts"
    table_dir.mkdir(parents=True)
    data = pa.table(
        {
            "id": pa.array([int(SENTINEL_NUMBERS[0]), 2, 3], pa.int64()),
            "amount": pa.array([float(SENTINEL_NUMBERS[1]), 1.0, 2.0], pa.float64()),
            "label": pa.array(SENTINELS, pa.string()),
            "data_source": pa.array(["eg-test1"] * 3, pa.string()),
        }
    )
    pq.write_table(data, table_dir / "part-0.parquet")
    other_dir = root / "eg-test1" / "silver" / "other_thing"
    other_dir.mkdir(parents=True)
    pq.write_table(data, other_dir / "part-0.parquet")
    return FloeSession(Profile(name="local-ask", mode="local", local_fixture_dir=str(root)))


def test_no_row_data_is_ever_sent(sentinel_session):
    session = sentinel_session
    ref = "eg-test1"
    # Sanity: the sentinel rows really are in the table.
    df = session.query(ref, "SELECT label FROM gold_sentinel_facts").df
    assert set(df["label"]) == set(SENTINELS)

    known = [t.view_name for t in session.list_tables(ref)]
    question = "Total amount per label in gold_sentinel_facts?"
    focus_names = pick_focus_views(question, "SELECT 1", None, known)
    assert focus_names == ["gold_sentinel_facts"]
    focus = [TableSchema.from_columns(v, session.schema(ref, v)) for v in focus_names]
    others = [v for v in known if v not in focus_names]
    messages = build_prompt(question, focus, others)

    reply = _ok_body("```sql\nSELECT label, sum(amount) FROM gold_sentinel_facts GROUP BY 1;\n```")
    with CapturingServer(reply) as server:
        settings = _settings(base_url=server.base_url, timeout_s=5)
        result = OpenRouterClient(settings, FAKE_KEY).generate(messages)
    assert result.sql == "SELECT label, sum(amount) FROM gold_sentinel_facts GROUP BY 1"
    assert validate_generated_sql(result.sql) == []

    assert len(server.captured) == 1
    req = server.captured[0]
    assert req["path"] == "/api/v1/chat/completions"
    blob = req["body"].decode("utf-8") + json.dumps(req["headers"]) + req["path"]
    for value in SENTINELS + SENTINEL_NUMBERS + ["SENTINEL"]:
        assert value not in blob
    assert "label" in blob and "VARCHAR" in blob and "other_thing" in blob
    assert req["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert req["headers"]["X-Title"] == "Floe"
    # The body sent is exactly the previewed body (first model in the chain succeeded).
    assert req["body"].decode("utf-8") == preview_request(messages)
    assert result.model == "synthetic/model-a"  # from the fake reply's top-level "model"
    assert result.attempts == ((MODEL_CHAIN[0], "ok"),)


@pytest.mark.parametrize(
    "bad",
    [
        pd.DataFrame({"label": SENTINELS}),
        {"label": SENTINELS},
        [{"label": SENTINELS[0]}],
        [pd.DataFrame({"label": SENTINELS})],
        ["gold_sentinel_facts"],
    ],
)
def test_build_prompt_rejects_anything_but_schema_objects(bad):
    with pytest.raises(TypeError):
        build_prompt("q", bad, [])


@pytest.mark.parametrize(
    "bad", [pd.DataFrame({"v": SENTINELS}), {"a": 1}, [1, 2], [("a", "b")], "view_a"]
)
def test_build_prompt_rejects_bad_other_views(bad):
    with pytest.raises(TypeError):
        build_prompt("q", [], bad)


def test_build_prompt_rejects_non_str_question():
    with pytest.raises(TypeError):
        build_prompt(pd.DataFrame({"v": [1]}), [], [])


def test_schema_types_hold_only_names_and_types():
    cols = [ColumnInfo("id", "BIGINT", True), ColumnInfo("label", "VARCHAR", False)]
    t = TableSchema.from_columns("gold_x", cols)
    expected = (ColumnSchema("id", "BIGINT"), ColumnSchema("label", "VARCHAR"))
    assert t == TableSchema("gold_x", expected)
    with pytest.raises(TypeError):
        TableSchema.from_columns("gold_x", [{"name": "id", "type": "BIGINT"}])
    with pytest.raises(TypeError):
        TableSchema("gold_x", [ColumnSchema("id", "BIGINT")])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ColumnSchema("id", 5)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- prompt content


def _table(name: str, n: int) -> TableSchema:
    return TableSchema(name, tuple(ColumnSchema(f"col_{i}", "VARCHAR") for i in range(n)))


def test_prompt_content():
    cols = (ColumnSchema("id", "BIGINT"), ColumnSchema("Amt", "DOUBLE"))
    focus = [TableSchema("gold_summary", cols)]
    msgs = build_prompt("How many rows?", focus, ["bronze_medical", "gold_ref_codes"])
    assert [m["role"] for m in msgs] == ["system", "user"]
    system, user = msgs[0]["content"], msgs[1]["content"]
    assert "DuckDB" in system
    assert "exactly one read-only" in system
    assert "already filtered to the current tenant" in system and "data_source" in system
    assert "```sql" in system
    assert "Quote identifiers" in system
    assert "How many rows?" in user
    assert "gold_summary(id BIGINT, Amt DOUBLE)" in user
    assert "bronze_medical, gold_ref_codes" in user
    assert "truncated" not in user


def test_prompt_size_cap_truncates_other_views_first_then_later_columns():
    focus = [_table("view_first", 30), _table("view_second", 30)]
    others = [f"other_view_{i:04d}" for i in range(500)]
    full = build_prompt("q?", focus, others, max_chars=10**6)[1]["content"]
    assert "truncated" not in full

    # Enough room for all columns but not all other views.
    cap = len(build_prompt("q?", focus, [], max_chars=10**6)[1]["content"]) + 400
    user = build_prompt("q?", focus, others, max_chars=cap)[1]["content"]
    assert len(user) <= cap
    assert "col_29 VARCHAR" in user.split("view_second")[1]
    assert "more views omitted" in user and "truncated" in user
    assert "other_view_0000" in user and "other_view_0499" not in user

    # Tight cap: other views all gone, then the second table loses columns before the first.
    user = build_prompt("q?", focus, others, max_chars=900)[1]["content"]
    assert len(user) <= 900
    assert "other_view_" not in user and "500 more views omitted" in user
    first, second = user.split("view_second")
    assert "col_29 VARCHAR" in first
    assert "more columns omitted" in second or "columns omitted" in second
    assert "truncated" in user

    with pytest.raises(AskError):
        build_prompt("x" * 2000, focus, others, max_chars=500)


def test_prompt_other_views_exclude_focus_and_dedupe():
    user = build_prompt("q", [_table("a_view", 1)], ["a_view", "b_view", "b_view"])[1]["content"]
    assert user.count("b_view") == 1
    assert "Other views (columns not listed):\nb_view" in user


def test_pick_focus_views():
    known = ["gold_summary", "bronze_medical", "gold_ref_codes", "summary", "silver_x"]
    got = pick_focus_views(
        "compare GOLD_SUMMARY with bronze_medicalx and summary",
        "select * from silver_x join gold_ref_codes",
        "bronze_medical",
        known,
    )
    assert got == ["bronze_medical", "gold_summary", "summary", "silver_x", "gold_ref_codes"]
    many = [f"v{i}" for i in range(20)]
    assert len(pick_focus_views(" ".join(many), "", None, many)) == 8
    assert pick_focus_views("q", "", "not_known", known) == []


# --------------------------------------------------------------------------- settings


def test_settings_round_trip_and_key_never_in_json(tmp_path):
    from floe.core import paths

    assert load_settings() == AskSettings()  # missing file -> defaults, disabled
    assert AskSettings().enabled is False
    s = AskSettings(enabled=True, base_url="http://x.invalid/v1", timeout_s=7)
    save_settings(s)
    path = paths.app_support_dir() / "ask_settings.json"
    assert path.exists()
    if sys.platform != "win32":  # POSIX mode bits; Windows only has a read-only flag
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_settings() == s
    assert "model" not in json.loads(path.read_text())

    set_api_key(FAKE_KEY)
    save_settings(s)
    assert FAKE_KEY not in path.read_text()
    assert keyring.get_password("Floe", "ask:openrouter_api_key") == FAKE_KEY
    assert get_api_key() == FAKE_KEY and has_api_key()
    set_api_key("")
    assert not has_api_key()

    path.write_text("{not json")
    assert load_settings() == AskSettings()
    path.write_text(json.dumps({"enabled": "yes", "timeout_s": -1, "model": 3}))
    assert load_settings() == AskSettings()


def test_old_settings_file_with_model_field_loads_fine(tmp_path):
    """A settings file written by a version of Floe that still had a user-chosen model
    must load without error; the old `model` value is simply ignored."""
    from floe.core import paths

    path = paths.app_support_dir() / "ask_settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "model": "some/old-model:free",
                "base_url": "http://old.invalid/v1",
                "timeout_s": 42,
            }
        )
    )
    settings = load_settings(path)
    assert settings == AskSettings(enabled=True, base_url="http://old.invalid/v1", timeout_s=42)
    assert not hasattr(settings, "model")


def test_api_key_env_fallback_and_registered(monkeypatch):
    assert get_api_key() is None
    monkeypatch.setenv(ask.API_KEY_ENV, FAKE_KEY)
    assert get_api_key() == FAKE_KEY
    assert FAKE_KEY not in diagnostics.redact(f"x {FAKE_KEY} y")
    # Keyring wins over the env var.
    set_api_key("sk-or-synthetic-keyring-value")
    assert get_api_key() == "sk-or-synthetic-keyring-value"


@pytest.mark.parametrize(
    "settings,key,needle",
    [
        (AskSettings(enabled=False), FAKE_KEY, "turned off"),
        (AskSettings(enabled=True), None, "API key"),
    ],
)
def test_not_configured(settings, key, needle):
    with pytest.raises(AskNotConfigured) as ei:
        OpenRouterClient(settings, key, opener=FakeOpener())
    assert needle in ei.value.user_message()


# --------------------------------------------------------------------------- client


def _messages():
    return build_prompt("count rows", [_table("gold_summary", 2)], [])


def test_client_success():
    reply = "Here:\n```sql\n-- count\nSELECT count(*) FROM gold_summary;\n```"
    opener = FakeOpener(body=_ok_body(reply))
    settings = _settings(timeout_s=9)
    result = OpenRouterClient(settings, FAKE_KEY, opener=opener).generate(_messages())
    assert isinstance(result, AskResult)
    assert result.sql == "-- count\nSELECT count(*) FROM gold_summary"
    assert result.model == "synthetic/model-a" and result.elapsed_ms >= 0
    assert result.attempts == ((MODEL_CHAIN[0], "ok"),)
    assert result.fell_back is False
    assert len(opener.requests) == 1  # the first model in the chain succeeded
    req, timeout = opener.requests[0]
    assert 0 < timeout <= 9
    assert req.full_url == "http://fake.invalid/api/v1/chat/completions"
    assert req.get_method() == "POST"
    body = json.loads(req.data)
    assert body == {"model": MODEL_CHAIN[0], "messages": _messages(), "temperature": 0}
    assert req.get_header("Authorization") == f"Bearer {FAKE_KEY}"
    assert req.get_header("Content-type") == "application/json"
    assert FAKE_KEY not in preview_request(_messages())


@pytest.mark.parametrize(
    "opener,exc_type,needle",
    [
        (FakeOpener(401, b'{"error":{"message":"bad ' + _KEY_B + b'"}}'), AskAuthError, "API key"),
        (FakeOpener(402, b"{}"), AskQuotaError, "credits"),
    ],
)
def test_no_fallback_on_auth_and_quota_errors(opener, exc_type, needle, caplog):
    """A bad key or no credits is not a per-model problem: raise immediately, after
    exactly one attempt, never trying the rest of the chain."""
    caplog.set_level(logging.DEBUG, logger="floe")
    client = OpenRouterClient(_settings(), FAKE_KEY, opener=opener)
    with pytest.raises(exc_type) as ei:
        client.generate(_messages())
    err = ei.value
    assert needle in err.user_message()
    assert err.attempts == ()
    for text in (str(err), err.user_message(), repr(err.args), caplog.text):
        assert FAKE_KEY not in text
    assert len(opener.requests) == 1


@pytest.mark.parametrize(
    "opener,reason_needle",
    [
        (FakeOpener(429, b'{"error":{"message":"slow down"}}'), "429"),
        (FakeOpener(500, b"oops " + FAKE_KEY.encode()), "server error"),
        (FakeOpener(503, b""), "server error"),
        (FakeOpener(404, b'{"error":{"message":"missing"}}'), "404"),
        (
            FakeOpener(400, b'{"error":{"message":"model ' + _KEY_B + b' not found"}}'),
            "unavailable",
        ),
        (FakeOpener(exc=TimeoutError("timed out")), "timeout"),
        (FakeOpener(exc=urllib.error.URLError(TimeoutError("timed out"))), "timeout"),
        (FakeOpener(exc=urllib.error.URLError(ConnectionRefusedError())), "timeout"),
        (FakeOpener(body=b"not json"), "JSON"),
        (FakeOpener(body=b'{"choices": []}'), "response"),
        (FakeOpener(body=b'{"error": {"code": 429, "message": "x"}}'), "429"),
        (FakeOpener(body=_ok_body("")), "empty"),
    ],
)
def test_fallback_exhausts_chain_then_raises_unavailable(opener, reason_needle, caplog):
    caplog.set_level(logging.DEBUG, logger="floe")
    client = OpenRouterClient(_settings(), FAKE_KEY, opener=opener)
    with pytest.raises(AskUnavailable) as ei:
        client.generate(_messages())
    err = ei.value
    assert "All free models are busy or unavailable" in err.user_message()
    assert [m for m, _ in err.attempts] == list(MODEL_CHAIN)
    assert all(
        reason_needle in outcome or reason_needle in outcome.lower() for _, outcome in err.attempts
    )
    assert len(opener.requests) == len(MODEL_CHAIN)  # every model was tried
    for text in (str(err), err.user_message(), repr(err.args), caplog.text):
        assert FAKE_KEY not in text
    assert "count rows" not in caplog.text  # never log the prompt


def test_client_logs_metadata_only(caplog):
    caplog.set_level(logging.DEBUG, logger="floe")
    opener = FakeOpener(body=_ok_body("```sql\nSELECT 42 AS answer_text_marker\n```"))
    OpenRouterClient(_settings(), FAKE_KEY, opener=opener).generate(_messages())
    assert "status=200" in caplog.text and "prompt_chars=" in caplog.text
    for secret in (FAKE_KEY, "count rows", "answer_text_marker", "gold_summary"):
        assert secret not in caplog.text


def test_generate_rejects_bad_messages():
    client = OpenRouterClient(_settings(), FAKE_KEY, opener=FakeOpener())
    with pytest.raises(TypeError):
        client.generate([{"role": "user", "content": "x", "rows": [1]}])
    with pytest.raises(TypeError):
        client.generate(pd.DataFrame({"a": [1]}))


# --------------------------------------------------------------------------- model chain


def _model(id_: str, context_length: int = 32_000) -> dict:
    return {"id": id_, "context_length": context_length}


def test_model_chain_fixed_order_and_length():
    assert MODEL_CHAIN == (
        "qwen/qwen3-coder:free",
        "openai/gpt-oss-120b:free",
        "openai/gpt-oss-20b:free",
        "openrouter/free",
    )


def test_get_model_chain_fetch_failure_yields_four_fixed_models():
    opener = FakeOpener(body=_ok_body("x"), models_status=500)
    chain = get_model_chain(_settings(), FAKE_KEY, opener=opener, now=1000.0)
    assert chain == list(MODEL_CHAIN)


def test_get_model_chain_picks_coder_model_for_dynamic_slot():
    models = {
        "data": [
            _model("qwen/qwen3-coder:free"),
            _model("openai/gpt-oss-120b:free"),
            _model("openai/gpt-oss-20b:free"),
            _model("openrouter/free"),
            _model("some-org/great-coder-model:free", context_length=200_000),
            _model("some-org/generic-chat:free", context_length=500_000),
            _model("some-org/paid-model"),  # not :free -> never picked
        ]
    }
    opener = FakeOpener(
        body=_ok_body("x"), models_status=200, models_body=json.dumps(models).encode()
    )
    chain = get_model_chain(_settings(), FAKE_KEY, opener=opener, now=1000.0)
    assert chain == [
        "qwen/qwen3-coder:free",
        "openai/gpt-oss-120b:free",
        "openai/gpt-oss-20b:free",
        "some-org/great-coder-model:free",
        "openrouter/free",
    ]


def test_get_model_chain_prefers_larger_context_within_a_tier():
    models = {
        "data": [
            _model("org-a/instruct-small:free", context_length=8_000),
            _model("openai/instruct-big:free", context_length=300_000),
        ]
    }
    opener = FakeOpener(
        body=_ok_body("x"), models_status=200, models_body=json.dumps(models).encode()
    )
    chain = get_model_chain(_settings(), FAKE_KEY, opener=opener, now=1000.0)
    # Both are non-coder well-known/instruct tier: the larger context wins the slot.
    assert chain[-2] == "openai/instruct-big:free"


def test_get_model_chain_drops_missing_fixed_id_when_fetch_succeeds():
    models = {
        "data": [
            _model("openai/gpt-oss-120b:free"),
            _model("openai/gpt-oss-20b:free"),
            _model("openrouter/free"),
            # qwen/qwen3-coder:free is missing from the live list -> dropped.
        ]
    }
    opener = FakeOpener(
        body=_ok_body("x"), models_status=200, models_body=json.dumps(models).encode()
    )
    chain = get_model_chain(_settings(), FAKE_KEY, opener=opener, now=1000.0)
    assert "qwen/qwen3-coder:free" not in chain
    assert chain[0] == "openai/gpt-oss-120b:free"
    assert chain[-1] == "openrouter/free"


def test_get_model_chain_caches_for_24h():
    models = {"data": [_model("some-org/great-coder-model:free")]}
    opener = FakeOpener(
        body=_ok_body("x"), models_status=200, models_body=json.dumps(models).encode()
    )
    settings = _settings()
    chain1 = get_model_chain(settings, FAKE_KEY, opener=opener, now=1000.0)
    assert len(opener.model_requests) == 1
    # Well within 24h: no re-fetch, same chain, no extra request.
    chain2 = get_model_chain(settings, FAKE_KEY, opener=opener, now=1000.0 + 3600)
    assert chain2 == chain1
    assert len(opener.model_requests) == 1
    # Past 24h: re-fetch.
    get_model_chain(settings, FAKE_KEY, opener=opener, now=1000.0 + 24 * 3600 + 1)
    assert len(opener.model_requests) == 2


def test_get_model_chain_sends_key_but_does_not_require_it():
    models = {"data": []}
    opener = FakeOpener(models_status=200, models_body=json.dumps(models).encode())
    get_model_chain(_settings(), FAKE_KEY, opener=opener, now=1000.0)
    req, _ = opener.model_requests[0]
    assert req.get_method() == "GET"
    assert req.full_url == "http://fake.invalid/api/v1/models"
    assert req.get_header("Authorization") == f"Bearer {FAKE_KEY}"
    ask._model_chain_mem_cache.clear()
    opener2 = FakeOpener(models_status=200, models_body=json.dumps(models).encode())
    get_model_chain(_settings(), None, opener=opener2, now=1000.0 + 24 * 3600 + 1)
    req2, _ = opener2.model_requests[0]
    assert req2.get_header("Authorization") is None


# --------------------------------------------------------------------------- output handling


@pytest.mark.parametrize(
    "text,expected",
    [
        ("```sql\nSELECT 1;\n```", "SELECT 1"),
        ("Sure!\n```SQL\nSELECT a FROM t;;  \n```\nThat's it.", "SELECT a FROM t"),
        ("```\nSELECT 2\n```", "SELECT 2"),
        ("```python\nx=1\n```\n```sql\nSELECT 3\n```", "SELECT 3"),
        ("SELECT 4;  \n", "SELECT 4"),
        ("```sql\nSELECT 5\n", "SELECT 5"),
        ("```sql\n-- top ids\nSELECT id FROM t\n```", "-- top ids\nSELECT id FROM t"),
    ],
)
def test_extract_sql(text, expected):
    assert extract_sql(text) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM gold_summary",
        "COPY gold_summary TO 'out.csv'",
        "SELECT * FROM read_parquet('x.parquet')",
        "SELECT 1; SELECT 2",
        "SELECT * FROM 'some/file.parquet'",
        "",
    ],
)
def test_validate_generated_sql_flags(sql):
    warnings = validate_generated_sql(sql)
    assert warnings and all(isinstance(w, str) and w for w in warnings)


def test_validate_generated_sql_passes_select():
    assert validate_generated_sql("WITH x AS (SELECT id FROM gold_summary) SELECT * FROM x") == []
    assert validate_generated_sql('SELECT "Amt", count(*) FROM gold_summary GROUP BY 1') == []
