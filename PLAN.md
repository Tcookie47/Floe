# Floe — Build Plan

Phased plan for implementing [SPEC.md](SPEC.md). SPEC.md stays the source of truth; this file sequences the work, calls out decisions, and lists risks. Each phase ends in one PR with green CI. Tick boxes as work lands.

---

## Guiding decisions

| Decision | Choice | Why |
|---|---|---|
| HTTP client for Nessie | stdlib `urllib` | Spec allows either; no extra dependency, and it pairs naturally with the stdlib `http.server` fake. |
| Layer boundary | `floe.core` never imports Qt | Enforced by a test that imports every `core` module with `PySide6` blocked. |
| Storage paths | One `paths.py` helper in `core` returning App Support / Logs dirs, overridable by env var (`FLOE_HOME`) | Tests and CI never touch the real `~/Library`. |
| Keyring in tests | In-memory `keyring` backend set in `conftest.py` | Never touches the real Keychain. |
| CI timing | Move `ci.yml` into Phase 0 (spec lists it in M2) | Spec requires every milestone PR to pass CI, so CI must exist from the first PR. |
| Container check abstraction | `container_of(location) -> str` that understands `abfss://<container>@<acct>.dfs.core.windows.net/...` **and** local fixture paths (`<root>/<container>/...`) | Lets `TenantScopeError` be tested against local pyiceberg fixtures. |

---

## Phase 0 — Scaffold (small PR, do first)

Goal: an installable, lint-clean, test-running skeleton.

- [x] `pyproject.toml` (Python 3.12, `src/` layout, deps: `PySide6`, `duckdb`, `pandas`, `keyring`; `dev` extra: `pytest`, `pytest-qt`, `pyiceberg[pyarrow]`, `ruff`). Script entry `floe = floe.__main__:main`.
- [x] Package skeleton per SPEC §3 with empty modules; `src/floe/__init__.py` exposing `__version__` / `__commit__` (placeholder until M2).
- [x] `.gitignore` additions required by SPEC §9: `.env*`, `profiles.json`, `*.parquet` with `!tests/**/*.parquet`, and `!packaging/*.spec` (the template currently ignores `*.spec` and only `.env`).
- [x] `CLAUDE.md`: layer rule, read-only rule, secrets rule, "synthetic data only", how to run tests (`QT_QPA_PLATFORM=offscreen pytest`).
- [x] `.github/workflows/ci.yml` on `macos-14`: install, `ruff check`, `pytest` (offscreen).
- [x] `tests/conftest.py`: in-memory keyring, temp `FLOE_HOME`, and the "core has no Qt import" test.

**Done when:** CI is green on an essentially empty test suite.

---

## Phase 1 — Core, no UI (SPEC M1)

Largest phase; split into three stacked PRs so each stays reviewable.

### 1a. Errors, diagnostics, profiles
- [x] `errors.py`: `FloeError` base + the eight exceptions from SPEC §7, each carrying structured, non-secret fields (e.g. `NessieAuthError(endpoint, status)`).
- [x] `diagnostics.py`:
  - `RedactionFilter` (logging filter) + `redact(text)` for UI strings. Masks registered secret values (exact), `Bearer …`, `AccountKey=…`, `client_secret=…`, JWT-shaped tokens.
  - `register_secret(value)` — profiles call this when a secret is loaded into memory.
  - `setup_logging()` → rotating file handler (5 × 5 MB) under the Logs dir; redaction on every handler.
  - `build_diagnostics(profile, catalog_status)` → text bundle (UI-free; Qt only does the clipboard).
- [x] `profiles.py`:
  - `Profile` dataclass with every field/default from SPEC §4.2; secret fields excluded from `to_json()`.
  - `ProfileStore`: load/save JSON, `get_secret` / `set_secret` via keyring (`service="Floe"`, `user="<name>:<field>"`), `rename` (moves keyring entries), `delete` (removes them).
  - `parse_env_file(path) -> (partial_profile_dict, secrets_dict, notes)` implementing SPEC §4.3 incl. the `AZURE_STORAGE_KEY` alias and the Key Vault note.
- Tests: round trip; secrets never in JSON; rename/delete keyring behaviour; every `.env` variable mapped; redaction of each pattern.

