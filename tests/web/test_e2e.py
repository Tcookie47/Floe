"""Browser end-to-end tests of the web UI (SPEC §8, §15): a real `floe serve
--no-browser` subprocess against a temp FLOE_HOME and the synthetic local-mode fixtures,
driven with Playwright + Chromium. Skipped when Playwright or Chromium isn't installed.

Set FLOE_E2E_SCREENSHOTS=<dir> to keep the light/dark screenshots of the main view.
"""

from __future__ import annotations

import http.server
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from floe import cli
from floe.core import ask
from floe.core.profiles import Profile, ProfileStore
from tests.fakes.iceberg_fixtures import REGISTRY_KEY, SHARED_CONTAINERS, SHARED_NAMESPACES
from tests.web.conftest import SLOW_SQL

pytestmark = pytest.mark.e2e

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT_MS = 20_000
JOIN_SQL = (
    "SELECT m.id, m.code, c.label FROM bronze_medical m "
    "JOIN gold_ref_codes c ON m.code = c.code ORDER BY m.id"
)
ASK_SQL = "SELECT code, count(*) AS n FROM bronze_medical GROUP BY code ORDER BY code"
# Values that exist only in fixture rows (never in names/types): must never reach the LLM.
ROW_SENTINELS = ("C001", "C003", "alpha", "o'delta", "synthetic-a", "10.5")


@pytest.fixture(scope="module")
def browser():
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Playwright unavailable: {exc}")
    try:
        chromium = pw.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - no browser downloaded
        pw.stop()
        pytest.skip(f"Chromium unavailable (run `playwright install chromium`): {exc}")
    yield chromium
    chromium.close()
    pw.stop()


def _launch(tmp_path: Path, n: int):
    """Start `floe serve --no-browser`; returns (proc, stderr file, tokenized url)."""
    env = dict(os.environ)
    env.update(
        FLOE_HOME=str(tmp_path),
        # Never touch a real keychain; the Ask API key comes from its env var instead.
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
        FLOE_OPENROUTER_API_KEY="sk-or-synthetic-e2e-KEY",
        PYTHONUNBUFFERED="1",
    )
    env.pop("QT_QPA_PLATFORM", None)
    stderr_path = tmp_path / f"server-stderr-{n}.txt"
    stderr = open(stderr_path, "w", encoding="utf-8")  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, "-m", "floe.cli", "serve", "--no-browser"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
    )
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(
        target=lambda: [lines.put(line) for line in proc.stdout], daemon=True
    ).start()
    url = None
    deadline = time.monotonic() + 30
    while url is None and time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        m = re.search(r"http://127\.0\.0\.1:\d+/\?token=[A-Za-z0-9_-]+", line)
        url = m.group(0) if m else None
    return proc, stderr, stderr_path, url


def _stop(proc, stderr) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    stderr.close()


@pytest.fixture
def server(tmp_path):
    """Start `floe serve --no-browser`; yields a dict with the tokenized `url` (single
    use) and `restart()`, which restarts the server on the same FLOE_HOME and returns
    the new launch URL (also stored in `url`)."""
    running: list = []

    def start() -> str:
        proc, stderr, stderr_path, url = _launch(tmp_path, len(running))
        running.append((proc, stderr))
        if url is None:
            pytest.fail("floe serve did not print its URL: "
                        + stderr_path.read_text(encoding="utf-8")[-2000:])
        return url

    info: dict = {"home": tmp_path}

    def restart() -> str:
        _stop(*running[-1])
        info["url"] = start()
        return info["url"]

    try:
        info["url"] = start()
        info["restart"] = restart
        yield info
    finally:
        for proc, stderr in running:
            if proc.poll() is None or not stderr.closed:
                _stop(proc, stderr)


