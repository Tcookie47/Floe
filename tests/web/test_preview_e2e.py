"""Browser end-to-end test of the paged Preview (SPEC §8.5) against the fake Nessie and the
multi-file paging tables. The server runs in-process (uvicorn in a thread) so its
sessions can use the test hooks and a slow-page switch; skipped without Playwright /
Chromium or DuckDB's iceberg extension.
"""

from __future__ import annotations

import re
import threading
import time

import pytest

from floe.core.context import FloeSession
from floe.core.errors import QueryCancelled
from floe.core.nessie import NessieClient
from floe.core.profiles import ProfileStore
from floe.web.app import create_app
from tests.fakes.fake_nessie import DEFAULT_CLIENT_SECRET
from tests.fakes.iceberg_fixtures import (
    REGISTRY_KEY,
    SHARED_CONTAINERS,
    SHARED_NAMESPACES,
    seed_fake_nessie,
    seed_paging_tables,
)
from tests.web.test_e2e import _api_get, _new_page, browser  # noqa: F401 - `browser` fixture

pytestmark = pytest.mark.e2e

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

TOKEN = "e2e-preview-token-" + "p" * 24
SHARED_VIEW = "gold_ref_paging_codes"
TENANT_VIEW = "silver_paging_events"
CHANGED = "This branch changed since you started browsing — back to page 1"


