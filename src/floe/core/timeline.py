"""Timeline: data freshness, Iceberg snapshot history and the branch commit log (SPEC §15.7).

Metadata only — no row data is ever read:

- `table_snapshots(session, ref, key)` resolves the table exactly like view registration
  (catalog entry, shared namespaces from main, Nessie pointer, container check) and reads
  **only** its current `metadata.json` (see `FloeSession.with_table_metadata`), then parses
  `snapshots[]`, `current-snapshot-id` and each snapshot's `summary`. Parsed results are
  cached by metadata location, which is immutable.
- `freshness(session, ref)` builds one row per table on the branch (the tree's scoping)
  from each table's current snapshot, in a small bounded thread pool; a table that fails
  (not found / corrupt / out of scope / unreadable) gets a status instead of failing the
  list. "Last published" comes from the newest commit whose operations touched the table,
  when the Nessie server returns operations (a bounded scan of the commit log).
- `branch_history(session, ref, ...)` is one page of the ref's Nessie commit log.

No Qt here. Nothing here logs table contents, commit messages or file locations.
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from floe.core import diagnostics
from floe.core.context import CancelToken, FloeSession, TableInfo, _status_for
from floe.core.errors import (
    FloeError,
    MetadataReadError,
    QueryCancelled,
    TenantScopeError,
    TimelineUnavailable,
)
from floe.core.nessie import HISTORY_PAGE_SIZE, CommitInfo, HistoryPage, TableKey

log = logging.getLogger("floe.timeline")

FreshnessStatus = Literal["ok", "not_found", "corrupt", "scope_error", "error"]

FRESHNESS_WORKERS = 6
PUBLISH_SCAN_PAGES = 5
PUBLISH_SCAN_PAGE_SIZE = HISTORY_PAGE_SIZE
METADATA_CACHE_SIZE = 512
SCOPE_ERROR_MESSAGE = "Points outside its expected container; Floe refuses to read it."


# --------------------------------------------------------------------------- data types


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: int
    parent_id: int | None
    time: datetime | None  # snapshot `timestamp-ms`, UTC
    operation: str | None  # append / overwrite / delete / replace
    added_records: int | None
    deleted_records: int | None
    added_files: int | None
    removed_files: int | None
    total_records: int | None


@dataclass(frozen=True)
class TableHistory:
    current_snapshot_id: int | None
    snapshots: tuple[SnapshotInfo, ...]  # oldest first

    @property
    def current(self) -> SnapshotInfo | None:
        if self.current_snapshot_id is not None:
            for snap in self.snapshots:
                if snap.snapshot_id == self.current_snapshot_id:
                    return snap
            return None
        return None


@dataclass(frozen=True)
class TableSnapshots:
    """`table_snapshots` result: where the table resolved from, plus its history."""

    info: TableInfo
    history: TableHistory


@dataclass(frozen=True)
class FreshnessRow:
    key: TableKey
    view_name: str
    source_ref: str
    shared: bool
    status: FreshnessStatus
    error: str | None
    last_write_time: datetime | None
    operation: str | None
    added_records: int | None
    total_records: int | None
    last_published_time: datetime | None
    last_commit_message: str | None
    snapshot_count: int | None = None


@dataclass(frozen=True)
class Published:
    time: datetime | None
    message: str
    hash: str


# --------------------------------------------------------------------------- parsing


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _time_ms(value: Any) -> datetime | None:
    ms = _int(value)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def decode_metadata(raw: bytes) -> dict[str, Any]:
    """Parse metadata.json bytes (gzip-compressed `*.gz.metadata.json` too)."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("metadata.json is not an object")
    return data