class FakeOpenRouter:
    """A local chat-completions endpoint that records request bodies."""

    def __init__(self, sql: str) -> None:
        self.bodies: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                outer.bodies.append(json.loads(self.rfile.read(length)))
                content = f"Here you go:\n```sql\n{sql}\n```"
                body = json.dumps({
                    "model": "synthetic/free-model:free",
                    "choices": [{"message": {"content": content}}],
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class Console:
    """Collects page errors and CSP violations (which Chromium reports on the console)."""

    def __init__(self, page) -> None:
        self.errors: list[str] = []
        page.on("pageerror", lambda exc: self.errors.append(f"pageerror: {exc}"))
        page.on("console", self._on_console)

    def _on_console(self, msg) -> None:
        text = msg.text
        if msg.type == "error" and "Failed to load resource" not in text:
            self.errors.append(text)
        elif "Content Security Policy" in text or "Refused to" in text:
            self.errors.append(text)

    def assert_clean(self) -> None:
        assert self.errors == []


def _new_page(browser, *, dark: bool = False):
    context = browser.new_context(
        viewport={"width": 1400, "height": 860},
        color_scheme="dark" if dark else "light",
        accept_downloads=True,
    )
    context.set_default_timeout(TIMEOUT_MS)
    page = context.new_page()
    return context, page, Console(page)


def _open(page, url: str) -> None:
    page.goto(url)
    expect(page.locator("body")).to_have_attribute("data-ready", "true")


def _set_editor(page, text: str) -> None:
    page.keyboard.press("Alt+3")
    editor = page.locator("[data-testid=sql-editor]")
    editor.click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.press("Delete")
    page.keyboard.insert_text(text)


def _editor_text(page) -> str:
    return page.locator("[data-testid=sql-editor]").inner_text()


def _screenshot(page, name: str, tmp_path: Path) -> None:
    out = Path(os.environ.get("FLOE_E2E_SCREENSHOTS") or tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out / name))


def _api_get(context, page, path: str):
    """GET an API path with the page's cookie *and* its per-launch API key header."""
    key = page.evaluate("sessionStorage.getItem('floe-api-key')")
    base = page.url.split("?")[0].split("#")[0].rstrip("/")
    return context.request.get(base + path, headers={"X-Floe-Auth": key})


def _wait_prefs(context, page, check, timeout: float = 10.0) -> dict:
    """Poll GET /api/prefs until `check(prefs)` (prefs are saved debounced)."""
    deadline = time.monotonic() + timeout
    while True:
        prefs = _api_get(context, page, "/api/prefs").json()
        if check(prefs):
            return prefs
        assert time.monotonic() < deadline, prefs
        time.sleep(0.1)


def _seed_profile(fx, name: str = "e2e-seeded") -> None:
    """Saved in-process: FLOE_HOME is the same temp dir the server uses."""
    ProfileStore().save(
        Profile(
            name=name,
            mode="local",
            local_fixture_dir=str(fx.local_root),
            shared_containers=list(SHARED_CONTAINERS),
            shared_namespaces=list(SHARED_NAMESPACES),
            tenant_registry_table=REGISTRY_KEY,
        )
    )


# --------------------------------------------------------------------------- main flow


def test_profile_browse_preview_schema_sql(browser, server, iceberg_fixtures, tmp_path):
    fx = iceberg_fixtures
    context, page, console = _new_page(browser)
    _open(page, server["url"])

    # No profiles yet: the Profiles dialog opens on a new-profile form.
    dialog = page.locator("#profiles-dialog")
    expect(dialog).to_be_visible()
    page.fill("#pf-name", "e2e-local")
    page.select_option("#pf-mode", "local")
    expect(page.locator("#pf-local_fixture_dir")).to_be_visible()
    expect(page.locator("fieldset[data-group='Nessie']")).to_be_hidden()
    page.click("#pf-save")  # missing fixture dir → per-field validation error
    expect(page.locator("#pf-local_fixture_dir-error")).to_contain_text("fixture directory")
    page.fill("#pf-local_fixture_dir", str(fx.local_root))
    page.fill("#pf-preview_row_limit", "0")
    page.click("#pf-save")
    expect(page.locator("#pf-preview_row_limit-error")).to_be_visible()
    page.fill("#pf-preview_row_limit", "1000")
    page.fill("#pf-shared_containers", "\n".join(SHARED_CONTAINERS))
    page.fill("#pf-shared_namespaces", "\n".join(SHARED_NAMESPACES))
    page.fill("#pf-tenant_registry_table", REGISTRY_KEY)
    page.check("#pf-allow_export")
    page.click("#pf-test-btn")
    expect(page.locator("#pf-test-summary")).to_have_text("All checks passed.")
    page.click("#pf-save")
    expect(page.locator("#pf-status")).to_have_text('Saved "e2e-local".')
    selected = page.locator("#profile-list li", has_text="e2e-local")
    expect(selected).to_have_class(re.compile("selected"))
    dialog.locator(".dialog-foot [data-close]").click()
    expect(dialog).to_be_hidden()

    # Profile + branch pickers; inactive tenants are marked.
    expect(page.locator("#profile-select")).to_have_value("e2e-local")
    expect(page.locator("#catalog-pill")).to_have_text("Local mode")
    inactive = page.locator("#branch-select option[value='eg-test2']")
    expect(inactive).to_have_text("eg-test2 (inactive)")
    page.select_option("#branch-select", "eg-test1")
    expect(page.locator(".tree-row.table[data-view='bronze_medical']")).to_be_visible()
    expect(page.locator("#tenant-filter")).to_be_enabled()

    # Ctrl/Cmd+R refreshes the catalog instead of reloading the page.
    page.evaluate("window.__noReload = true; document.querySelector('#statusbar').textContent = ''")
    page.keyboard.press("ControlOrMeta+r")
    expect(page.locator("#statusbar")).to_contain_text("6 tables on eg-test1.")
    assert page.evaluate("window.__noReload") is True

    # Tree: layers first, then the reference group; the filter keeps matching parents.
    expect(page.locator("#tree > .tree-group > .tree-row .label")).to_have_text(
        ["bronze", "silver", "gold", "Reference (main)"]
    )
    page.keyboard.press("ControlOrMeta+f")
    expect(page.locator("#tree-filter")).to_be_focused()
    page.keyboard.type("claim")
    visible = page.locator(".tree-row.table:visible")
    expect(visible).to_have_count(1)
    expect(visible).to_have_attribute("data-view", "silver_input_layer_medical_claim")
    page.fill("#tree-filter", "")
    expect(page.locator(".tree-row.table:visible")).to_have_count(6)
    tip = page.get_attribute(".tree-row.table[data-view='gold_ref_codes']", "title")
    assert "SQL view: gold_ref_codes" in tip and "Resolved from ref:" in tip

    # Preview (tenant-filtered: 3 own rows) and Schema.
    page.keyboard.press("Alt+1")
    page.click(".tree-row.table[data-view='bronze_medical']")
    rows = page.locator("#preview-results tbody tr")
    expect(rows).to_have_count(3)
    expect(page.locator("#preview-results .results-status")).to_contain_text("3 rows")
    expect(page.locator("#preview-results thead th .cname")).to_have_text(
        ["id", "amount", "code", "data_source"]
    )
    # Client-side sort on the loaded page.
    page.click("#preview-results thead th[data-col='0']")
    page.click("#preview-results thead th[data-col='0']")
    expect(rows.first.locator("td").nth(1)).to_have_text("3")
    page.keyboard.press("Alt+2")
    expect(page.locator("#schema-results tbody tr")).to_have_count(4)
    expect(page.locator("#schema-results tbody tr").first).to_contain_text("id")

    # A table without data_source: the tenant filter fails → one-click override.
    page.keyboard.press("Alt+1")
    page.click(".tree-row.table[data-view='silver_input_layer_no_ds']")
    button = page.get_by_role("button", name="Show without tenant filter")
    expect(button).to_be_visible()
    button.click()
    expect(page.locator("#preview-results tbody tr")).to_have_count(2)
    expect(page.locator("#preview-results .results-status")).to_contain_text("tenant filter off")

    # SQL: a join across a tenant table and a reference table.
    _set_editor(page, JOIN_SQL)
    page.keyboard.press("ControlOrMeta+Enter")
    expect(page.locator("#sql-results tbody tr")).to_have_count(3)
    expect(page.locator("#sql-results .results-status")).to_contain_text("3 rows")
    expect(page.locator("#sql-results tbody tr").first).to_contain_text("alpha")
    expect(page.locator("#history-select option")).to_have_count(2)
    _screenshot(page, "floe-web.png", tmp_path)

    # Cell selection → Ctrl/Cmd+C copies TSV.
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.click("#sql-results tbody tr >> nth=0 >> td[data-c='0']")
    page.click("#sql-results tbody tr >> nth=1 >> td[data-c='1']", modifiers=["Shift"])
    page.keyboard.press("ControlOrMeta+c")
    expect(page.locator("#toast")).to_contain_text("Copied 2 line(s)")
    assert page.evaluate("navigator.clipboard.readText()") == "1\tC001\n2\tC002"

    # Export (allowed for this profile) after a confirm dialog with the row count.
    export = page.locator("#sql-results .export-btn")
    expect(export).to_be_enabled()
    export.click()
    expect(page.locator("#confirm-message")).to_contain_text("Export 3 row(s)")
    with page.expect_download() as download_info:
        page.click("#confirm-ok")
    csv_text = Path(download_info.value.path()).read_text(encoding="utf-8")
    assert csv_text.splitlines()[0] == "id,code,label" and len(csv_text.splitlines()) == 4

    # Paging (500 rows per page) and the row-limit truncation notice.
    _set_editor(page, "SELECT range AS n FROM range(1200)")
    page.fill("#row-limit", "1000")
    page.click("#btn-run")
    expect(page.locator("#sql-results .results-status")).to_contain_text(
        "Showing first 1,000 rows (limit reached)"
    )
    expect(page.locator("#sql-results .page-label")).to_have_text("1–500 of 1,000")
    expect(page.locator("#sql-results tbody tr")).to_have_count(500)
    page.click("#sql-results [aria-label='Next page']")
    expect(page.locator("#sql-results .page-label")).to_have_text("501–1,000 of 1,000")
    expect(page.locator("#sql-results tbody tr").first.locator("td").nth(1)).to_have_text("500")
    page.fill("#row-limit", "10000")

    # Cancel a long query with Esc.
    _set_editor(page, SLOW_SQL)
    page.click("#btn-run")
    expect(page.locator("#btn-cancel")).to_be_enabled()
    expect(page.locator("#sql-results .results-status")).to_contain_text("Running…")
    page.keyboard.press("Escape")
    expect(page.locator("#sql-results .results-message")).to_have_text("Query cancelled.")
    expect(page.locator("#btn-run")).to_be_enabled()

    # Writes are refused with the read-only message.
    _set_editor(page, "DELETE FROM bronze_medical")
    page.click("#btn-run")
    expect(page.locator("#sql-results .results-error")).to_contain_text(
        "Only read-only queries are allowed"
    )

    # MissingDataSourceColumn in SQL → "Run without tenant filter" (one run only).
    _set_editor(page, "SELECT * FROM silver_input_layer_no_ds ORDER BY id")
    page.click("#btn-run")
    run_without = page.get_by_role("button", name="Run without tenant filter")
    expect(run_without).to_be_visible()
    run_without.click()
    expect(page.locator("#sql-results tbody tr")).to_have_count(2)
    expect(page.locator("#sql-results .results-status")).to_contain_text("tenant filter off")

    # Help menu: About shows the version; shortcuts list opens.
    page.click("#btn-help")
    page.click("#help-menu [data-action='about']")
    expect(page.locator("#about-commit")).not_to_have_text("…")
    page.locator("#about-dialog [data-close]").first.click()
    _wait_prefs(
        context,
        page,
        lambda p: p["last_profile"] == "e2e-local"
        and p["last_branch"].get("e2e-local") == "eg-test1"
        and p["last_tab"] == "sql"
        and "silver_input_layer_no_ds" in p["editor_text"].get("e2e-local", ""),
    )
    console.assert_clean()
    context.close()

    # Prefs restore profile / branch / tab / editor text (after a restart: the launch
    # link is single-use); dark-mode screenshot.
    context, page, console = _new_page(browser, dark=True)
    _open(page, server["restart"]())
    expect(page.locator("#profile-select")).to_have_value("e2e-local")
    expect(page.locator("#branch-select")).to_have_value("eg-test1")
    expect(page.locator("#tab-sql")).to_be_visible()
    assert "silver_input_layer_no_ds" in _editor_text(page)
    expect(page.locator(".tree-row.table[data-view='bronze_medical']")).to_be_visible()
    # Double-click a table inserts its view name into the editor.
    _set_editor(page, "SELECT * FROM ")
    page.dblclick(".tree-row.table[data-view='gold_ref_codes']")
    expect(page.locator("[data-testid=sql-editor]")).to_contain_text("SELECT * FROM gold_ref_codes")
    _set_editor(page, JOIN_SQL)
    page.click("#btn-run")
    expect(page.locator("#sql-results tbody tr")).to_have_count(3)
    page.click(".tree-row.table[data-view='bronze_medical']")
    _screenshot(page, "floe-web-dark.png", tmp_path)
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert bg != "rgb(255, 255, 255)"
    console.assert_clean()
    context.close()


def test_new_profile_form_is_not_reset_by_a_slow_profile_list(browser, server):
    """Regression: the dialog used to show before its list loaded, so typing into it was
    wiped when a slow GET /api/profiles (e.g. a slow keyring probe) came back."""
    context, page, _console = _new_page(browser)

    # Delay GET /api/profiles in the page (a blocking route handler would stall the test).
    page.add_init_script("""
      const realFetch = window.fetch;
      window.__listGets = 0;
      window.fetch = async (input, init) => {
        const url = typeof input === "string" ? input : input.url;
        const method = ((init && init.method) || "GET").toUpperCase();
        // The 1st list GET is the page's own; delay the 2nd (the dialog's).
        if (method === "GET" && /\\/api\\/profiles(\\?|$)/.test(url) && ++window.__listGets === 2) {
          await new Promise((resolve) => setTimeout(resolve, 2000));
        }
        return realFetch(input, init);
      };
    """)
    _open(page, server["url"])
    expect(page.locator("#profiles-dialog")).to_be_visible()
    # Ready to type the moment it shows (no waiting: that would hide the old race).
    assert page.evaluate("document.activeElement && document.activeElement.id") == "pf-name"
    page.fill("#pf-name", "e2e-typed")
    page.select_option("#pf-mode", "local")
    page.wait_for_timeout(2500)
    expect(page.locator("#pf-name")).to_have_value("e2e-typed")
    expect(page.locator("#pf-mode")).to_have_value("local")
    context.close()


# --------------------------------------------------------------------------- ask


def test_ask_generates_sql_into_editor_without_running(browser, server, iceberg_fixtures):
    fake = FakeOpenRouter(ASK_SQL)
    try:
        _seed_profile(iceberg_fixtures)
        context, page, console = _new_page(browser)
        _open(page, server["url"])
        expect(page.locator("#profile-select")).to_have_value("e2e-seeded")
        page.select_option("#branch-select", "eg-test1")
        expect(page.locator(".tree-row.table[data-view='bronze_medical']")).to_be_visible()
        page.keyboard.press("Alt+3")
        # This profile doesn't allow export.
        _set_editor(page, "SELECT 1 AS x")
        page.click("#btn-run")
        expect(page.locator("#sql-results tbody tr")).to_have_count(1)
        export = page.locator("#sql-results .export-btn")
        expect(export).to_be_disabled()
        expect(export).to_have_attribute("title", re.compile("Export is disabled for this profile"))
        page.click("#btn-ask-toggle")
        expect(page.locator("#ask-panel")).to_be_visible()
        expect(page.locator("#ask-panel .ask-note")).to_contain_text(
            "Only table and column names are sent — never data"
        )
        # Not configured yet → disabled with a "Set up Ask…" link.
        expect(page.locator("#ask-question")).to_be_disabled()
        page.click("#btn-ask-setup")
        expect(page.locator("#ask-settings-dialog")).to_be_visible()
        expect(page.locator("#as-api-key")).to_have_attribute("placeholder", "(saved)")
        # There is no model field any more — Ask always uses the fixed fallback chain.
        expect(page.locator("#ask-settings-dialog")).not_to_contain_text("Model ID")
        page.check("#as-enabled")
        page.click("#as-advanced summary")
        page.fill("#as-base-url", fake.base_url)
        page.fill("#as-timeout", "20")
        page.click("#as-save")
        expect(page.locator("#ask-settings-dialog")).to_be_hidden()
        expect(page.locator("#ask-question")).to_be_enabled()

        page.fill("#ask-question", "Count bronze_medical rows per code")
        page.click("#btn-ask-preview")
        preview = page.locator("#ask-preview")
        expect(preview).to_be_visible()
        expect(preview).to_contain_text("bronze_medical")
        expect(preview).to_contain_text(f'"model": "{ask.MODEL_CHAIN[0]}"')
        preview_text = preview.inner_text()
        assert not any(s in preview_text for s in ROW_SENTINELS)
        assert fake.bodies == []  # the preview sends nothing

        page.click("#btn-ask-generate")
        expect(page.locator("#ask-status")).to_contain_text("review it, then click Run")
        assert _editor_text(page).strip() == ASK_SQL
        # The first model in the chain answered: no fallback, and the note says which
        # model generated the SQL.
        expect(page.locator("#ask-generated-by")).to_have_text(
            "Generated by synthetic/free-model:free"
        )
        # Generated, never executed: the old result stays, the history is unchanged.
        expect(page.locator("#sql-results tbody tr")).to_have_count(1)
        history = _api_get(context, page, "/api/profiles/e2e-seeded/history")
        assert [e["sql"] for e in history.json()["entries"]] == ["SELECT 1 AS x"]
        assert len(fake.bodies) == 1
        sent = json.dumps(fake.bodies[0])
        assert "bronze_medical" in sent
        assert not any(s in sent for s in ROW_SENTINELS)

        # "Insert at cursor" keeps the existing editor text.
        _set_editor(page, "-- mine\n")
        page.check("input[name=ask-mode][value=insert]")
        page.click("#btn-ask-generate")
        expect(page.locator("#ask-status")).to_contain_text("review it, then click Run")
        text = _editor_text(page)
        assert text.startswith("-- mine") and ASK_SQL in text
        console.assert_clean()
        context.close()
    finally:
        fake.close()


# --------------------------------------------------------------------------- profiles


ENV_TEXT = """\
# synthetic
ADLS_ACCOUNT=egtestaccount
ADLS_ACCOUNT_KEY=synthetic-account-key-0123
NESSIE_URI=http://127.0.0.1:1/api/v2
NESSIE_AUTH_MODE=none
NESSIE_KEY_VAULT=kv-synthetic
GOLD_REF_CONTAINER=ref-a
"""


def test_profiles_import_env_keyring_error_duplicate_delete(browser, server, iceberg_fixtures):
    _seed_profile(iceberg_fixtures)
    context, page, console = _new_page(browser)
    _open(page, server["url"])
    expect(page.locator("#profile-select")).to_have_value("e2e-seeded")
    page.click("#btn-profiles")
    dialog = page.locator("#profiles-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#pf-name")).to_have_value("e2e-seeded")

    # Duplicate → name prompt → the copy is selected and appears in the top bar.
    page.click("#pf-duplicate")
    expect(page.locator("#confirm-input")).to_have_value("e2e-seeded copy")
    page.fill("#confirm-input", "e2e-copy")
    page.click("#confirm-ok")
    expect(page.locator("#pf-name")).to_have_value("e2e-copy")
    expect(page.locator("#profile-select option[value='e2e-copy']")).to_have_count(1)

    # New remote profile from a .env file: fields filled, secret held server-side only.
    page.click("#pf-new")
    page.fill("#pf-name", "e2e-remote")
    page.set_input_files(
        "#pf-import-file",
        files=[{"name": "synthetic.env", "mimeType": "text/plain", "buffer": ENV_TEXT.encode()}],
    )
    expect(page.locator("#pf-notes")).to_contain_text("Key Vault")
    expect(page.locator("#pf-adls_account")).to_have_value("egtestaccount")
    expect(page.locator("#pf-nessie_uri")).to_have_value("http://127.0.0.1:1/api/v2")
    expect(page.locator("#pf-nessie_auth")).to_have_value("none")
    expect(page.locator("#pf-shared_containers")).to_have_value("ref-a")
    expect(page.locator("#pf-nessie_token_endpoint")).to_be_hidden()
    key = page.locator("#pf-adls_account_key")
    expect(key).to_have_value("")
    expect(key).to_have_attribute("placeholder", "(from .env — saved on Save)")
    # No usable keyring in this server: the env-var fallback is explained up front.
    expect(page.locator("#pf-keyring-note")).to_contain_text(
        "FLOE_SECRET__E2E_REMOTE__ADLS_ACCOUNT_KEY"
    )

    # Test connection on the unsaved form: live step list, clear failure.
    page.click("#pf-test-btn")
    expect(page.locator("#pf-test-summary")).to_contain_text("Failed at")
    expect(page.locator("#pf-test-steps li").first).to_contain_text("✗")

    # Saving the secret fails clearly (no keyring) and names the env var to set.
    page.click("#pf-save")
    error = page.locator("#pf-general-error")
    expect(error).to_contain_text("Keychain / keyring error")
    expect(error).to_contain_text("FLOE_SECRET__E2E_REMOTE__ADLS_ACCOUNT_KEY")
    assert "synthetic-account-key-0123" not in page.content()

    # Clear the imported secret → the profile saves without it.
    page.click(".secret-clear[data-field='adls_account_key']")
    page.click("#pf-save")
    expect(page.locator("#pf-status")).to_have_text('Saved "e2e-remote".')
    expect(page.locator("#profile-list li")).to_have_text(["e2e-copy", "e2e-remote", "e2e-seeded"])

    # Delete the copy (with confirmation).
    page.click("#profile-list li:has-text('e2e-copy')")
    expect(page.locator("#pf-name")).to_have_value("e2e-copy")
    page.click("#pf-delete")
    expect(page.locator("#confirm-message")).to_contain_text('Delete profile "e2e-copy"?')
    page.click("#confirm-ok")
    expect(page.locator("#profile-list li")).to_have_text(["e2e-remote", "e2e-seeded"])
    expect(page.locator("#profile-select option[value='e2e-copy']")).to_have_count(0)
    assert "synthetic-account-key-0123" not in page.content()
    dialog.locator(".dialog-foot [data-close]").click()

    # Help → Copy diagnostics puts the (redacted) diagnostics on the clipboard.
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.click("#btn-help")
    page.click("#help-menu [data-action='diagnostics']")
    expect(page.locator("#toast")).to_have_text("Diagnostics copied to the clipboard.")
    text = page.evaluate("navigator.clipboard.readText()")
    assert "Floe" in text and "synthetic-account-key-0123" not in text
    console.assert_clean()
    context.close()


