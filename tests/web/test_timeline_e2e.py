"""Browser end-to-end test of the Timeline tab (SPEC §15.7) against the fake Nessie and
pyiceberg fixtures. The server runs in-process (uvicorn in a thread) so its sessions can
use the test hooks (`local_storage_root`, `skip_storage_setup`); skipped without
Playwright / Chromium or DuckDB's iceberg extension.

Set FLOE_E2E_SCREENSHOTS=<dir> to keep the Timeline screenshot.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import pytest

from floe.core.context import FloeSession
from floe.core.nessie import NessieClient
from floe.core.profiles import ProfileStore
from floe.core.timeline import METADATA_CACHE
from floe.web.app import create_app
from tests.fakes.fake_nessie import DEFAULT_CLIENT_SECRET
from tests.fakes.iceberg_fixtures import (
    HISTORY_KEY,
    REGISTRY_KEY,
    SHARED_CONTAINERS,
    SHARED_NAMESPACES,
    build_history_table,
    seed_fake_nessie,
)
from tests.web.test_e2e import _new_page, browser  # noqa: F401 - `browser` is a fixture

pytestmark = pytest.mark.e2e

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

TOKEN = "e2e-timeline-token-" + "t" * 24
MESSAGE = "pipeline: load events <b>not bold</b>\nrun=synthetic-42\n  rows=4"


@pytest.fixture(scope="module")
def history_table(iceberg_fixtures):
    return build_history_table(
        iceberg_fixtures.iceberg_root, container="eg-test1", key=HISTORY_KEY + "_e2e"
    )


@pytest.fixture
def timeline_server(fake_nessie, iceberg_fixtures, history_table, iceberg_unavailable_reason):
    if iceberg_unavailable_reason:
        pytest.skip(iceberg_unavailable_reason)
    import uvicorn

    from floe.cli import _bind

    METADATA_CACHE.clear()
    fx = iceberg_fixtures
    seed_fake_nessie(fake_nessie, fx)
    fake_nessie.add_table(
        "eg-test1", history_table.key, metadata_location=history_table.location,
        commit_message=MESSAGE, commit_time=time.time() - 3 * 3600,
    )
    fake_nessie.state.history_page_size = 5
    store = ProfileStore()
    store.save(fake_nessie.profile(
        name="e2e-remote",
        nessie_head_ttl_seconds=0,
        shared_containers=list(SHARED_CONTAINERS),
        shared_namespaces=list(SHARED_NAMESPACES),
        tenant_registry_table=REGISTRY_KEY,
    ))

    def factory(p):
        return FloeSession(
            p, {}, NessieClient(p, DEFAULT_CLIENT_SECRET, timeout=5),
            local_storage_root=fx.iceberg_root, skip_storage_setup=True,
        )

    sock = _bind(0)
    port = sock.getsockname()[1]
    app = create_app(token=TOKEN, port=port, store=store, session_factory=factory)
    server = uvicorn.Server(uvicorn.Config(
        app, log_config=None, log_level="warning", access_log=False, lifespan="on",
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield {"url": f"http://127.0.0.1:{port}/?token={TOKEN}", "fake": fake_nessie}
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_timeline_freshness_commits_and_table_history(
    browser, timeline_server, history_table, tmp_path  # noqa: F811
):
    context, page, console = _new_page(browser)
    page.goto(timeline_server["url"])
    expect(page.locator("body")).to_have_attribute("data-ready", "true")
    expect(page.locator("#profile-select")).to_have_value("e2e-remote")
    expect(page.locator("#branch-select")).to_have_value("eg-test1")
    expect(page.locator(".tree-row.table[data-view='bronze_medical']")).to_be_visible()

    # Alt+4 opens the Timeline; freshness rows show ages with exact-time tooltips.
    page.keyboard.press("Alt+4")
    expect(page.locator("#tab-timeline")).to_be_visible()
    fresh = page.locator("[data-testid=freshness-table]")
    row = fresh.locator(f"tr[data-key='{history_table.key}']")
    expect(row).to_be_visible()
    expect(row.locator("td").nth(2)).to_have_text(re.compile(r"just now|min ago"))
    assert re.search(r"\d{4}", row.locator(".age").first.get_attribute("title"))
    expect(row.locator(".tl-op")).to_have_text("append")
    expect(row.locator("td").nth(5)).to_have_text("4")
    expect(row.locator("td").nth(6)).to_have_text("3 h ago")
    expect(row.locator("td").nth(7)).to_have_text("pipeline: load events <b>not bold</b>")
    shared = fresh.locator("tr[data-key='gold_ref.codes']")
    expect(shared).to_be_visible()
    assert "Resolved from ref: main" in shared.get_attribute("title")
    expect(fresh.locator("tbody tr")).to_have_count(7)

    # Filter + layer filter; sort by total rows.
    page.fill("#tl-filter", "events")
    expect(fresh.locator("tbody tr")).to_have_count(1)
    page.fill("#tl-filter", "")
    page.select_option("#tl-layer", "gold_ref")
    expect(fresh.locator("tbody tr")).to_have_count(1)
    page.select_option("#tl-layer", index=0)
    fresh.locator("th[data-col='total_records']").click()
    total_th = fresh.locator("th[data-col='total_records']")
    expect(total_th).to_have_attribute("aria-sort", "descending")

    # Branch timeline: newest first, messages escaped with line breaks kept; Load more.
    commits = page.locator("[data-testid=commit-list] > li")
    expect(commits).to_have_count(5)
    newest = commits.first
    assert newest.locator(".tl-msg").inner_text() == MESSAGE
    assert newest.locator(".tl-msg b").count() == 0
    expect(newest.locator(".hash")).to_have_text(re.compile(r"^[0-9a-f]{10}$"))
    page.click(".tl-more")
    expect(commits).to_have_count(6)
    expect(page.locator(".tl-more")).to_have_count(0)

    # A touched-table chip opens the table history: snapshots + SVG chart.
    newest.locator(f".chip[data-key='{history_table.key}']").click()
    snaps = page.locator("[data-testid=snapshot-table] tbody tr")
    expect(snaps).to_have_count(4)
    expect(snaps.first.locator(".tl-op")).to_have_text("append")
    expect(snaps.nth(1).locator(".tl-op")).to_have_text("delete")
    chart = page.locator("[data-testid=snapshot-chart]")
    expect(chart).to_be_visible()
    expect(chart.locator("circle")).to_have_count(4)
    expect(chart.locator("circle.op-delete")).to_have_count(1)
    assert chart.locator("polyline").count() == 1
    expect(page.locator("#tl-table-title")).to_contain_text(history_table.key)
    out = Path(os.environ.get("FLOE_E2E_SCREENSHOTS") or tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / "floe-timeline.png"))

    # Tree context menu → Show history (a shared table, resolved from main).
    page.keyboard.press("Alt+1")
    page.click(".tree-row.table[data-view='gold_ref_codes']", button="right")
    page.get_by_role("menuitem", name="Show history").click()
    expect(page.locator("#tab-timeline")).to_be_visible()
    expect(page.locator("#tl-table-title")).to_contain_text(
        "gold_ref.codes · gold_ref_codes · from main"
    )
    expect(page.locator("[data-testid=snapshot-table] tbody tr")).to_have_count(1)

    # Branch change reloads the timeline for the new branch.
    page.select_option("#branch-select", "eg-test2")
    expect(fresh.locator(f"tr[data-key='{history_table.key}']")).to_have_count(0)
    expect(fresh.locator("tr[data-key='bronze.medical']")).to_be_visible()
    console.assert_clean()
    context.close()