### 1b. Fake Nessie + Nessie client
- [x] `tests/fakes/fake_nessie.py`: `ThreadingHTTPServer` on port 0, state held in a mutable object so tests can script scenarios: token endpoint with configurable expiry, `/trees`, `/trees/<ref>`, `/trees/<ref>/entries` (with paging via `pageToken`), `/trees/<ref>/contents/<key>`; bearer validation; knobs for move-head, 404 table, `snapshotId=-1`, out-of-container pointer, transient 5xx (N times), token expiry, and request counters.
- [x] `core/nessie.py` — `NessieClient(profile, secrets)`:
  - Token cache (refresh when < 60 s left), 401 → refresh once and retry, `nessie_auth=none` sends no header.
  - `list_refs()`, `head(ref)` with TTL cache + last-known-good on failure (exposes `catalog_status`: `ok` / `stale`), `list_tables(ref)` (paged, `ICEBERG_TABLE` only, grouped Silver/Gold), `pointer(ref, key)` with no-expiry cache and `clear_pointers(ref)`.
  - Connection refused / timeout → `NessieUnreachable`; token failure → `NessieAuthError`.
  - Logs method, path, status, ms — never headers or bodies.
  - Pure helpers: `view_name(key)` (`silver_<ns>_<table>`, `gold_<table>`), key ↔ URL-path encoding.
- Tests: all Nessie items in SPEC §13.2.

