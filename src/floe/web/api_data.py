"""Catalog, jobs, history, prefs, diagnostics and about endpoints.

Catalog endpoints (branches / tables / head / refresh) are sync FastAPI endpoints, so
they run in the server's worker threads, never on the event loop; the first one for a
profile builds its `FloeSession` (and reads its keyring secrets) there.

Preview / schema / query / test-connection / ask work runs as jobs (`floe.web.jobs`):
`POST /api/jobs` returns an id at once, the browser polls `GET /api/jobs/{id}` and can
`POST /api/jobs/{id}/cancel`. Result rows are served a page at a time from memory.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from floe import __commit__, __version__
from floe.core import diagnostics
from floe.core.context import ColumnInfo, FloeSession, QueryResult, TableInfo
from floe.core.export import ExportNotAllowed
from floe.web import api_ask, api_profiles
from floe.web.jobs import CANCELLED, DONE, ERROR, Job, JobNotFound
from floe.web.prefs import PrefsError
from floe.web.serialize import error_payload, frame_columns, frame_rows
from floe.web.state import ApiError, JsonBody, OptionalJson, State, WebState

log = logging.getLogger("floe.web.data")

router = APIRouter(prefix="/api")

DEFAULT_QUERY_LIMIT = 10_000  # SPEC §8.5
MAX_QUERY_LIMIT = 1_000_000
DEFAULT_PAGE = 500
MAX_PAGE = 5_000
MAX_SQL = 1_000_000
CSV_CHUNK_ROWS = 2_000
DEFAULT_CHANNELS = {
    "preview": "preview",
    "schema": "schema",
    "query": "sql",
    "test_connection": "test_connection",
    "ask": "ask",
}


def _bad(message: str) -> ApiError:
    return ApiError(400, "ValidationError", message)


def _session(state: WebState, name: str) -> FloeSession:
    if state.store.get(name) is None:
        raise ApiError(404, "ProfileNotFound", f"No such profile: {name}")
    return state.sessions.get(name)


def _catalog_status(session: FloeSession | None) -> str:
    if session is None:
        return "unknown"
    return "local" if session.is_local else session.catalog_status


def _ref(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise _bad("ref (branch) is required.")
    return value


def table_payload(info: TableInfo) -> dict[str, Any]:
    return {
        "key": info.key.dotted,
        "elements": list(info.key.elements),
        "layer": info.key.layer,
        "view_name": info.view_name,
        "source_ref": info.source_ref,
        "shared": info.shared,
        "status": info.status,
        "error": diagnostics.redact(info.error) if info.error else None,
        "container": info.container,
    }


# --------------------------------------------------------------------------- catalog


@router.get("/profiles/{name}/branches")
def branches(name: str, state: State) -> dict[str, Any]:
    session = _session(state, name)
    items = session.branches()
    return {
        "branches": [{"name": b.name, "active": b.active} for b in items],
        "is_local": session.is_local,
        "main_ref": None if session.is_local else session.main_ref,
        "catalog_status": _catalog_status(session),
    }


@router.get("/profiles/{name}/tables")
def tables(
    name: str, ref: Annotated[str, Query()], state: State
) -> dict[str, Any]:
    session = _session(state, name)
    ref = _ref(ref)
    items = session.list_tables(ref)
    return {
        "ref": ref,
        "tables": [table_payload(t) for t in items],
        "head": session.current_head(ref),
        "catalog_status": _catalog_status(session),
        "tenant_filter_applicable": session.is_tenant_ref(ref),
    }


@router.get("/profiles/{name}/head")
def head(name: str, ref: Annotated[str, Query()], state: State) -> dict[str, Any]:
    session = _session(state, name)
    ref = _ref(ref)
    return {
        "ref": ref,
        "head": session.current_head(ref),
        "catalog_status": _catalog_status(session),
        "is_local": session.is_local,
        "tenant_filter_applicable": session.is_tenant_ref(ref),
    }


@router.post("/profiles/{name}/refresh")
def refresh(
    state: State,
    name: str,
    payload: OptionalJson = None,
) -> dict[str, Any]:
    """Expire cached heads / listings; with `{"ref": ...}` also return that ref's tables."""
    session = _session(state, name)
    session.refresh()
    ref = (payload or {}).get("ref")
    if ref is None:
        return {"refreshed": True, "catalog_status": _catalog_status(session)}
    return {"refreshed": True, **tables(name, _ref(ref), state)}


