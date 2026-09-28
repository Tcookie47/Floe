"""QRunnable-based query/IO workers with cancel.

Generic on purpose: Phase 4 (browser) and Phase 5 (SQL editor) reuse `Worker`
for catalog listing, preview, schema and query execution. This module is Qt
UI plumbing, not `floe.core` — it belongs on this side of the layering rule.
"""

from __future__ import annotations

import inspect
import itertools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from floe.core import diagnostics
from floe.core.context import CancelToken
from floe.core.errors import FloeError

log = logging.getLogger("floe.ui.workers")

_generation_counter = itertools.count(1)
_generation_lock = threading.Lock()


def next_generation() -> int:
    """A process-wide, monotonically increasing generation id.

    Callers stamp work with a generation before submitting it, then drop
    results whose generation is stale (e.g. the user changed branch or
    profile while a worker was still running).
    """
    with _generation_lock:
        return next(_generation_counter)


class WorkerSignals(QObject):
    """Signals emitted by a `Worker`. Owned separately since `QRunnable` isn't a `QObject`."""

    result = Signal(object)
    error = Signal(Exception)
    progress = Signal(object)
    finished = Signal()
    # (task_id, ok, payload): one emission per run, after result/error. Used by
    # `TaskRunner`, which connects it to a slot of a main-thread QObject (queued).
    completed = Signal(object)


class Worker(QRunnable):
    """Runs `fn(*args, is_cancelled=..., progress=..., cancel_token=..., **kwargs)` in a
    `QThreadPool` thread.

    `fn` may accept `is_cancelled` (a zero-arg callable returning bool), `progress` (a
    one-arg callable emitting `WorkerSignals.progress`) and/or `cancel_token` (the worker's
    `floe.core.context.CancelToken`, to pass on to `FloeSession` calls) as keyword
    arguments; only the ones its signature declares are passed. `cancel()` sets the
    cancelled flag *and* cancels the token, which interrupts a running DuckDB statement.
    """

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        generation: int | None = None,
        cancel_token: CancelToken | None = None,
        task_id: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.generation = generation if generation is not None else next_generation()
        self.cancel_token = cancel_token if cancel_token is not None else CancelToken()
        self.task_id = task_id
        self.signals = WorkerSignals()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """Request cancellation: cooperative flag + the `CancelToken` (interrupts DuckDB)."""
        self._cancelled.set()
        self.cancel_token.cancel()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def run(self) -> None:  # noqa: D102 - QRunnable override
        kwargs = dict(self.kwargs)
        try:
            params = inspect.signature(self.fn).parameters
            if "is_cancelled" in params:
                kwargs["is_cancelled"] = self.is_cancelled
            if "progress" in params:
                kwargs["progress"] = self.signals.progress.emit
            if "cancel_token" in params:
                kwargs["cancel_token"] = self.cancel_token
        except (TypeError, ValueError):
            pass

        ok = False
        payload: Any = None
        try:
            payload = self.fn(*self.args, **kwargs)
            ok = True
        except Exception as exc:  # noqa: BLE001 - reported through the signal, not re-raised
            payload = exc
            self.signals.error.emit(exc)
        else:
            self.signals.result.emit(payload)
        finally:
            self.signals.finished.emit()
            self.signals.completed.emit((self.task_id, ok, payload))


def run_in_background(
    fn_or_worker: Callable[..., Any] | Worker, *args: Any, **kwargs: Any
) -> Worker:
    """Submit work to the global thread pool.

    Pass a plain callable (and its args/kwargs) to have a `Worker` built for it, or
    pass an already-constructed `Worker` (e.g. one whose signals you connected first).
    """
    if isinstance(fn_or_worker, Worker):
        worker = fn_or_worker
    else:
        worker = Worker(fn_or_worker, *args, **kwargs)
    QThreadPool.globalInstance().start(worker)
    return worker


# --------------------------------------------------------------------------- error text


def user_error_text(exc: BaseException) -> str:
    """Redacted, user-facing text for an error raised by a worker (SPEC §7, §10)."""
    if isinstance(exc, FloeError):
        text = exc.user_message()
    else:
        text = f"Unexpected error ({type(exc).__name__}): {exc}"
    return diagnostics.redact(text)


# --------------------------------------------------------------------------- task runner