### 1c. Iceberg fixtures + DuckDB context
- [x] `tests/fakes/iceberg_fixtures.py`: pyiceberg `SqlCatalog` (sqlite) writing tiny synthetic tables into `tmp/<container>/…`: a silver table with `data_source`, one without, a gold table; plus parquet files for local mode. Returns `{key: metadata_location}` to seed the fake Nessie.
- [x] `core/context.py`:
  - `ConnectionCache` — bounded LRU keyed by `(profile_name, mode, ref, ref_head, main_head, fixture_dir)`; eviction drops the ref without closing.
  - `Context` — owns one DuckDB connection + `threading.Lock`; remote setup (extensions, limits, curl transport + CA, `CREATE SECRET` logged with values redacted); local setup (no extensions beyond what's needed).
  - `register(key, tenant_filter=True)`: pointer → container check (`TenantScopeError`) → `data_source` column check (`MissingDataSourceColumn`) → `CREATE OR REPLACE VIEW`. Per-table failure doesn't poison the connection.
  - `resolve_sql_references(sql)`: whole-word match of known view names, register the missing ones.
  - `query(sql, limit)`, `schema(view)`, `interrupt()`.
  - Missing-`metadata.json` detection → clear pointers/heads/connection, rebuild, retry once, return a "reloaded" flag for the UI notice.
  - Tenant rules: `container_for(ref)` / `data_source_for(ref)` = override map → naming rule (placeholder until open question 1–2 answered) → error.
- [x] `examples/list_and_query.py`: starts the fake Nessie + fixtures, lists tables, queries one. (This is M1's "done when" script.)
- Tests: all context items in SPEC §13.2, including `interrupt()` from another thread on a deliberately slow query (e.g. a large `range()` cross join).

**Done when:** `pytest` green, example script runs, local mode works end to end.

---

## Phase 2 — Packaging & release pipeline (SPEC M2)

- [x] `src/floe/__main__.py` → `QApplication` + minimal `MainWindow` with **Help → About** (version + commit).
- [x] Version stamping: `release.yml` writes `src/floe/_build_info.py` from `github.ref_name` + short SHA before PyInstaller; dev builds fall back to `git describe` / `"dev"`.
- [x] `packaging/Floe.spec`: onedir `.app` bundle, collect DuckDB native lib (`collect_dynamic_libs("duckdb")`), `CFBundleIdentifier=com.tcookie.floe`, optional icon.
- [x] `release.yml` exactly per SPEC §11.2 (+ the build-info step).
- [x] Runtime check: on first connection, log clearly if extension install fails (the packaged app downloads to `~/.duckdb/extensions`).
- [x] README install section from SPEC §11.4.
- [x] Smoke test: main window opens offscreen and About shows the version.

**Done when:** tag `v0.1.0` → release zip that opens on a Mac and shows About. *(Manual check on your Mac.)*

---

## Phase 3 — Profiles & Test connection (SPEC M3)

SPEC §12 questions are resolved; see *Resolved open questions* below. Phase 1 already implements the real rules, so this phase only has to confirm them against the live environment.

- [x] `ui/profile_dialog.py`: list + form, conditional fields by `mode` / `adls_auth` / `nessie_auth`, password inputs showing "(saved)", New / Duplicate / Delete / Import from .env / Save.
- [x] `core/connection_test.py` (UI-free): the four ordered steps of SPEC §8.3, yielding per-step results; the dialog renders them from a worker.
- [x] Wire `setup_logging()` at startup; **Help → Copy diagnostics**.
- [x] Tests: dialog creates/saves a local profile; test-connection steps against fake Nessie (pass, not-on-VPN, bad token).

**Done when:** v0.2.0 — profile survives restart; Test connection against the real environment shows per-step results. Paste Copy-diagnostics output into the next session if anything fails.

---

## Phase 4 — Browser (SPEC M4)

- [x] `ui/workers.py`: `QRunnable` worker with signals + cancellation token; generation counter so stale results from an old branch/profile are dropped.
- [x] Top bar: profile dropdown, Edit profiles…, branch dropdown (inactive tenants greyed out), refresh, short head hash (click to copy), catalog-status indicator.
- [x] `ui/browser_panel.py`: filter box, Bronze / Silver / Gold (grouped by key elements) + Reference (main); status icons for not-found / corrupt; tooltip with SQL view name.
- [x] `ui/results_model.py`: DataFrame-backed model, sorting, copy selection as TSV.
- [x] `ui/preview_tab.py`, `ui/schema_tab.py`; tenant-filter toggle (default on) with the "disable for this query" offer on `MissingDataSourceColumn`.
- [x] Tests: UI smoke — local profile, table tree populated, preview renders rows, schema tab shows columns.

**Done when:** v0.3.0 — pick a real tenant branch, see tables, preview one.

---

## Phase 5 — SQL editor (SPEC M5)

- [x] `ui/sql_tab.py`: monospace editor, Cmd+Enter, Run / Cancel, per-query row-limit override (default 10,000), results grid.
- [x] Lazy registration via `resolve_sql_references`, per-table registration errors surfaced inline.
- [x] Cancel → `ctx.interrupt()` → quiet `QueryCancelled` status message.
- [x] Status bar: elapsed, row count, truncation notice; non-blocking "Catalog changed — reloaded and retried."
- [x] Tests: SQL runs a join across two local tables; cancel of a long query from the UI.

**Done when:** v0.4.0 — join across two tables on a real branch; long query cancels.

---

## Phase 6 — Polish (SPEC M6)

- [x] CSV export gated by `allow_export`, with row count + destination confirmation.
- [x] Query history (SQL text only) in App Support.
- [x] Keyboard shortcuts; remember last profile, branch, window geometry/splitters (`QSettings`).
- [ ] Optional: lookups via `read_parquet` from a per-profile directory (depends on §12 Q3).

---

## Web app (SPEC §15) — v1

The Mac app is frozen at v0.9.x. Phases W1–W5 below build the cross-platform local web app.

- [x] **W1 Cross-platform core:** `platformdirs` in `core/paths.py`, `FLOE_SECRET__…` env fallback, PySide6 moved to a `desktop` extra, CI matrix (Linux/Windows/macOS).
- [x] **W2 Server:** `floe.web` FastAPI app, 127.0.0.1 + token cookie + Host/Origin checks, profiles/secrets API, Test connection, session + job manager with cancel.
- [x] **W3 Browser UI:** vendored htmx/CodeMirror, profiles page, branch picker, tree, Preview/Schema/SQL, status, export gate, history, diagnostics, prefs JSON.
- [x] **W4 Packaging:** `floe serve` CLI, wheel release on `v1.*`, Mac release moved to `desktop-v*`, README install per OS.
- [x] **W5 Ask:** `core/ask.py` prompt builder (schema only, no rows — tested), OpenRouter client, settings, "What will be sent" preview, validate but never run.

- [ ] **W6 Timeline:** freshness overview, Nessie branch commit timeline (messages, touched tables when available), per-table Iceberg snapshot history with chart (SPEC §15.7).

## Later
Nessie commit history / Iceberg snapshot time travel; side-by-side table comparison across branch heads.

---

## Resolved open questions (SPEC §12 and follow-ups)

**No environment-specific value is hard-coded in the app:** no IP, host, storage account, container, tenant branch, namespace or table key. The user enters all of them in the profile. Defaults are empty or generic (`main` for the main ref, as Nessie's own default). Tests use synthetic names only (`eg-test1`, `ref-a`, …).

1. **Branch → container:** the rule is "container = branch name". Shared content on the main ref may live in several containers, listed in the profile field `shared_containers`.
2. **Branch → `data_source`:** the rule is "value = branch name". Apply the filter on tenant branches only, never to shared content read from the main ref (it has no `data_source` column). `MissingDataSourceColumn` stays as a guard for tenant tables only.
3. **Tenant registry:** optional profile field `tenant_registry_table` (a dotted key on the main ref, one row per tenant with container, branch, active …). If set, it is the preferred mapping source, loaded lazily after the first storage-capable connection. Otherwise, or if it can't be read, fall back to the branch-name rules. Rows with no branch are ignored.
4. **Shared namespaces:** profile field `shared_namespaces` lists the top-level namespaces that are shared reference content. On tenant branches, entries under those namespaces are stale copies and are always hidden. The tree shows one **Reference (main)** group whose views resolve against the main ref's head, so they work in SQL from any branch. `main_head` is already part of the connection cache key.
5. **Namespaces and view names:** keys are element lists of any length. View name is the elements joined with `_`. The URL path uses the dotted key. The tree groups by the first element, not a hard-coded list of layers.
6. **Entries API (Nessie REST v2):** response `{token, entries, effectiveReference, hasMore}`. Loop with `max-records` / `page-token` while `hasMore`. The pointer comes from `/contents/<dotted.key>` → `content.metadataLocation` / `content.snapshotId`.
7. **Paths:** `abfss://<container>@<account>.dfs.core.windows.net/.../metadata/*.metadata.json`. The container is the part before `@`. Never derive remote paths from keys; always use `metadataLocation`.
8. **Storage auth:** the account key via `CREATE SECRET (TYPE azure, PROVIDER config, CONNECTION_STRING ...)` is the primary path; service principal is kept as an option. One secret per profile.
9. **Token:** standard Azure AD v2 client-credentials, form-encoded. The scope is used verbatim from the profile.
10. **CA bundle:** default on macOS is `/etc/ssl/cert.pem` (fall back to certifi's bundle) when using curl transport. Overridable in the profile.
11. **Local mode layout:** `<root>/<container>/<key path>/*.parquet`, mirroring the lake. The branch picker lists the container directories under `<root>`.
12. **Lookups:** deferred past v1. When added, the directory and the view-name → file mapping come from the profile.
13. **Bronze:** every table is accessible (bronze, silver, gold). Bronze is no longer a non-goal.
14. **Inactive tenants:** branches marked inactive in the registry stay listed and selectable, greyed out and labelled *inactive*.

---

## Risks & things to verify early

1. **DuckDB `iceberg_scan` on local pyiceberg output.** Verify in Phase 1c right away (metadata path format, `file://` vs plain paths). This underpins every context test.
2. **Extension downloads in CI.** `iceberg` must be fetched by `macos-14` runners; if flaky, cache `~/.duckdb/extensions` in CI.
3. **Detecting "metadata.json gone".** Match DuckDB's IO error text narrowly and test it by deleting the fixture's metadata file mid-sequence.
4. **Nessie v2 key encoding.** Namespaced keys containing dots need the v2 escaping rules; confirm against the deployed version (§12 Q4) and cover in the fake.
5. **`interrupt()` race.** DuckDB interrupt only affects a running statement; tests must wait until the query has actually started (use an event) to avoid flakiness.
6. **Keychain prompts after each release** (ad-hoc signing) — documented in the README, not a bug.
7. **Local dev here is Linux / Python 3.11**; CI and target are macOS / 3.12. Keep core code platform-neutral and pin `requires-python = ">=3.12"` but test locally where possible.