# --------------------------------------------------------------------------- auth


def test_launch_file_opens_the_app(browser, server):
    """The real `floe serve` path: the browser opens the private file:// launch page,
    which meta-refreshes to the tokenized URL. That chain starts cross-site, so the
    SameSite=Strict cookie must still end up on the app's first request."""
    launch = cli.LaunchFile(server["url"])
    try:
        context, page, console = _new_page(browser)
        page.goto(launch.uri)
        expect(page.locator("body")).to_have_attribute("data-ready", "true")
        expect(page.locator("#profile-select")).to_be_visible()
        assert "#" not in page.url and "token" not in page.url
        assert page.evaluate("sessionStorage.getItem('floe-api-key')")
        assert _api_get(context, page, "/api/about").status == 200
        console.assert_clean()
        context.close()
    finally:
        launch.remove()


def test_direct_link_opens_app_and_replay_shows_help(browser, server):
    context, page, console = _new_page(browser)
    _open(page, server["url"])
    expect(page.locator("#profile-select")).to_be_visible()
    console.assert_clean()
    context.close()
    # Replaying the used token (fresh browser profile) lands on the help page.
    other, other_page, _ = _new_page(browser)
    other_page.goto(server["url"])
    expect(other_page.locator("body")).to_contain_text("link printed in the terminal")
    expect(other_page.locator("#profile-select")).to_have_count(0)
    other.close()