@dataclass
class _Task:
    task_id: int
    channel: str
    generation: int
    worker: Worker
    on_result: Callable[[Any], None] | None
    on_error: Callable[[BaseException], None] | None
    on_finished: Callable[[], None] | None


class TaskRunner(QObject):
    """Runs UI background work on its own `QThreadPool`, with per-channel generations.

    Each task belongs to a *channel* (e.g. "branches", "tables", "preview"). Every
    channel has a current generation; `invalidate(channel)` bumps it and cancels that
    channel's in-flight tasks (their `CancelToken`s interrupt DuckDB). A task's callbacks
    run on the UI thread, and only if its generation is still current when it finishes, so
    a result for an old profile / branch / table selection is dropped even when the work
    itself could not be cancelled. `submit(..., replace=True)` (the default) invalidates
    the channel first, so a new preview supersedes the previous one.

    `shutdown()` cancels everything and waits (bounded) for the pool.
    """

    def __init__(self, parent: QObject | None = None, max_threads: int = 4) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_threads)
        self._ids = itertools.count(1)
        self._generations: dict[str, int] = {}
        self._tasks: dict[int, _Task] = {}
        self._closed = False

    @property
    def pool(self) -> QThreadPool:
        return self._pool

    def generation(self, channel: str) -> int:
        gen = self._generations.get(channel)
        if gen is None:
            gen = self._generations[channel] = next_generation()
        return gen

    def is_current(self, channel: str, generation: int) -> bool:
        return self._generations.get(channel) == generation

    def invalidate(self, *channels: str) -> None:
        """Bump the generation of `channels` (all channels if none) and cancel their tasks."""
        targets = set(channels) if channels else set(self._generations) | {
            t.channel for t in self._tasks.values()
        }
        for channel in targets:
            self._generations[channel] = next_generation()
        for task in list(self._tasks.values()):
            if task.channel in targets:
                task.worker.cancel()

    def active(self, channel: str | None = None) -> list[Worker]:
        """In-flight workers (all channels if None), including already-stale ones."""
        return [
            t.worker for t in self._tasks.values() if channel is None or t.channel == channel
        ]

    def submit(
        self,
        channel: str,
        fn: Callable[..., Any],
        *args: Any,
        on_result: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_finished: Callable[[], None] | None = None,
        replace: bool = True,
        **kwargs: Any,
    ) -> Worker:
        """Run `fn(*args, **kwargs)` in the pool (see `Worker` for injected kwargs).

        `on_finished` runs (on the UI thread) after `on_result` / `on_error` of a current
        task only; stale tasks run no callback at all.
        """
        if self._closed:
            raise RuntimeError("TaskRunner is shut down")
        if replace:
            self.invalidate(channel)
        task_id = next(self._ids)
        generation = self.generation(channel)
        worker = Worker(fn, *args, generation=generation, task_id=task_id, **kwargs)
        worker.setAutoDelete(False)  # lifetime managed from Python (kept in `_tasks`)
        worker.signals.completed.connect(self._on_completed)
        self._tasks[task_id] = _Task(
            task_id, channel, generation, worker, on_result, on_error, on_finished
        )
        self._pool.start(worker)
        return worker

    @Slot(object)
    def _on_completed(self, outcome: tuple[int, bool, Any]) -> None:
        task_id, ok, payload = outcome
        task = self._tasks.pop(task_id, None)
        if task is None or self._closed:
            return
        if not self.is_current(task.channel, task.generation) or task.worker.is_cancelled():
            log.debug("Dropping stale %s result (generation %d)", task.channel, task.generation)
            return
        try:
            if ok:
                if task.on_result is not None:
                    task.on_result(payload)
            else:
                if not isinstance(payload, FloeError):
                    log.error(
                        "%s task failed: %s",
                        task.channel,
                        diagnostics.redact(f"{type(payload).__name__}: {payload}"),
                    )
                if task.on_error is not None:
                    task.on_error(payload)
        finally:
            if task.on_finished is not None:
                task.on_finished()

    def shutdown(self, timeout_ms: int = 3000) -> bool:
        """Cancel all tasks, drop queued ones, and wait up to `timeout_ms` for the pool."""
        self.invalidate()
        self._closed = True
        self._pool.clear()
        done = self._pool.waitForDone(timeout_ms)
        if not done:
            log.warning("Background work still running after %d ms at shutdown", timeout_ms)
        return done
