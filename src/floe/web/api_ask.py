"""Ask endpoints (SPEC §15.4): settings, "What will be sent" preview, and the Ask job.

The prompt is built from schema only: view names from the catalog listing and column
names/types from `FloeSession.schema()` (DESCRIBE — metadata, never rows), passed through
`core/ask.py`'s `TableSchema`, which accepts nothing else. The generated SQL is returned
to the browser for the editor and checked with `validate_generated_sql`; it is never
executed here.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

from floe.core import ask
from floe.core.context import CancelToken, FloeSession
from floe.core.errors import FloeError, QueryCancelled
from floe.web import api_profiles
from floe.web.jobs import Job
from floe.web.sessions import SessionManager
from floe.web.state import ApiError, JsonBody, State, WebState

log = logging.getLogger("floe.web.ask")

router = APIRouter(prefix="/api/ask")

MAX_QUESTION = 4_000
MAX_EDITOR_SQL = 100_000
MAX_MODEL = 200
MAX_BASE_URL = 500
MAX_TIMEOUT_S = 600
SETTINGS_KEYS = {"enabled", "model", "base_url", "timeout_s", "api_key"}


def _bad(message: str) -> ApiError:
    return ApiError(400, "ValidationError", message)


# --------------------------------------------------------------------------- settings


def _settings_payload() -> dict[str, Any]:
    settings = ask.load_settings()
    return {
        **settings.to_dict(),
        "api_key": {"saved": ask.has_api_key()},
        "api_key_env": ask.API_KEY_ENV,
    }


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return _settings_payload()


@router.put("/settings")
def put_settings(payload: JsonBody) -> dict[str, Any]:
    """Partial update. `api_key` is write-only: omitted/null = unchanged, "" = delete."""
    unknown = set(payload) - SETTINGS_KEYS
    if unknown:
        raise _bad(f"Unknown Ask setting(s): {', '.join(sorted(map(str, unknown)))}.")
    current = ask.load_settings().to_dict()
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise _bad("enabled must be true or false.")
        current["enabled"] = payload["enabled"]
    if "model" in payload:
        model = payload["model"]
        if not isinstance(model, str) or len(model) > MAX_MODEL:
            raise _bad("model must be a string.")
        current["model"] = model.strip()
    if "base_url" in payload:
        url = payload["base_url"]
        if not isinstance(url, str) or len(url) > MAX_BASE_URL:
            raise _bad("base_url must be a string.")
        url = url.strip() or ask.DEFAULT_BASE_URL
        # The OpenRouter API key is sent there: https, or http only to this machine.
        api_profiles.require_https_unless_loopback("base_url", url)
        current["base_url"] = url
    if "timeout_s" in payload:
        timeout = payload["timeout_s"]
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= MAX_TIMEOUT_S
        ):
            raise _bad(f"timeout_s must be an integer from 1 to {MAX_TIMEOUT_S}.")
        current["timeout_s"] = timeout
    api_key = payload.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 1000):
        raise _bad("api_key must be a string.")
    if api_key is not None:
        ask.set_api_key(api_key)
    ask.save_settings(ask.AskSettings.from_dict(current))
    return _settings_payload()


# --------------------------------------------------------------------------- prompt


def _question_args(payload: dict[str, Any]) -> tuple[str, str | None, str, str, bool]:
    question = payload.get("question")
    if not isinstance(question, str) or len(question) > MAX_QUESTION:
        raise _bad(f"question must be a string (max {MAX_QUESTION} characters).")
    selected = payload.get("selected_view")
    if selected is not None and not isinstance(selected, str):
        raise _bad("selected_view must be a string or null.")
    editor_sql = payload.get("editor_sql") or ""
    if not isinstance(editor_sql, str) or len(editor_sql) > MAX_EDITOR_SQL:
        raise _bad("editor_sql must be a string.")
    ref = payload.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        raise _bad("ref (branch) is required.")
    tenant_filter = payload.get("tenant_filter", True)
    if not isinstance(tenant_filter, bool):
        raise _bad("tenant_filter must be true or false.")
    return question, selected, editor_sql, ref, tenant_filter


def build_messages(
    session: FloeSession,
    ref: str,
    question: str,
    selected_view: str | None,
    editor_sql: str,
    tenant_filter: bool,
    cancel_token: CancelToken,
) -> tuple[list[dict[str, str]], list[str]]:
    """Chat messages from the catalog listing + DESCRIBE of the focus views only."""
    known = [t.view_name for t in session.list_tables(ref) if t.status != "error"]
    focus_names = ask.pick_focus_views(question, editor_sql, selected_view, known)
    focus: list[ask.TableSchema] = []
    for view in focus_names:
        try:
            columns = session.schema(
                ref, view, tenant_filter=tenant_filter, cancel_token=cancel_token
            )
        except QueryCancelled:
            raise
        except FloeError as exc:
            log.info("Ask: schema unavailable for a focus view (%s)", type(exc).__name__)
            continue
        focus.append(ask.TableSchema.from_columns(view, columns))
    sent = [t.view_name for t in focus]
    others = [v for v in known if v not in sent]
    return ask.build_prompt(question, focus, others), sent


def _profile_name(state: WebState, payload: dict[str, Any]) -> str:
    name = api_profiles.check_name(payload.get("profile"), "profile")
    if state.store.get(name) is None:
        raise ApiError(404, "ProfileNotFound", f"No such profile: {name}")
    return name


@router.post("/preview")
def preview(
    payload: JsonBody, state: State
) -> dict[str, Any]:
    """The exact JSON body `POST /api/ask` would send (no API key; runs in a worker)."""
    name = _profile_name(state, payload)
    question, selected, editor_sql, ref, tenant_filter = _question_args(payload)
    session = state.sessions.get(name)
    messages, focus = build_messages(
        session, ref, question, selected, editor_sql, tenant_filter, CancelToken()
    )
    return {
        "body": ask.preview_request(messages, ask.load_settings()),
        "focus_views": focus,
    }


# --------------------------------------------------------------------------- ask job


def start_ask(
    state: WebState, name: str, payload: dict[str, Any], channel: str | None = "ask"
) -> Job:
    if state.store.get(name) is None:
        raise ApiError(404, "ProfileNotFound", f"No such profile: {name}")
    question, selected, editor_sql, ref, tenant_filter = _question_args(payload)
    settings = ask.load_settings()
    api_key = ask.get_api_key()
    ask.ensure_configured(settings, api_key)  # AskNotConfigured → 400 before any work
    # Also for a base URL edited into the settings file by hand.
    api_profiles.require_https_unless_loopback("base_url", settings.base_url.strip())
    sessions: SessionManager = state.sessions
    opener = state.ask_opener

    def run(cancel_token) -> dict[str, Any]:
        session = sessions.get(name)
        messages, focus = build_messages(
            session, ref, question, selected, editor_sql, tenant_filter, cancel_token
        )
        cancel_token.raise_if_cancelled()
        client = ask.OpenRouterClient(settings, api_key, opener=opener)
        result = client.generate(messages)
        return {
            "sql": result.sql,
            "warnings": ask.validate_generated_sql(result.sql),
            "model": result.model,
            "elapsed_ms": result.elapsed_ms,
            "focus_views": focus,
        }

    return state.jobs.submit("ask", run, profile=name, channel=channel, meta={"ref": ref})


@router.post("", status_code=202)
def ask_job(
    payload: JsonBody, state: State
) -> dict[str, Any]:
    name = _profile_name(state, payload)
    job = start_ask(state, name, payload)
    return {"id": job.id, "status": job.status, "kind": job.kind}