@pytest.fixture
def preview_server(fake_nessie, iceberg_fixtures, paging_tables, iceberg_unavailable_reason):
    if iceberg_unavailable_reason:
        pytest.skip(iceberg_unavailable_reason)
    import uvicorn

    from floe.cli import _bind

    seed_fake_nessie(fake_nessie, iceberg_fixtures)
    seed_paging_tables(fake_nessie, paging_tables)
    store = ProfileStore()
    store.save(fake_nessie.profile(
        name="e2e-paging",
        nessie_head_ttl_seconds=0,
        shared_containers=list(SHARED_CONTAINERS),
        shared_namespaces=list(SHARED_NAMESPACES),
        tenant_registry_table=REGISTRY_KEY,
        allow_export=True,
    ))
    # Test switch: page loads at offset >= slow["offset"] wait (cancellably) for up to 20 s.
    slow: dict = {"offset": None, "cancelled": []}

    def factory(p):
        session = FloeSession(
            p, {}, NessieClient(p, DEFAULT_CLIENT_SECRET, timeout=5),
            local_storage_root=iceberg_fixtures.iceberg_root, skip_storage_setup=True,
        )
        real = session.preview_page

        def preview_page(ref, view_name, offset=0, tenant_filter=True, **kw):
            token = kw.get("cancel_token")
            if slow["offset"] is not None and offset >= slow["offset"] and token is not None:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if token.cancelled:
                        slow["cancelled"].append(offset)
                        raise QueryCancelled()
                    time.sleep(0.02)
            return real(ref, view_name, offset, tenant_filter, **kw)

        session.preview_page = preview_page
        return session

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
        yield {"url": f"http://127.0.0.1:{port}/?token={TOKEN}", "fake": fake_nessie,
               "slow": slow}
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_preview_pages_from_the_server(browser, preview_server):  # noqa: F811
    context, page, console = _new_page(browser)
    jobs: list[dict] = []  # every POST /api/jobs body + its job id

    def on_response(response):
        request = response.request
        if request.method == "POST" and request.url.endswith("/api/jobs"):
            try:
                jobs.append({**(request.post_data_json or {}), "_id": response.json().get("id")})
            except Exception:  # noqa: BLE001 - e.g. an error body
                pass

    page.on("response", on_response)

    def preview_jobs():
        return [j for j in jobs if j.get("kind") == "preview"]

    page.goto(preview_server["url"])
    expect(page.locator("body")).to_have_attribute("data-ready", "true")
    expect(page.locator("#profile-select")).to_have_value("e2e-paging")
    page.select_option("#branch-select", "main")
    expect(page.locator("#branch-select")).to_have_value("main")
    page.keyboard.press("Alt+1")

    rows = page.locator("#preview-results tbody tr")
    status = page.locator("#preview-results .results-status")
    label = page.locator("#preview-results .page-label")
    nxt = page.locator("#preview-results [aria-label='Next page']")
    prev = page.locator("#preview-results [aria-label='Previous page']")
    last = page.locator("#preview-results [aria-label='Last page']")
    count_btn = page.locator("#preview-results .count-btn")
    first_rownum = rows.first.locator("td.rownum")

    # --- A shared table on main: exact total from the snapshot summary, Last available.
    page.click(f".tree-row.table[data-view='{SHARED_VIEW}']")
    expect(page.locator("#preview-title")).to_contain_text("500 rows per page")
    expect(rows).to_have_count(500)
    expect(status).to_contain_text("Rows 1–500 of 2,400")
    expect(label).to_have_text("1–500 of 2,400")
    expect(last).to_be_enabled()
    expect(count_btn).to_be_hidden()
    assert [j["offset"] for j in preview_jobs()] == [0]
    assert preview_jobs()[0].get("expect_version") is None

    nxt.click()
    expect(label).to_have_text("501–1,000 of 2,400")
    expect(rows).to_have_count(500)
    expect(first_rownum).to_have_text("501")
    expect(status).to_contain_text("Rows 501–1,000 of 2,400")
    page_two = preview_jobs()[-1]
    assert page_two["offset"] == 500 and page_two["view_name"] == SHARED_VIEW
    assert page_two["expect_version"]  # pinned to page 1's data version

    prev.click()
    expect(label).to_have_text("1–500 of 2,400")
    expect(first_rownum).to_have_text("1")
    assert preview_jobs()[-1]["offset"] == 0  # fetched again from the server

    last.click()
    expect(label).to_have_text("2,001–2,400 of 2,400")
    expect(rows).to_have_count(400)
    expect(nxt).to_be_disabled()
    expect(last).to_be_disabled()
    assert preview_jobs()[-1]["offset"] == 2000
    assert len(preview_jobs()) == 4

    # Export / Save as image cover the page that is showing.
    page.click("#preview-results .export-btn")
    expect(page.locator("#confirm-message")).to_contain_text(
        "Export this page (rows 2,001–2,400) to a CSV file?"
    )
    with page.expect_download() as download_info:
        page.click("#confirm-ok")
    assert download_info.value.suggested_filename == f"{SHARED_VIEW}_rows_2001-2400.csv"
    page.click("#preview-results .image-btn")
    expect(page.locator("#confirm-message")).to_contain_text("this page (rows 2,001–2,400)")
    page.click("#confirm-cancel")

    # --- A tenant-filtered table: total unknown ("of N+"), Count rows enables Last.
    page.select_option("#branch-select", "eg-test1")
    expect(page.locator(f".tree-row.table[data-view='{TENANT_VIEW}']")).to_be_visible()
    page.click(f".tree-row.table[data-view='{TENANT_VIEW}']")
    expect(rows).to_have_count(500)
    expect(label).to_have_text("1–500 of 500+")
    expect(status).to_contain_text("Rows 1–500 of 500+")
    expect(last).to_be_disabled()
    expect(count_btn).to_be_visible()
    nxt.click()
    expect(label).to_have_text("501–1,000 of 1,000+")
    count_btn.click()
    expect(label).to_have_text("501–1,000 of 1,800")
    expect(count_btn).to_be_hidden()
    expect(last).to_be_enabled()
    counts = [j for j in jobs if j.get("kind") == "preview_count"]
    assert len(counts) == 1 and counts[0]["view_name"] == TENANT_VIEW
    last.click()
    expect(label).to_have_text("1,501–1,800 of 1,800")
    expect(rows).to_have_count(300)

    # --- The branch moves while browsing: back to page 1 with a notice.
    preview_server["fake"].move_head("eg-test1")
    prev.click()
    expect(page.locator("#preview-results .results-notice")).to_have_text(CHANGED)
    expect(label).to_have_text("1–500 of 500+")  # the count was for the old version
    expect(first_rownum).to_have_text("1")

    # --- Branch change mid-paging cancels the in-flight page load.
    nxt.click()
    expect(label).to_have_text("501–1,000 of 1,000+")
    preview_server["slow"]["offset"] = 1000
    nxt.click()
    deadline = time.monotonic() + 10
    while not preview_jobs() or preview_jobs()[-1]["offset"] != 1000:
        assert time.monotonic() < deadline
        page.wait_for_timeout(50)
    slow_id = preview_jobs()[-1]["_id"]
    page.select_option("#branch-select", "main")
    expect(page.locator("#preview-title")).to_have_text("Select a table in the tree.")
    deadline = time.monotonic() + 10
    while True:
        job = _api_get(context, page, f"/api/jobs/{slow_id}").json()
        if job["status"] == "cancelled":
            break
        assert time.monotonic() < deadline, job
        page.wait_for_timeout(50)
    assert preview_server["slow"]["cancelled"] == [1000]
    preview_server["slow"]["offset"] = None
    page.click(f".tree-row.table[data-view='{SHARED_VIEW}']")
    expect(label).to_have_text("1–500 of 2,400")
    expect(page.locator("#preview-results thead th[data-col='0']")).to_have_attribute(
        "title", re.compile("Sorts this page only")
    )
    console.assert_clean()
    context.close()
