"""JobManager unit tests: channels, stale-result dropping, retention, shutdown."""

from __future__ import annotations

import threading

import pytest

from floe.core.errors import QueryCancelled
from floe.web.jobs import JobManager, TooManyJobs


@pytest.fixture
def jobs():
    manager = JobManager(max_workers=2, keep_per_profile=2)
    yield manager
    manager.shutdown(timeout=5)


def test_result_of_a_cancelled_job_is_dropped(jobs):
    release = threading.Event()
    started = threading.Event()

    def work():
        started.set()
        release.wait(5)
        return "stale result"

    job = jobs.submit("query", work, profile="p", channel="sql")
    assert started.wait(5)
    jobs.cancel(job.id)
    release.set()
    job = jobs.wait(job.id, 5)
    assert job.status == "cancelled" and job.result is None


def test_same_channel_supersedes_and_cancels_token(jobs):
    started = threading.Event()

    def slow(cancel_token):
        started.set()
        while not cancel_token.cancelled:
            threading.Event().wait(0.01)
        raise QueryCancelled()

    first = jobs.submit("query", slow, profile="p", channel="preview")
    assert started.wait(5)
    second = jobs.submit("query", lambda: 42, profile="p", channel="preview")
    assert jobs.wait(first.id, 5).status == "cancelled"
    assert jobs.wait(second.id, 5).result == 42


def test_errors_progress_and_on_success(jobs):
    recorded = []

    def fails():
        raise ValueError("boom")

    def steps(progress):
        progress("a")
        progress("b")
        return "ok"

    err = jobs.wait(jobs.submit("x", fails, profile="q").id, 5)
    assert err.status == "error" and isinstance(err.error, ValueError)
    ok = jobs.submit("x", steps, profile="q", on_success=lambda j, r: recorded.append(r))
    ok = jobs.wait(ok.id, 5)
    assert ok.status == "done" and ok.progress == ["a", "b"] and recorded == ["ok"]


def test_retention_per_profile(jobs):
    ids = [jobs.wait(jobs.submit("x", lambda i=i: i, profile="p").id, 5).id for i in range(4)]
    other = jobs.wait(jobs.submit("x", lambda: 0, profile="other").id, 5)
    remaining = {j.id for j in jobs.jobs("p")}
    assert remaining == set(ids[-2:])
    assert jobs.get(other.id).status == "done"


def test_shutdown_cancels_running_work():
    manager = JobManager(max_workers=1)
    started = threading.Event()

    def slow(cancel_token):
        started.set()
        while not cancel_token.cancelled:
            threading.Event().wait(0.01)
        raise QueryCancelled()

    job = manager.submit("query", slow, profile="p")
    queued = manager.submit("query", lambda: 1, profile="p")
    assert started.wait(5)
    assert manager.shutdown(timeout=5) is True
    assert job.status == "cancelled"
    assert queued.status in ("queued", "cancelled")
    with pytest.raises(RuntimeError):
        manager.submit("query", lambda: 1)


def test_unfinished_jobs_are_capped_globally():
    manager = JobManager(max_workers=2, max_unfinished=3)
    release = threading.Event()
    try:
        held = [manager.submit("q", lambda: release.wait(5), profile=f"p{i}") for i in range(3)]
        with pytest.raises(TooManyJobs):
            manager.submit("q", lambda: 1, profile="other")
        # A cancelled job no longer counts (hard cap: twice the limit)...
        manager.cancel(held[0].id)
        a = manager.submit("q", lambda: release.wait(5), profile="c", channel="sql")
        with pytest.raises(TooManyJobs):
            manager.submit("q", lambda: 1, profile="other")
        # ...and superseding a job in its own channel is allowed (it replaces it).
        b = manager.submit("q", lambda: 2, profile="c", channel="sql")
        assert a.cancel_requested
        release.set()
        assert manager.wait(b.id, 5).result == 2
        for job in held[1:]:
            assert manager.wait(job.id, 5).status == "done"
        assert manager.wait(manager.submit("q", lambda: 3).id, 5).result == 3
    finally:
        release.set()
        manager.shutdown(timeout=5)


class _Rows:
    def __init__(self, n: int) -> None:
        self.df = range(n)


def test_retained_rows_budget_evicts_oldest_finished_globally():
    manager = JobManager(max_workers=1, keep_per_profile=50, max_retained_rows=100)
    try:
        ids = [
            manager.wait(manager.submit("q", lambda: _Rows(40), profile=f"p{i}").id, 5).id
            for i in range(4)
        ]
        remaining = {j.id for j in manager.jobs()}
        assert remaining == set(ids[-2:])  # 2 x 40 rows fit in 100; 3 wouldn't
        # A single result bigger than the budget is kept (it's the newest).
        big = manager.wait(manager.submit("q", lambda: _Rows(500), profile="x").id, 5)
        assert {j.id for j in manager.jobs()} == {big.id}
        assert big.result is not None
    finally:
        manager.shutdown(timeout=5)


def test_finished_job_count_is_capped_globally():
    manager = JobManager(max_workers=1, keep_per_profile=50, max_finished=3)
    try:
        ids = [manager.wait(manager.submit("q", lambda: 1, profile=f"p{i}").id, 5).id
               for i in range(6)]
        assert {j.id for j in manager.jobs()} == set(ids[-3:])
    finally:
        manager.shutdown(timeout=5)