def parse_table_history(metadata: dict[str, Any]) -> TableHistory:
    """`snapshots[]` + `current-snapshot-id` of an Iceberg table-metadata document."""
    snapshots: list[SnapshotInfo] = []
    for raw in metadata.get("snapshots") or []:
        if not isinstance(raw, dict):
            continue
        snapshot_id = _int(raw.get("snapshot-id"))
        if snapshot_id is None:
            continue
        summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
        operation = summary.get("operation") or raw.get("operation")
        snapshots.append(
            SnapshotInfo(
                snapshot_id=snapshot_id,
                parent_id=_int(raw.get("parent-snapshot-id")),
                time=_time_ms(raw.get("timestamp-ms")),
                operation=str(operation) if operation else None,
                added_records=_int(summary.get("added-records")),
                deleted_records=_int(summary.get("deleted-records")),
                added_files=_int(summary.get("added-data-files")),
                removed_files=_int(summary.get("deleted-data-files")),
                total_records=_int(summary.get("total-records")),
            )
        )
    snapshots.sort(key=lambda s: (s.time or datetime.min.replace(tzinfo=UTC), s.snapshot_id))
    current = _int(metadata.get("current-snapshot-id"))
    if current is not None and current < 0:
        current = None
    return TableHistory(current_snapshot_id=current, snapshots=tuple(snapshots))


# --------------------------------------------------------------------------- snapshots


class _MetadataCache:
    """Thread-safe LRU of parsed histories keyed by metadata location (immutable files)."""

    def __init__(self, max_size: int = METADATA_CACHE_SIZE) -> None:
        self.max_size = max_size
        self._lock = threading.Lock()
        self._items: OrderedDict[str, TableHistory] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, location: str) -> TableHistory | None:
        with self._lock:
            item = self._items.get(location)
            if item is not None:
                self._items.move_to_end(location)
                self.hits += 1
            else:
                self.misses += 1
            return item

    def put(self, location: str, history: TableHistory) -> None:
        with self._lock:
            self._items[location] = history
            self._items.move_to_end(location)
            while len(self._items) > self.max_size:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.hits = self.misses = 0


METADATA_CACHE = _MetadataCache()


def table_snapshots(
    session: FloeSession,
    ref: str,
    key: TableKey | str,
    *,
    cancel_token: CancelToken | None = None,
) -> TableSnapshots:
    """The Iceberg snapshot history of `key` as seen from `ref` (metadata.json only)."""
    table_key = TableKey.parse(key) if isinstance(key, str) else key

    def load(location: str, read: Callable[[], bytes]) -> TableHistory:
        cached = METADATA_CACHE.get(location)
        if cached is not None:
            return cached
        raw = read()
        try:
            history = parse_table_history(decode_metadata(raw))
        except (ValueError, UnicodeDecodeError, OSError, EOFError) as exc:
            reason = f"invalid JSON ({type(exc).__name__})"
            raise MetadataReadError(table_key.dotted, reason) from None
        METADATA_CACHE.put(location, history)
        return history

    info, history = session.with_table_metadata(ref, table_key, load, cancel_token=cancel_token)
    return TableSnapshots(info=info, history=history)


def timeline_error_message(exc: BaseException) -> str:
    """User-facing text for a Timeline failure: redacted, and without container names."""
    if isinstance(exc, TenantScopeError):
        return SCOPE_ERROR_MESSAGE
    if isinstance(exc, FloeError):
        return diagnostics.redact(exc.user_message())
    return f"Unexpected error ({type(exc).__name__})."


# --------------------------------------------------------------------------- commit log


def branch_history(
    session: FloeSession,
    ref: str,
    *,
    page_token: str | None = None,
    max_records: int = HISTORY_PAGE_SIZE,
) -> HistoryPage:
    """One page of `ref`'s Nessie commit log, newest first (operations when available)."""
    if session.is_local or session.nessie is None:
        raise TimelineUnavailable()
    return session.nessie.history(ref, page_token=page_token, max_records=max_records)


def last_published(
    session: FloeSession,
    ref: str,
    keys: set[TableKey],
    *,
    pages: int = PUBLISH_SCAN_PAGES,
    page_size: int = PUBLISH_SCAN_PAGE_SIZE,
    token: CancelToken | None = None,
) -> dict[TableKey, Published] | None:
    """The newest commit on `ref` touching each of `keys` (bounded scan of the log).

    Returns None when the server doesn't return commit operations (unknown), else a
    mapping for the keys found within `pages` pages. Failures return None (the freshness
    list must not fail because of the commit log).
    """
    if session.nessie is None:
        return None
    if not keys:
        return {}
    found: dict[TableKey, Published] = {}
    page_token: str | None = None
    try:
        for _ in range(max(1, pages)):
            if token is not None:
                token.raise_if_cancelled()
            page = session.nessie.history(ref, page_token=page_token, max_records=page_size)
            if not page.operations_available:
                return None
            for commit in page.entries:
                for key in commit.touched or ():
                    if key in keys and key not in found:
                        found[key] = Published(commit.time, commit.message, commit.hash)
            if len(found) == len(keys) or page.next_token is None:
                break
            page_token = page.next_token
    except QueryCancelled:
        raise
    except FloeError as exc:
        log.warning("Commit log of %s unavailable for freshness (%s)", ref, type(exc).__name__)
        return None
    return found