@router.post("/profiles/{name}/disconnect")
def disconnect(name: str, state: State) -> dict[str, Any]:
    """Drop the profile's session (cancelling its work); the next call reconnects."""
    state.sessions.drop(name)
    state.jobs.cancel_profile(name)
    return {"disconnected": name}


# --------------------------------------------------------------------------- jobs


def _bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise _bad(f"{key} must be true or false.")
    return value


def _view(payload: dict[str, Any]) -> str:
    view = payload.get("view_name")
    if not isinstance(view, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,300}", view):
        raise _bad("view_name is required (letters, digits and _ only).")
    return view


def _start_job(state: WebState, payload: dict[str, Any]) -> Job:
    kind = payload.get("kind")
    if kind == "export":
        raise _bad(
            "Export runs as a download of a finished preview/query job: "
            "GET /api/jobs/{id}/csv (allowed only when the profile allows export)."
        )
    if kind not in DEFAULT_CHANNELS:
        raise _bad(f"kind must be one of {', '.join(DEFAULT_CHANNELS)}.")
    name = api_profiles.check_name(payload.get("profile"), "profile")
    channel = payload.get("channel", DEFAULT_CHANNELS[kind])
    if channel is not None and (not isinstance(channel, str) or len(channel) > 64):
        raise _bad("channel must be a short string or null.")
    if kind == "test_connection":
        return api_profiles.start_test_connection(
            state, name, {**payload, "channel": channel}, form_key="profile_form"
        )
    if kind == "ask":
        return api_ask.start_ask(state, name, payload, channel)
    profile = state.store.get(name)
    if profile is None:
        raise ApiError(404, "ProfileNotFound", f"No such profile: {name}")
    ref = _ref(payload.get("ref"))
    tenant_filter = _bool(payload, "tenant_filter", True)
    sessions = state.sessions
    meta: dict[str, Any] = {"ref": ref, "tenant_filter": tenant_filter}

    if kind == "preview":
        view = _view(payload)
        meta["view_name"] = view
        meta["row_limit"] = profile.preview_row_limit

        def run_preview(cancel_token) -> QueryResult:
            return sessions.get(name).preview(
                ref, view, tenant_filter=tenant_filter, cancel_token=cancel_token
            )

        return state.jobs.submit(kind, run_preview, profile=name, channel=channel, meta=meta)

    if kind == "schema":
        view = _view(payload)
        meta["view_name"] = view

        def run_schema(cancel_token) -> list[ColumnInfo]:
            return sessions.get(name).schema(
                ref, view, tenant_filter=tenant_filter, cancel_token=cancel_token
            )

        return state.jobs.submit(kind, run_schema, profile=name, channel=channel, meta=meta)

    sql = payload.get("sql")
    if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL:
        raise _bad("sql is required.")
    limit = payload.get("limit", DEFAULT_QUERY_LIMIT)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_QUERY_LIMIT
    ):
        raise _bad(f"limit must be an integer from 1 to {MAX_QUERY_LIMIT:,}.")
    meta["row_limit"] = limit
    history = state.history

    def run_query(cancel_token) -> QueryResult:
        return sessions.get(name).query(
            ref, sql, limit=limit, tenant_filter=tenant_filter, cancel_token=cancel_token
        )

    def record(_job: Job, _result: QueryResult) -> None:
        history.record(name, sql, ref)  # SQL text only, never results (SPEC §9)

    return state.jobs.submit(
        kind, run_query, profile=name, channel=channel, meta=meta, on_success=record
    )


def _result_payload(job: Job, offset: int, limit: int) -> Any:
    result = job.result
    if isinstance(result, QueryResult):
        df = result.df
        return {
            "columns": frame_columns(df),
            "rows": frame_rows(df, offset, limit),
            "offset": offset,
            "limit": limit,
            "total_rows": len(df),
            "truncated": result.truncated,
            "reloaded": result.reloaded,
            "query_elapsed": result.elapsed,
        }
    if job.kind == "schema":
        return {
            "columns": [
                {"name": c.name, "type": c.type, "nullable": c.nullable} for c in result
            ]
        }
    return result


def job_payload(job: Job, offset: int = 0, limit: int = DEFAULT_PAGE) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": job.id,
        "kind": job.kind,
        "profile": job.profile,
        "channel": job.channel,
        "status": job.status,
        "elapsed": round(job.elapsed, 3),
        "cancel_requested": job.cancel_requested,
        **{k: v for k, v in job.meta.items() if k in ("ref", "view_name", "tenant_filter",
                                                        "row_limit")},
    }
    if job.kind == "test_connection":
        out["steps"] = list(job.progress)
    if job.status == DONE:
        out["result"] = _result_payload(job, offset, limit)
    elif job.status == ERROR and job.error is not None:
        out["error"] = error_payload(job.error)
    elif job.status == CANCELLED:
        out["message"] = "Cancelled."
    return out