def test_single_use_link_api_key_and_new_tabs(browser, server):
    context, page, console = _new_page(browser)
    _open(page, server["url"])
    base = server["url"].split("?")[0].rstrip("/")
    # The key arrived in the fragment, moved to sessionStorage, and left the URL.
    assert "#" not in page.url and "token" not in page.url
    key = page.evaluate("sessionStorage.getItem('floe-api-key')")
    assert key and len(key) >= 40
    # The cookie alone doesn't authenticate the API; cookie + key does.
    assert context.request.get(base + "/api/about").status == 401
    wrong = context.request.get(base + "/api/about", headers={"X-Floe-Auth": key + "x"})
    assert wrong.status == 401
    assert _api_get(context, page, "/api/about").status == 200
    # A reload keeps working (sessionStorage survives it).
    page.reload()
    expect(page.locator("body")).to_have_attribute("data-ready", "true")
    # A new tab gets the key from the open tab (same-origin BroadcastChannel).
    second = context.new_page()
    second.goto(base + "/")
    expect(second.locator("body")).to_have_attribute("data-ready", "true")
    console.assert_clean()

    # With no Floe tab left, a new tab can't sign in: clear message, no API calls.
    page.close()
    second.close()
    third = context.new_page()
    third.goto(base + "/")
    expect(third.locator("[data-testid=signed-out]")).to_contain_text("terminal")
    context.close()

    # The launch link can't be replayed (e.g. from `ps` or shell history).
    other, other_page, _ = _new_page(browser)
    other_page.goto(server["url"])
    expect(other_page.locator("body")).to_contain_text("link printed in the terminal")
    assert other.request.get(base + "/api/about").status == 401
    other.close()


