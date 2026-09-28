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
    AskAuthError,
    AskError,
    AskNotConfigured,
    AskQuotaError,
    AskRateLimited,
    AskResult,
    AskSettings,
    AskUnavailable,
    ColumnSchema,
    OpenRouterClient,
    TableSchema,
    build_prompt,
    extract_sql,
    get_api_key,
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
    values = dict(enabled=True, model="synthetic/model-a", base_url="http://fake.invalid/api/v1")
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
    """Records requests; returns a canned response or raises a canned exception."""

    def __init__(self, status: int = 200, body: bytes = b"", exc: BaseException | None = None):
        self.status, self.body, self.exc = status, body, exc
        self.requests: list = []

    def open(self, req, timeout=None):
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
    yield
    diagnostics.clear_secrets()


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
    # The body sent is exactly the previewed body.
    assert req["body"].decode("utf-8") == preview_request(messages, settings)


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

    assert load_settings() == AskSettings()  # missing file -> defaults, disabled, no model
    assert AskSettings().enabled is False and AskSettings().model == ""
    s = AskSettings(
        enabled=True, model="synthetic/model-b", base_url="http://x.invalid/v1", timeout_s=7
    )
    save_settings(s)
    path = paths.app_support_dir() / "ask_settings.json"
    assert path.exists()
    if sys.platform != "win32":  # POSIX mode bits; Windows only has a read-only flag
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert load_settings() == s

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
        (AskSettings(enabled=False, model="m/x"), FAKE_KEY, "turned off"),
        (AskSettings(enabled=True, model=""), FAKE_KEY, "No model"),
        (AskSettings(enabled=True, model="m/x"), None, "API key"),
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
    req, timeout = opener.requests[0]
    assert timeout == 9
    assert req.full_url == "http://fake.invalid/api/v1/chat/completions"
    assert req.get_method() == "POST"
    body = json.loads(req.data)
    assert body == {"model": "synthetic/model-a", "messages": _messages(), "temperature": 0}
    assert req.get_header("Authorization") == f"Bearer {FAKE_KEY}"
    assert req.get_header("Content-type") == "application/json"
    assert FAKE_KEY not in preview_request(_messages(), settings)


@pytest.mark.parametrize(
    "opener,exc_type",
    [
        (FakeOpener(401, b'{"error":{"message":"bad ' + _KEY_B + b'"}}'), AskAuthError),
        (FakeOpener(402, b"{}"), AskQuotaError),
        (FakeOpener(429, b'{"error":{"message":"slow down"}}'), AskRateLimited),
        (FakeOpener(500, b"oops " + FAKE_KEY.encode()), AskUnavailable),
        (FakeOpener(503, b""), AskUnavailable),
        (FakeOpener(exc=TimeoutError("timed out")), AskUnavailable),
        (FakeOpener(exc=urllib.error.URLError(TimeoutError("timed out"))), AskUnavailable),
        (FakeOpener(exc=urllib.error.URLError(ConnectionRefusedError())), AskUnavailable),
        (FakeOpener(400, b'{"error":{"message":"m ' + _KEY_B + b' not found"}}'), AskError),
        (FakeOpener(body=b"not json"), AskError),
        (FakeOpener(body=b'{"choices": []}'), AskError),
        (FakeOpener(body=b'{"error": {"code": 429, "message": "x"}}'), AskRateLimited),
        (FakeOpener(body=_ok_body("")), AskError),
    ],
)
def test_client_errors_are_typed_and_redacted(opener, exc_type, caplog):
    caplog.set_level(logging.DEBUG, logger="floe")
    client = OpenRouterClient(_settings(), FAKE_KEY, opener=opener)
    with pytest.raises(exc_type) as ei:
        client.generate(_messages())
    err = ei.value
    for text in (str(err), err.user_message(), repr(err.args), caplog.text):
        assert FAKE_KEY not in text
    assert "count rows" not in caplog.text  # never log the prompt
    assert "ask model=synthetic/model-a status=" in caplog.text


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