# --------------------------------------------------------------------------- freshness


def _row_for(
    session: FloeSession, ref: str, info: TableInfo, token: CancelToken
) -> FreshnessRow:
    base = dict(
        key=info.key,
        view_name=info.view_name,
        source_ref=info.source_ref,
        shared=info.shared,
        last_published_time=None,
        last_commit_message=None,
    )
    empty = dict(
        last_write_time=None, operation=None, added_records=None, total_records=None
    )
    try:
        result = table_snapshots(session, ref, info.key, cancel_token=token)
    except QueryCancelled:
        raise
    except FloeError as exc:
        status = _status_for(exc)
        if status == "missing_data_source":  # can't happen here; be safe
            status = "error"
        return FreshnessRow(
            **base, **empty, status=status, error=timeline_error_message(exc)
        )
    except Exception as exc:  # noqa: BLE001 - one table must not fail the whole list
        log.warning("Freshness of %s failed (%s)", info.dotted, type(exc).__name__)
        return FreshnessRow(
            **base, **empty, status="error", error=timeline_error_message(exc)
        )
    history = result.history
    current = history.current  # None for an empty table (no current snapshot)
    return FreshnessRow(
        **base,
        status="ok",
        error=None,
        last_write_time=current.time if current else None,
        operation=current.operation if current else None,
        added_records=current.added_records if current else None,
        total_records=current.total_records if current else None,
        snapshot_count=len(history.snapshots),
    )


def freshness(
    session: FloeSession,
    ref: str,
    *,
    cancel_token: CancelToken | None = None,
    max_workers: int = FRESHNESS_WORKERS,
    publish_pages: int = PUBLISH_SCAN_PAGES,
) -> list[FreshnessRow]:
    """One freshness row per table visible on `ref` (tenant tables + shared from main)."""
    if session.is_local or session.nessie is None:
        raise TimelineUnavailable()
    token = cancel_token if cancel_token is not None else CancelToken()
    infos = session.list_tables(ref)
    token.raise_if_cancelled()
    rows: dict[TableKey, FreshnessRow] = {}
    if infos:
        pool = ThreadPoolExecutor(
            max_workers=max(1, min(max_workers, len(infos))), thread_name_prefix="floe-fresh"
        )
        try:
            pending: set[Future[FreshnessRow]] = {
                pool.submit(_row_for, session, ref, info, token) for info in infos
            }
            while pending:
                done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                if token.cancelled:
                    raise QueryCancelled()
                for fut in done:
                    row = fut.result()
                    rows[row.key] = row
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # "Last published": newest commit touching each table, on the ref it resolves from.
    by_source: dict[str, set[TableKey]] = {}
    for info in infos:
        by_source.setdefault(info.source_ref, set()).add(info.key)
    for source_ref, keys in by_source.items():
        published = last_published(session, source_ref, keys, pages=publish_pages, token=token)
        if not published:
            continue
        for key, pub in published.items():
            row = rows.get(key)
            if row is not None:
                rows[key] = replace(
                    row, last_published_time=pub.time, last_commit_message=pub.message
                )
    token.raise_if_cancelled()
    out = [rows[i.key] for i in infos if i.key in rows]
    log.info(
        "Freshness of %s: %d table(s), %d with errors",
        ref,
        len(out),
        sum(1 for r in out if r.status != "ok"),
    )
    return out


__all__ = [
    "CommitInfo",
    "FreshnessRow",
    "HistoryPage",
    "METADATA_CACHE",
    "SnapshotInfo",
    "TableHistory",
    "TableSnapshots",
    "branch_history",
    "decode_metadata",
    "freshness",
    "last_published",
    "parse_table_history",
    "table_snapshots",
    "timeline_error_message",
]