# --------------------------------------------------------------------------- background


def _floe_cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, FLOE_HOME=str(home),
               PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
    env.pop("QT_QPA_PLATFORM", None)
    return subprocess.run([sys.executable, "-m", "floe.cli", *args], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=60, check=False)


def _link(output: str) -> str:
    m = re.search(r"http://127\.0\.0\.1:\d+/\?token=[A-Za-z0-9_-]+", output)
    assert m, output
    return m.group(0)


@pytest.fixture
def background_server(tmp_path):
    """`floe serve --background --no-browser` on a temp FLOE_HOME; stopped (and killed
    if need be) at teardown."""
    from floe import instance

    started = _floe_cli(tmp_path, "serve", "--background", "--no-browser")
    assert started.returncode == 0, started.stdout + started.stderr
    state_file = tmp_path / "support" / "server.json"
    pid = json.loads(state_file.read_text(encoding="utf-8"))["pid"]
    try:
        yield tmp_path
    finally:
        _floe_cli(tmp_path, "stop")
        if instance.pid_alive(pid):
            instance.terminate(pid)


def test_background_server_show_links_after_closing_all_tabs(browser, background_server):
    home = background_server
    first = _link(_floe_cli(home, "show").stdout)
    context, page, console = _new_page(browser)
    _open(page, first)
    expect(page.locator("#profile-select")).to_be_visible()
    assert page.evaluate("sessionStorage.getItem('floe-api-key')")
    console.assert_clean()
    context.close()  # every Floe tab closed

    # A fresh link from `floe show` gets back in (new browser profile, no cookie)...
    second = _link(_floe_cli(home, "show").stdout)
    assert second != first
    context, page, console = _new_page(browser)
    _open(page, second)
    expect(page.locator("#profile-select")).to_be_visible()
    assert _api_get(context, page, "/api/about").status == 200
    # ...and a further link opened in another tab of the same browser keeps both working.
    third = _link(_floe_cli(home, "show").stdout)
    other = context.new_page()
    other.goto(third)
    expect(other.locator("body")).to_have_attribute("data-ready", "true")
    page.reload()
    expect(page.locator("body")).to_have_attribute("data-ready", "true")
    assert _api_get(context, page, "/api/about").status == 200
    console.assert_clean()
    context.close()

    # The used links are rejected.
    context, page, _ = _new_page(browser)
    page.goto(first)
    expect(page.locator("body")).to_contain_text("link printed in the terminal")
    page.goto(second)
    expect(page.locator("body")).to_contain_text("floe show")
    context.close()
