"""Timeline jobs for the web app (SPEC §15.7): freshness, branch history, table history.

All three run as background jobs (`POST /api/jobs` with `kind` = `freshness`, `history`
or `table_history`) with the usual cancel + channel semantics, and return JSON-safe
dicts. Responses carry only table keys, view names, refs, commit hashes, messages and
metadata numbers — never file paths, container or account names. Times are ISO-8601 UTC
strings plus epoch milliseconds (`<field>_ms`). Snapshot ids are strings (64-bit).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from floe.core import diagnostics, timeline
from floe.core.context import CancelToken
from floe.core.nessie import CommitInfo, TableKey
from floe.web.jobs import Job
from floe.web.serialize import error_payload
from floe.web.state import ApiError, WebState

KINDS = ("freshness", "history", "table_history")
CHANNELS = {"freshness": "freshness", "history": "timeline", "table_history": "table_history"}
MAX_KEY_ELEMENTS = 20
MAX_ELEMENT_LEN = 300
MAX_PAGE_TOKEN = 2000
MAX_HISTORY_RECORDS = 200


def _bad(message: str) -> ApiError:
    return ApiError(400, "ValidationError", message)


def _time(value: datetime | None) -> tuple[str | None, int | None]:
    if value is None:
        return None, None
    return value.isoformat().replace("+00:00", "Z"), int(round(value.timestamp() * 1000))


def _times(out: dict[str, Any], name: str, value: datetime | None) -> None:
    iso, ms = _time(value)
    out[name] = iso
    out[f"{name}_ms"] = ms


def _key_payload(key: TableKey) -> dict[str, Any]:
    return {"key": key.dotted, "elements": list(key.elements), "view_name": key.view_name}


def parse_key(value: object) -> TableKey:
    """A table key from a list of elements (preferred) or a dotted string."""
    if isinstance(value, str):
        elements = value.split(".")
    elif isinstance(value, list):
        elements = value
    else:
        raise _bad("key is required (a list of key elements).")
    if not 1 <= len(elements) <= MAX_KEY_ELEMENTS or not all(
        isinstance(e, str)
        and 0 < len(e) <= MAX_ELEMENT_LEN
        and not any(ord(ch) < 32 for ch in e)
        for e in elements
    ):
        raise _bad("key must be 1-20 non-empty elements without control characters.")
    return TableKey(tuple(elements))


# --------------------------------------------------------------------------- payloads


def freshness_payload(ref: str, rows: list[timeline.FreshnessRow]) -> dict[str, Any]:
    out_rows = []
    for row in rows:
        item: dict[str, Any] = {
            **_key_payload(row.key),
            "layer": row.key.layer,
            "source_ref": row.source_ref,
            "shared": row.shared,
            "status": row.status,
            "error": diagnostics.redact(row.error) if row.error else None,
            "operation": row.operation,
            "added_records": row.added_records,
            "total_records": row.total_records,
            "snapshot_count": row.snapshot_count,
            "last_commit_message": row.last_commit_message,
        }
        _times(item, "last_write_time", row.last_write_time)
        _times(item, "last_published_time", row.last_published_time)
        out_rows.append(item)
    return {"ref": ref, "rows": out_rows}


def commit_payload(commit: CommitInfo) -> dict[str, Any]:
    item: dict[str, Any] = {
        "hash": commit.hash,
        "short_hash": commit.hash[:10],
        "author": commit.author,
        "committer": commit.committer,
        "message": commit.message,
        "touched": None
        if commit.touched is None
        else [_key_payload(k) for k in commit.touched],
    }
    _times(item, "time", commit.time)
    return item


def history_payload(ref: str, page: Any) -> dict[str, Any]:
    return {
        "ref": ref,
        "entries": [commit_payload(c) for c in page.entries],
        "next_token": page.next_token,
        "operations_available": page.operations_available,
    }


def table_history_payload(ref: str, result: timeline.TableSnapshots) -> dict[str, Any]:
    info = result.info
    history = result.history
    snapshots = []
    for snap in reversed(history.snapshots):  # newest first
        item: dict[str, Any] = {
            "snapshot_id": str(snap.snapshot_id),
            "parent_id": None if snap.parent_id is None else str(snap.parent_id),
            "operation": snap.operation,
            "added_records": snap.added_records,
            "deleted_records": snap.deleted_records,
            "added_files": snap.added_files,
            "removed_files": snap.removed_files,
            "total_records": snap.total_records,
            "current": snap.snapshot_id == history.current_snapshot_id,
        }
        _times(item, "time", snap.time)
        snapshots.append(item)
    return {
        "ref": ref,
        **_key_payload(info.key),
        "source_ref": info.source_ref,
        "shared": info.shared,
        "current_snapshot_id": None
        if history.current_snapshot_id is None
        else str(history.current_snapshot_id),
        "snapshots": snapshots,
    }


def error_for(job: Job) -> dict[str, Any]:
    """The job's error payload, with Timeline-safe text (no container names)."""
    payload = error_payload(job.error)
    payload["message"] = timeline.timeline_error_message(job.error)
    return payload


# --------------------------------------------------------------------------- jobs


def start_job(
    state: WebState, name: str, kind: str, ref: str, payload: dict[str, Any], channel: str | None
) -> Job:
    sessions = state.sessions
    meta: dict[str, Any] = {"ref": ref}

    if kind == "freshness":

        def run_freshness(cancel_token: CancelToken) -> dict[str, Any]:
            rows = timeline.freshness(sessions.get(name), ref, cancel_token=cancel_token)
            return freshness_payload(ref, rows)

        return state.jobs.submit(kind, run_freshness, profile=name, channel=channel, meta=meta)

    if kind == "history":
        page_token = payload.get("page_token")
        if page_token is not None and (
            not isinstance(page_token, str) or not 0 < len(page_token) <= MAX_PAGE_TOKEN
        ):
            raise _bad("page_token must be a string from a previous page.")
        max_records = payload.get("max_records", timeline.PUBLISH_SCAN_PAGE_SIZE)
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or not 1 <= max_records <= MAX_HISTORY_RECORDS
        ):
            raise _bad(f"max_records must be an integer from 1 to {MAX_HISTORY_RECORDS}.")
        meta["page_token"] = page_token

        def run_history(cancel_token: CancelToken) -> dict[str, Any]:
            cancel_token.raise_if_cancelled()
            page = timeline.branch_history(
                sessions.get(name), ref, page_token=page_token, max_records=max_records
            )
            cancel_token.raise_if_cancelled()
            return history_payload(ref, page)

        return state.jobs.submit(kind, run_history, profile=name, channel=channel, meta=meta)

    key = parse_key(payload.get("key"))
    meta["key"] = key.dotted

    def run_table_history(cancel_token: CancelToken) -> dict[str, Any]:
        result = timeline.table_snapshots(
            sessions.get(name), ref, key, cancel_token=cancel_token
        )
        return table_history_payload(ref, result)

    return state.jobs.submit(kind, run_table_history, profile=name, channel=channel, meta=meta)