def _job(state: WebState, job_id: str) -> Job:
    try:
        return state.jobs.get(job_id)
    except JobNotFound:
        raise ApiError(404, "JobNotFound", "No such job (it may have expired).") from None


@router.post("/jobs", status_code=202)
def create_job(
    payload: JsonBody, state: State
) -> dict[str, Any]:
    return job_payload(_start_job(state, payload))


@router.get("/jobs/{job_id}")
def get_job(
    job_id: str,
    state: State,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> dict[str, Any]:
    return job_payload(_job(state, job_id), offset, limit)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, state: State) -> dict[str, Any]:
    _job(state, job_id)
    return job_payload(state.jobs.cancel(job_id), 0, 1)


@router.post("/jobs/cancel-channels")
def cancel_channels(
    state: State,
    payload: OptionalJson = None,
) -> dict[str, Any]:
    """Cancel the unfinished jobs of the given channels (all channels if none given),
    e.g. when the user switches branch or profile (SPEC §8.4)."""
    channels = (payload or {}).get("channels")
    if channels is not None and (
        not isinstance(channels, list) or not all(isinstance(c, str) for c in channels)
    ):
        raise _bad("channels must be a list of strings.")
    cancelled = []
    for job in state.jobs.jobs():
        if job.status in (DONE, ERROR, CANCELLED):
            continue
        if channels is None or job.channel in channels:
            state.jobs.cancel(job.id)
            cancelled.append(job.id)
    return {"cancelled": cancelled}


@router.get("/jobs/{job_id}/csv")
def download_csv(job_id: str, state: State) -> StreamingResponse:
    """The finished preview/query result as CSV — exactly the rows that were shown.

    Allowed only when the job's profile currently has `allow_export` on (SPEC §9); the
    browser shows the row count before offering the link.
    """
    job = _job(state, job_id)
    if job.kind not in ("preview", "query") or job.status != DONE:
        raise ApiError(409, "NotExportable", "Only a finished preview or query can be exported.")
    profile = state.store.get(job.profile or "")
    if profile is None or not profile.allow_export:
        exc = ExportNotAllowed()
        raise ApiError(403, "ExportNotAllowed", exc.user_message())
    df = job.result.df
    log.info("CSV download of %d row(s)", len(df))

    def chunks() -> Iterator[bytes]:
        if len(df) == 0:
            yield df.to_csv(index=False).encode("utf-8")
            return
        for start in range(0, len(df), CSV_CHUNK_ROWS):
            part = df.iloc[start : start + CSV_CHUNK_ROWS]
            yield part.to_csv(index=False, header=start == 0).encode("utf-8")

    stem = job.meta.get("view_name") or "query"
    return StreamingResponse(
        chunks(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{stem}.csv"',
            "X-Row-Count": str(len(df)),
        },
    )


# --------------------------------------------------------------------------- history


@router.get("/profiles/{name}/history")
def get_history(name: str, state: State) -> dict[str, Any]:
    return {
        "entries": [
            {"sql": e.sql, "branch": e.branch, "timestamp": e.timestamp}
            for e in state.history.list(name)
        ]
    }


@router.delete("/profiles/{name}/history")
def clear_history(name: str, state: State) -> dict[str, Any]:
    state.history.clear(name)
    return {"entries": []}


# --------------------------------------------------------------------------- prefs


@router.get("/prefs")
def get_prefs(state: State) -> dict[str, Any]:
    return state.prefs.load()


@router.put("/prefs")
def put_prefs(
    payload: JsonBody, state: State
) -> dict[str, Any]:
    try:
        return state.prefs.update(payload)
    except PrefsError as exc:
        raise _bad(str(exc)) from None


# --------------------------------------------------------------------------- diagnostics


@router.get("/diagnostics")
def get_diagnostics(
    state: State, profile: Annotated[str | None, Query()] = None
) -> dict[str, Any]:
    active = state.store.get(profile) if profile else None
    session = state.sessions.peek(profile) if profile else None
    text = diagnostics.build_diagnostics(
        active, _catalog_status(session), version=__version__, commit=__commit__
    )
    return {"text": diagnostics.redact(text)}


@router.get("/about")
def about() -> dict[str, Any]:
    return {"name": "Floe", "version": __version__, "commit": __commit__}
