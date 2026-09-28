"""Background jobs for the web app: a thread pool with per-channel cancellation.

Mirrors `floe.ui.workers.TaskRunner` server-side, without Qt:

- Every job gets its own `CancelToken`; `cancel()` sets the job's cancelled flag and
  cancels the token, which interrupts a running DuckDB statement (SPEC §15.1).
- A job may belong to a *channel* (e.g. "preview", "schema", "sql"). Submitting a job to
  a channel cancels the channel's unfinished jobs first, so a preview for a new branch or
  table supersedes the old one — and a cancelled job's result is dropped even if the work
  itself finished.
- Results live only in memory (never on disk, SPEC §9). At most `keep_per_profile`
  finished jobs are kept per profile, at most `max_finished` overall, and the finished
  results together hold at most `max_retained_rows` rows; the oldest finished jobs are
  dropped first (the newest finished job is always kept).
- At most `max_unfinished` jobs may be queued or running at once; `submit` raises
  `TooManyJobs` beyond that (→ HTTP 429).
"""

from __future__ import annotations

import inspect
import itertools
import logging
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from floe.core import diagnostics
from floe.core.context import CancelToken
from floe.core.errors import FloeError, QueryCancelled

log = logging.getLogger("floe.web.jobs")

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"
FINISHED = frozenset({DONE, ERROR, CANCELLED})


@dataclass
class Job:
    id: str
    kind: str
    profile: str | None
    channel: str | None
    meta: dict[str, Any] = field(default_factory=dict)
    status: str = QUEUED
    created: float = field(default_factory=time.monotonic)
    started: float | None = None
    finished: float | None = None
    result: Any = None
    error: BaseException | None = None
    progress: list[Any] = field(default_factory=list)
    token: CancelToken = field(default_factory=CancelToken)
    seq: int = 0
    _cancel_requested: threading.Event = field(default_factory=threading.Event)

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    def cancel(self) -> None:
        self._cancel_requested.set()
        self.token.cancel()

    def is_cancelled(self) -> bool:
        return self._cancel_requested.is_set()

    @property
    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        end = self.finished if self.finished is not None else time.monotonic()
        return max(0.0, end - self.started)


class JobNotFound(KeyError):
    pass


class TooManyJobs(RuntimeError):
    """Too many unfinished jobs; the client should wait for (or cancel) some."""


def result_rows(result: Any) -> int:
    """How many rows a job result holds in memory (for the retention budget)."""
    df = getattr(result, "df", None)
    if df is not None:
        try:
            return len(df)
        except TypeError:
            return 0
    if isinstance(result, list | tuple):
        return len(result)
    return 0


