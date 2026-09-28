"""Per-app server state and the API error type shared by the routers."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Body, Depends, Request

from floe.core import diagnostics
from floe.core.connection_test import StepResult, run_connection_test
from floe.core.history import HistoryStore
from floe.core.profiles import Profile, ProfileStore
from floe.web.jobs import JobManager
from floe.web.prefs import PrefsStore
from floe.web.sessions import SessionManager

ConnectionTester = Callable[[Profile, Mapping[str, str | None]], Iterator[StepResult]]


class ApiError(Exception):
    """An error returned to the browser as `{"error": {"type", "message"}}` with `status`.

    `message` must not contain secrets; it is redacted again before sending anyway.
    """

    def __init__(self, status: int, type_name: str, message: str, **extra: Any) -> None:
        self.status = status
        self.type_name = type_name
        self.message = message
        self.extra = extra
        super().__init__(message)


class ImportCache:
    """Secrets parsed from an imported .env, held in memory only, keyed by a short-lived
    random id. The browser never receives the values: it gets the id and the names of
    the secret fields that were present, and passes the id back when saving (or testing)
    the profile. Entries expire after `ttl` seconds and are consumed by a save."""

    def __init__(self, ttl: float = 600.0, max_entries: int = 16) -> None:
        self._ttl = ttl
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, dict[str, str]]] = {}

    def _expire_locked(self) -> None:
        now = time.monotonic()
        for key in [k for k, (t, _) in self._entries.items() if now - t > self._ttl]:
            del self._entries[key]

    def put(self, values: dict[str, str]) -> str:
        for value in values.values():
            diagnostics.register_secret(value)
        import_id = secrets.token_urlsafe(12)
        with self._lock:
            self._expire_locked()
            while len(self._entries) >= self._max:
                oldest = min(self._entries, key=lambda k: self._entries[k][0])
                del self._entries[oldest]
            self._entries[import_id] = (time.monotonic(), dict(values))
        return import_id

    def get(self, import_id: str) -> dict[str, str] | None:
        with self._lock:
            self._expire_locked()
            entry = self._entries.get(import_id)
            return dict(entry[1]) if entry is not None else None

    def discard(self, import_id: str) -> None:
        with self._lock:
            self._entries.pop(import_id, None)


@dataclass
class WebState:
    store: ProfileStore
    sessions: SessionManager
    jobs: JobManager
    history: HistoryStore
    prefs: PrefsStore
    imports: ImportCache = field(default_factory=ImportCache)
    ask_opener: Any = None
    connection_tester: ConnectionTester = run_connection_test

    def shutdown(self, timeout: float = 5.0) -> bool:
        self.sessions.drop_all()
        return self.jobs.shutdown(timeout)


def get_state(request: Request) -> WebState:
    return request.app.state.floe


State = Annotated[WebState, Depends(get_state)]
JsonBody = Annotated[dict[str, Any], Body()]
OptionalJson = Annotated[dict[str, Any] | None, Body()]