class JobManager:
    """Runs job functions in a thread pool; see the module docstring."""

    def __init__(
        self,
        max_workers: int = 4,
        keep_per_profile: int = 20,
        *,
        max_unfinished: int = 16,
        max_finished: int = 200,
        max_retained_rows: int = 2_000_000,
    ) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="floe-job")
        self._keep = keep_per_profile
        self._max_unfinished = max_unfinished
        self._max_finished = max_finished
        self._max_rows = max_retained_rows
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._seq = itertools.count(1)
        self._closed = False

    # ----- submission --------------------------------------------------------
    def submit(
        self,
        kind: str,
        fn: Callable[..., Any],
        *,
        profile: str | None = None,
        channel: str | None = None,
        meta: dict[str, Any] | None = None,
        on_success: Callable[[Job, Any], None] | None = None,
    ) -> Job:
        """Run `fn(...)` in the pool. `fn` may accept `cancel_token`, `progress` (a
        one-arg callable appending to `job.progress`) and/or `is_cancelled` keyword
        arguments; only the ones its signature declares are passed.

        `on_success(job, result)` runs in the worker after a successful, not-cancelled
        run (e.g. to record history)."""
        job = Job(
            id=secrets.token_hex(8),
            kind=kind,
            profile=profile,
            channel=channel,
            meta=dict(meta or {}),
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("JobManager is shut down")
            superseded = [
                j
                for j in self._jobs.values()
                if channel is not None and j.channel == channel and j.status not in FINISHED
            ]
            # Superseded jobs are about to be cancelled, so they don't count; but a
            # cancelled job holds its worker until DuckDB notices, so every unfinished
            # job counts towards a hard cap of twice the limit.
            unfinished = [j for j in self._jobs.values() if j.status not in FINISHED]
            active = [
                j for j in unfinished if not j.cancel_requested and j not in superseded
            ]
            if len(active) >= self._max_unfinished or len(unfinished) >= 2 * self._max_unfinished:
                raise TooManyJobs(
                    f"Too many jobs are running ({len(unfinished)}). Wait for some to "
                    "finish or cancel them, then try again."
                )
            job.seq = next(self._seq)
            self._jobs[job.id] = job
        for old in superseded:
            log.debug("Cancelling superseded %s job in channel %s", old.kind, channel)
            self.cancel(old.id)
        self._pool.submit(self._run, job, fn, on_success)
        return job

    def _run(
        self,
        job: Job,
        fn: Callable[..., Any],
        on_success: Callable[[Job, Any], None] | None,
    ) -> None:
        with self._lock:
            if job.is_cancelled() or self._closed:
                self._finish_locked(job, CANCELLED)
                return
            job.status = RUNNING
            job.started = time.monotonic()
        kwargs: dict[str, Any] = {}
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        if "cancel_token" in params:
            kwargs["cancel_token"] = job.token
        if "progress" in params:
            kwargs["progress"] = job.progress.append
        if "is_cancelled" in params:
            kwargs["is_cancelled"] = job.is_cancelled
        try:
            result = fn(**kwargs)
        except QueryCancelled:
            with self._lock:
                self._finish_locked(job, CANCELLED)
            return
        except Exception as exc:  # noqa: BLE001 - reported through the job, never re-raised
            if not isinstance(exc, FloeError):
                log.error(
                    "%s job failed: %s",
                    job.kind,
                    diagnostics.redact(f"{type(exc).__name__}: {exc}"),
                )
            with self._lock:
                if job.is_cancelled():
                    self._finish_locked(job, CANCELLED)
                else:
                    job.error = exc
                    self._finish_locked(job, ERROR)
            return
        if job.is_cancelled():
            with self._lock:
                self._finish_locked(job, CANCELLED)  # stale: drop the result
            return
        if on_success is not None:
            try:
                on_success(job, result)
            except Exception as exc:  # noqa: BLE001 - a side effect must not fail the job
                log.warning("%s job post-processing failed (%s)", job.kind, type(exc).__name__)
        with self._lock:
            if job.is_cancelled():
                self._finish_locked(job, CANCELLED)
            else:
                job.result = result
                self._finish_locked(job, DONE)

    def _finish_locked(self, job: Job, status: str) -> None:
        job.status = status
        job.finished = time.monotonic()
        if job.started is None:
            job.started = job.finished
        if status != DONE:
            job.result = None
        self._prune_locked(job.profile)

    def _prune_locked(self, profile: str | None) -> None:
        finished = sorted(
            (j for j in self._jobs.values() if j.profile == profile and j.status in FINISHED),
            key=lambda j: j.seq,
        )
        for old in finished[: max(0, len(finished) - self._keep)]:
            self._jobs.pop(old.id, None)
        # Global limits: job count and retained result rows, oldest finished job first.
        finished = sorted(
            (j for j in self._jobs.values() if j.status in FINISHED),
            key=lambda j: j.finished or 0.0,
        )
        rows = sum(result_rows(j.result) for j in finished)
        while len(finished) > 1 and (
            len(finished) > self._max_finished or rows > self._max_rows
        ):
            old = finished.pop(0)
            rows -= result_rows(old.result)
            self._jobs.pop(old.id, None)

    # ----- access ------------------------------------------------------------
    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job

    def jobs(self, profile: str | None = None) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if profile is None or j.profile == profile]

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        job.cancel()
        with self._lock:
            if job.status == QUEUED:
                self._finish_locked(job, CANCELLED)
        return job

    def cancel_profile(self, profile: str) -> None:
        """Cancel every unfinished job of `profile` (it was edited, renamed or deleted)."""
        for job in self.jobs(profile):
            if job.status not in FINISHED:
                self.cancel(job.id)

    def forget_profile(self, profile: str) -> None:
        """Cancel and drop every job (and so every in-memory result) of `profile`."""
        self.cancel_profile(profile)
        with self._lock:
            for job_id in [j.id for j in self._jobs.values() if j.profile == profile]:
                if self._jobs[job_id].status in FINISHED:
                    self._jobs.pop(job_id, None)

    def wait(self, job_id: str, timeout: float) -> Job:
        """Poll until the job finishes or `timeout` elapses (tests, CLI)."""
        deadline = time.monotonic() + timeout
        job = self.get(job_id)
        while job.status not in FINISHED and time.monotonic() < deadline:
            time.sleep(0.01)
        return job

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Cancel everything, then wait (bounded) for the worker threads to finish."""
        with self._lock:
            self._closed = True
            pending = [j for j in self._jobs.values() if j.status not in FINISHED]
        for job in pending:
            job.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)
        deadline = time.monotonic() + timeout
        threads = list(getattr(self._pool, "_threads", ()))
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        done = not any(t.is_alive() for t in threads)
        if not done:
            log.warning("Background jobs still running after %.1f s at shutdown", timeout)
        return done
