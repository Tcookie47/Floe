# Floe — Spec

Floe is a personal Mac desktop app for browsing and querying Iceberg tables stored on ADLS Gen2, using Nessie as the catalog and DuckDB as the query engine.

This document is the source of truth for implementation. Build it milestone by milestone (see the end). Each milestone ends in one PR with passing tests.

---

## 1. Goals and non-goals

### Goals
- Save connection profiles so the app is usable without re-entering credentials after restart.
- Browse Nessie branches and the tables on each branch.
- Preview table data and schema.
- Run ad-hoc SQL against registered tables via DuckDB.
- Ship as a downloadable `.app` (zipped) via GitHub Releases.
- Run on macOS (Apple Silicon primary).

### Non-goals (v1)
- Writing to Iceberg tables or committing to Nessie. **The app is strictly read-only.**
- Creating, merging, or deleting Nessie branches.
- Azure Key Vault / managed-identity auth for Nessie (only works on the Spark master VM, not a laptop).
- Windows/Linux support, notarization, auto-update.

---

## 2. Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 |
| GUI | PySide6 (Qt 6) |
| Query engine | DuckDB with `azure`, `iceberg`, `httpfs` extensions |
| DataFrames | pandas (DuckDB returns DataFrames for display) |
| Secrets | `keyring` (macOS Keychain backend) |
| HTTP to Nessie | stdlib `urllib` (no extra dependency needed) or `httpx` — pick one and use it consistently |
| Tests | pytest, pytest-qt for UI smoke tests |
| Test fixtures | `pyiceberg` to generate local Iceberg tables in a temp dir |
| Packaging | PyInstaller → `.app`, zipped with `ditto` |
| CI/CD | GitHub Actions on `macos-14` |

**Rule:** the `core/` package must not import Qt. All data and auth logic must be testable without a GUI.

---

## 3. Repository layout

```
floe/
  SPEC.md
  CLAUDE.md
  pyproject.toml
  src/floe/
    __main__.py            # entry point: python -m floe
    core/
      profiles.py          # Profile dataclass, JSON load/save, keyring secrets
      nessie.py            # token cache, refs, branch heads, entries, pointers
      context.py           # DuckDB connection cache, lazy view registration, query
      diagnostics.py       # logging setup, secret redaction, diagnostics bundle
      errors.py            # typed exceptions (see §7)
    ui/
      main_window.py
      profile_dialog.py
      browser_panel.py     # branch picker + table tree
      preview_tab.py
      schema_tab.py
      sql_tab.py
      results_model.py     # QAbstractTableModel over a pandas DataFrame
      workers.py           # QRunnable-based query/IO workers with cancel
  tests/
    fakes/
      fake_nessie.py       # in-process HTTP server mimicking Nessie v2 API
      iceberg_fixtures.py  # builds local Iceberg tables with pyiceberg
    test_profiles.py
    test_nessie.py
    test_context.py
    test_ui_smoke.py
  packaging/
    Floe.spec              # PyInstaller spec
    icon.icns              # optional
  .github/workflows/
    ci.yml                 # tests on every PR
    release.yml            # build + publish on tag push
```

---

## 4. Profiles and credentials

### 4.1 Storage split

- **Non-secret fields** are stored as JSON at:
  `~/Library/Application Support/Floe/profiles.json`
- **Secrets** are stored in the macOS Keychain via `keyring`, with service name `Floe` and username `"<profile_name>:<field>"`.
- Secrets must **never** be written to `profiles.json`, logs, diagnostics output, exceptions shown in the UI, or anywhere else on disk.
- Renaming a profile moves its keyring entries. Deleting a profile deletes its keyring entries.

### 4.2 Profile fields

| Field | Type | Secret | Default | Notes |
|---|---|---|---|---|
| `name` | str | no | — | Unique |
| `mode` | enum `remote` / `local` | no | `remote` | `local` reads fixture parquet from disk, no creds needed |
| `local_fixture_dir` | path | no | — | Only for `local` mode |
| **ADLS** | | | | |
| `adls_account` | str | no | — | Storage account name |
| `adls_auth` | enum `account_key` / `service_principal` | no | `account_key` | |
| `adls_account_key` | str | **yes** | — | When `adls_auth=account_key` |
| `adls_tenant_id` | str | no | — | When `service_principal` |
| `adls_client_id` | str | no | — | When `service_principal` |
| `adls_client_secret` | str | **yes** | — | When `service_principal` |
| `adls_ca_cert_file` | path | no | system bundle | CA bundle for the azure extension |
| **Nessie** | | | | |
| `nessie_uri` | str | no | — | Required; entered by the user |
| `nessie_auth` | enum `oauth2` / `none` | no | `oauth2` | `none` only for a local unauthenticated Nessie |
| `nessie_token_endpoint` | str | no | — | Azure AD token URL |
| `nessie_client_id` | str | no | — | |
| `nessie_scope` | str | no | — | |
| `nessie_client_secret` | str | **yes** | — | |
| `nessie_main_ref` | str | no | `main` | Holds shared reference content |
| `nessie_head_ttl_seconds` | int | no | `45` | |
| **Tenant scoping** | | | | |
| `shared_containers` | list[str] | no | `[]` | Containers allowed for shared content on the main ref; entered by the user |
| `shared_namespaces` | list[str] | no | `[]` | Top-level namespaces that are shared reference content, always read from the main ref and hidden on tenant branches |
| `tenant_registry_table` | str | no | — | Optional dotted key of the tenant registry table on the main ref |
| `tenant_container_map` | dict[str, str] | no | `{}` | Branch/tenant ID → ADLS container name (see §12 open questions) |
| `tenant_data_source_map` | dict[str, str] | no | `{}` | Branch/tenant ID → `data_source` value |
| **DuckDB tuning** | | | | |
| `duckdb_memory_limit` | str | no | unset | e.g. `4GB` |
| `duckdb_threads` | int | no | unset | |
| `conn_cache_max` | int | no | `16` | |
| **Safety** | | | | |
| `allow_export` | bool | no | `false` | Gates CSV export for this profile (see §9) |
| `preview_row_limit` | int | no | `1000` | |

### 4.3 Import from `.env`

The profile dialog offers **Import from .env file**. It reads a `.env` using the existing variable names and fills the form:

`ADLS_ACCOUNT`, `ADLS_ACCOUNT_KEY` (alias `AZURE_STORAGE_KEY`), `ADLS_TENANT_ID`, `ADLS_CLIENT_ID`, `ADLS_CLIENT_SECRET`, `ADLS_CA_CERT_FILE`, `NESSIE_URI`, `NESSIE_CLIENT_ID`, `NESSIE_TOKEN_ENDPOINT`, `NESSIE_SCOPE`, `NESSIE_CLIENT_SECRET`, `NESSIE_AUTH_MODE`, `NESSIE_MAIN_REF`, `NESSIE_HEAD_TTL_SECONDS`, `GOLD_REF_CONTAINER`, `DUCKDB_MEMORY_LIMIT`, `DUCKDB_THREADS`, `CONN_CACHE_MAX`.

`GOLD_REF_CONTAINER` is added to `shared_containers`. Other shared containers, `shared_namespaces` and `tenant_registry_table` are entered in the form.

Ignore `NESSIE_KEY_VAULT` and `NESSIE_SECRET_NAME` and show a note that Key Vault auth isn't supported from a laptop. Imported values go to the form only; nothing is saved until the user clicks Save.

---

## 5. Nessie client (`core/nessie.py`)

Nessie never serves data. Its only job is to answer: *which Iceberg `metadata.json` is current for this table on this branch?*

### 5.1 Token
- OAuth2 client-credentials `POST` to `nessie_token_endpoint` with `client_id`, `client_secret`, `scope`, `grant_type=client_credentials`.
- Cache the token until shortly before expiry (refresh when < 60s remaining).
- Send it as `Authorization: Bearer <token>` on every Nessie call.
- On a 401 from Nessie, refresh the token once and retry.
- `nessie_auth=none` sends no auth header.

### 5.2 Refs (branches)
- `GET /trees` lists refs. Populates the branch picker.
- If `tenant_registry_table` is set, branches whose registry row has `active = false` are still listed and selectable, but shown greyed out and marked *inactive*.
- Branch names are tenant IDs (e.g. `<tenant-branch>`). Shared reference content lives on `main`.

### 5.3 Branch head
- `GET /trees/<ref>` returns the commit hash. This hash is the **data version**.
- Cache per ref with TTL `nessie_head_ttl_seconds`.
- If a refresh fails (network blip, 5xx), keep the last known head and log a warning instead of failing. Surface a small "catalog unreachable, showing last known state" indicator in the UI.

### 5.4 Table listing
- `GET /trees/<ref>/entries` lists content keys on a ref. Handle pagination if the API returns a page token.
- Keep only `ICEBERG_TABLE` entries.
- Every Iceberg table is accessible: bronze, silver and gold alike.
- Group for the UI by key elements: the first element is the layer (Bronze, Silver, Gold, …), then any middle elements as namespaces, then the table. Layers are not a hard-coded list; any top-level namespace in the catalog appears.

### 5.5 Table pointer
- `GET /trees/<ref>/contents/<key>` returns content including `metadataLocation` and `snapshotId`.
- Key format: `silver.<namespace>.<table>` (e.g. `silver.input_layer.medical_claim`) or `gold.<table>`.
- **404** → raise `TableNotFound`.
- **`snapshotId == -1`** → raise `CorruptPointer`. This is a real current state for some tables; the UI must show it clearly per table, not crash.
- Cache pointers per `(ref, key)` with no expiry. Clear them whenever a new DuckDB connection is built for that ref.

### 5.6 Design rule
Never locate current table files by listing ADLS directories. After a catalog rebuild, directory listing picks up orphaned files and serves stale data silently. Always resolve through Nessie.

---

## 6. DuckDB context (`core/context.py`)

### 6.1 Connection cache
- One in-memory DuckDB connection per **data version**.
- Cache key: `(profile_name, mode, ref, ref_head, main_head, fixture_dir)`.
- A catalog rebuild moves the head, which changes the key, which builds a fresh connection.
- Bounded LRU, size `conn_cache_max`. Eviction drops the reference but **does not close** the connection, so an in-flight query can finish.
- Each connection has its own `threading.Lock`. DuckDB connections are not thread-safe. Different connections may run in parallel.

### 6.2 Connection setup (remote mode)
1. `INSTALL`/`LOAD` the `azure`, `iceberg`, `httpfs` extensions.
2. Apply `memory_limit` and `threads` if set.
3. Set azure transport to curl and point it at the CA bundle.
4. `CREATE SECRET adls (...)` using the service principal or the account key, per the profile.

Secret values must not appear in any logged SQL. Log the statement with values redacted.

### 6.3 Lazy view registration
Unlike the service, **do not register all sources up front.** Register a table's view the first time it is previewed, opened, or referenced in SQL.

To register table `<key>` on ref `<ref>`:
1. Resolve its pointer via Nessie (§5.5).
2. Check that `metadataLocation` is inside the expected container for that ref (from `tenant_container_map`, or `shared_containers` for shared content on main). Otherwise raise `TenantScopeError`.
3. Create the view:
   ```sql
   CREATE OR REPLACE VIEW "<view_name>" AS
   SELECT * FROM iceberg_scan('<metadataLocation>')
   [WHERE data_source = '<data_source_value>']
   ```
   - The `data_source` filter is applied when the **Tenant filter** toggle is on (default **on**) and a value is configured for the ref.
   - If the toggle is on and the table has no `data_source` column, refuse registration with `MissingDataSourceColumn`.
4. View naming: `silver_<namespace>_<table>` and `gold_<table>`. Show these names in the table tree tooltip so users know what to type in SQL.

**SQL tab resolution:** before running user SQL, find referenced view names that match known catalog tables and aren't registered yet, and register them. A simple approach is fine: match known view names as whole-word tokens in the SQL text.

**Lookups:** 16 reference lookups are read with `read_parquet` from a local directory configured per profile. They are not read from ADLS. Registering them is optional in v1; if the directory isn't set, skip them.

### 6.4 Failure handling
- If registering one table fails, show the error for that table only. The connection stays usable for others.
- If a query fails because a `.metadata.json` no longer exists (catalog rebuilt mid-flight): clear pointers, heads, and that connection; rebuild; **retry once**. Show a non-blocking notice: *"Catalog changed — reloaded and retried."*

### 6.5 Query API
```python
ctx.query(sql: str, limit: int | None) -> pandas.DataFrame
ctx.schema(view_name: str) -> list[ColumnInfo]
ctx.interrupt() -> None
```
- Queries run under the connection's lock.
- `interrupt()` calls `connection.interrupt()` and must work while a query is running on another thread.

### 6.6 Local mode
With `mode=local`, skip Nessie and ADLS entirely. Register views with `read_parquet` over files in `local_fixture_dir`, using the same view naming. The branch picker shows a single pseudo-branch `local`.

---

## 7. Errors (`core/errors.py`)

| Exception | When | UI treatment |
|---|---|---|
| `NessieUnreachable` | Connection refused / timeout to Nessie host | "Can't reach Nessie — are you on VPN?" |
| `NessieAuthError` | Token request fails or 401 after refresh | Show token endpoint + HTTP status, never the secret |
| `TableNotFound` | 404 on pointer | Mark table in tree |
| `CorruptPointer` | `snapshotId == -1` | Warning icon on table; explanatory message |
| `TenantScopeError` | Pointer outside expected container | Hard error; do not register |
| `MissingDataSourceColumn` | Tenant filter on, column absent | Offer to disable the tenant filter for this query |
| `AdlsAuthError` | ADLS rejects credentials | Show account name + auth mode |
| `QueryCancelled` | User cancelled | Quiet status-bar message |

All user-facing error text passes through the redaction filter (§10).

---

## 8. UI (`ui/`)

### 8.1 Main window
- **Top bar:** profile dropdown, **Edit profiles…** button, branch dropdown, refresh button, current head hash (short, with copy-on-click), catalog status indicator.
- **Left panel:** table tree.
  - Bronze → table, Silver → namespace → table, Gold → table (grouping per §5.4)
  - Reference (main) → the `shared_namespaces` tables, read from the main ref
  - Lookups (if configured)
  - A filter box above the tree
  - Status icons for not-found and corrupt-pointer tables
- **Main area**, with three tabs:
  - **Preview:** first `preview_row_limit` rows of the selected table.
  - **Schema:** column name, type, nullable.
  - **SQL:** editor (monospace, Cmd+Enter to run), Run and Cancel buttons, results grid below.
- **Status bar:** elapsed time, row count, truncation notice when results hit the limit.

### 8.2 Profile dialog
- A list of profiles on the left and the form on the right. Fields show or hide based on `mode`, `adls_auth`, and `nessie_auth`.
- Secret fields are password inputs. They show "(saved)" when a Keychain value exists and are never pre-filled with the actual value.
- Buttons: **New**, **Duplicate**, **Delete**, **Import from .env**, **Test connection**, **Save**.

### 8.3 Test connection
Run the steps in order, with a pass/fail line each and a clear message on the first failure:
1. TCP reachability to the Nessie host and port (distinguishes "not on VPN" from auth problems).
2. Token acquisition.
3. `GET /trees/<main_ref>`.
4. ADLS: create a throwaway DuckDB connection with the secret and read a small known object, e.g. the metadata.json of the first table found on main.

### 8.4 Threading
- All Nessie calls and queries run in `QThreadPool` workers. The UI thread never blocks on I/O.
- Changing branch or profile cancels in-flight preview work for the old selection.
- The window must stay responsive during a long query, and Cancel must work.

### 8.5 Results grid
- A `QAbstractTableModel` backed by a DataFrame.
- Supports sorting, column resize, and copying selected cells as TSV.
- Default limits:
  - Preview uses `preview_row_limit`.
  - SQL results are capped at 10,000 rows, with a status-bar notice when truncated and a per-query override field.

---

## 9. Security requirements

This tool can display healthcare data. These rules are mandatory:

- The app is read-only. No write paths to Iceberg or Nessie exist in the code.
- Secrets exist only in Keychain and in process memory.
- No result data is persisted to disk by default:
  - no result caching to disk;
  - query history (M6) stores SQL text only, never results.
- CSV export is gated by the per-profile `allow_export` flag, which defaults to off. When exporting, show the row count and destination path before writing.
- Logs contain metadata only: endpoints, status codes, timings, table keys, error types. They never contain row data or secrets.
- Repository and fixtures contain **synthetic data only**. Never commit real data, real `.env` files, real container names, or real hostnames. No environment-specific name, host, IP, account, container or table key is hard-coded in the app; the user enters all of them in the profile. `.gitignore` must include `.env*`, `*.parquet` outside `tests/`, and `profiles.json`. The GitHub Python template ignores `*.spec`, so it must also contain `!packaging/*.spec` so the PyInstaller spec is committed.

---

## 10. Diagnostics (`core/diagnostics.py`)

- Log file: `~/Library/Logs/Floe/app.log`, rotating (5 × 5 MB).
- A **redaction filter** on all log handlers and user-facing error text masks:
  - any configured secret value (exact match);
  - `Authorization: Bearer ...` headers;
  - `AccountKey=...` and `client_secret=...` patterns;
  - anything that looks like a JWT.
- Log each Nessie call (method, path, status, ms), each pointer resolution (ref, key, snapshotId, metadata filename only), each connection build (cache key with hashes shortened), and each query (duration, row count, error type).
- **Help → Copy diagnostics** copies to the clipboard:
  - app version and build commit;
  - macOS version and architecture;
  - active profile with non-secret fields only;
  - catalog status;
  - the last 200 log lines, redacted.

  The purpose is to paste this into a future dev session when something fails against the real environment.

---

## 11. Build, CI, and release

### 11.1 CI (`ci.yml`)
Runs on every push and PR on `macos-14`:
- `pip install -e ".[dev]"`
- `ruff check`
- `pytest`, with UI smoke tests run under the offscreen Qt platform (`QT_QPA_PLATFORM=offscreen`)

### 11.2 Release (`release.yml`)
Triggered on tag push `v*`:

```yaml
name: release
on:
  push:
    tags: ["v*"]
permissions:
  contents: write
jobs:
  build-mac:
    runs-on: macos-14
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -e ".[dev]" pyinstaller
      - run: pytest
        env: { QT_QPA_PLATFORM: offscreen }
      - run: pyinstaller packaging/Floe.spec
      - run: codesign --force --deep -s - dist/Floe.app
      - run: ditto -c -k --keepParent dist/Floe.app "Floe-${{ github.ref_name }}-arm64.zip"
      - uses: softprops/action-gh-release@v2
        with:
          files: Floe-*.zip
          generate_release_notes: true
```

### 11.3 Packaging requirements
- The PyInstaller spec must bundle DuckDB's native library.
- DuckDB extensions (`azure`, `iceberg`, `httpfs`) download at runtime on first use into the user's DuckDB extension directory. Make sure this works from the packaged app, and log clearly if the download fails. As a stretch goal, pre-bundle them in the `.app` and set `extension_directory`.
- Embed the version from the git tag and the short commit hash; show both in **Help → About** and in diagnostics.
- Set `CFBundleIdentifier` to `com.tcookie.floe` and keep it stable across releases so the Keychain and Application Support paths stay consistent.
- Zip with `ditto`, never plain `zip`.

### 11.4 Installing a release (for the README)
1. Download the zip from the GitHub Release and unzip it.
2. Move `Floe.app` to `/Applications`.
3. The app isn't notarized, so clear the quarantine flag once:
   `xattr -dr com.apple.quarantine /Applications/Floe.app`
   (or right-click the app → Open → Open).
4. After updating to a new version, macOS may ask to allow Keychain access. Choose **Always Allow**. This happens because each build is ad-hoc signed.

Profiles and secrets live outside the `.app`, so replacing the app keeps them.

---

## 12. Open questions (resolve before or during M3)

1. **Tenant → container mapping:** how is a tenant branch (e.g. `<tenant-branch>`) mapped to its ADLS container? If it follows a naming rule, implement the rule and keep `tenant_container_map` only as an override.
2. **Tenant → `data_source` value:** how is a branch mapped to its `data_source` value? Same approach: a rule plus an override map.
3. **Lookups directory:** should lookups be supported in v1, and where will the parquet files live on the Mac?
4. **Nessie entries API shape:** confirm the pagination and response format of `/trees/<ref>/entries` on the deployed Nessie version.

---

## 13. Testing strategy

The build environment **cannot reach** the real Nessie or ADLS and has no real credentials. All automated tests run against fakes. Real-environment testing happens manually on the Mac using release builds.

### 13.1 Fakes
- **`fake_nessie.py`:** an in-process HTTP server (stdlib `http.server` on a random port) implementing:
  - the token endpoint, which issues tokens with a configurable expiry;
  - `GET /trees`, `/trees/<ref>`, `/trees/<ref>/entries`, `/trees/<ref>/contents/<key>`;
  - bearer token validation (401 without a valid token).

  It must support these scenarios:
  - moving a branch head;
  - a 404 table;
  - a `snapshotId=-1` pointer;
  - a pointer outside the tenant container;
  - a transient 5xx;
  - token expiry.
- **`iceberg_fixtures.py`:** uses `pyiceberg` to write small synthetic Iceberg tables to a temp directory, including tables with and without a `data_source` column. The fake Nessie points to their local `metadata.json` paths.

### 13.2 Required test cases
- Profile save/load round trip; secrets go to keyring (use an in-memory keyring backend in tests) and never appear in the JSON.
- Profile rename and delete move or remove the keyring entries.
- `.env` import maps every variable in §4.3 correctly.
- Token caching, refresh near expiry, and retry-once on 401.
- The head TTL is honored, and the last known head is kept on a transient failure.
- Pointer caching and cache clearing on a new connection.
- `TableNotFound`, `CorruptPointer`, `TenantScopeError`, and `MissingDataSourceColumn` are each raised in the right scenario.
- The `data_source` filter is applied when on and omitted when off.
- A head change produces a new connection; the LRU evicts without closing a connection mid-query.
- A missing `metadata.json` mid-query triggers exactly one rebuild and retry.
- `interrupt()` cancels a long-running query from another thread.
- The redaction filter masks every secret pattern in §10.
- UI smoke: the main window opens, a profile can be created in local mode, a table previews, and SQL runs.

---

## 14. Milestones

Each milestone is one PR and must pass CI. From M2 onward, each merged milestone is tagged, which produces a release zip for manual testing.

### M1 — Core (no UI)
`profiles.py`, `nessie.py`, `context.py`, `errors.py`, `diagnostics.py`, the fakes, and all core tests from §13.2. Local mode works end to end in tests.
**Done when:** `pytest` passes and a scripted example can list tables and query one against the fake Nessie.

### M2 — Packaging and release pipeline
`pyproject.toml` entry point, PyInstaller spec, `ci.yml`, `release.yml`, and a minimal window with Help → About (version and commit). README install steps from §11.4.
**Done when:** pushing tag `v0.1.0` produces a release zip that opens on a Mac and shows the About dialog.

### M3 — Profiles and Test connection
The profile dialog (§8.2), Keychain storage, `.env` import, Test connection (§8.3), the log file, and Copy diagnostics.
**Done when:** v0.2.0 lets the user create a profile, restart the app with the profile still there, and run Test connection against the real environment with per-step results.

### M4 — Browser
The branch picker, table tree with status icons, Preview and Schema tabs, the tenant filter toggle, worker threads, and catalog status indicator.
**Done when:** v0.3.0 lets the user pick a real tenant branch, see its tables, and preview one.

### M5 — SQL editor
The SQL tab, lazy registration on reference, run and cancel, the result cap, and the rebuild retry notice.
**Done when:** v0.4.0 runs a join across two tables on a real branch and can cancel a long query.

### M6 — Polish
Gated CSV export, query history (SQL text only), keyboard shortcuts, and remembering the last profile, branch, and window layout.

### Later
Time travel via Nessie commit history (`/trees/<ref>/history`) and Iceberg snapshot selection, plus side-by-side comparison of a table across two branch heads.

---

## 15. Web app (v1, cross-platform)

The macOS Qt app is **frozen at v0.9.x**: its code stays in the repo and keeps passing its tests, but it gets no new features. New work goes into a local web app that runs on macOS, Linux and Windows and reuses `floe.core` unchanged in behaviour.

### 15.1 Shape
- Installed as a Python package (`pipx install …` or a wheel from GitHub Releases). Command: `floe serve` starts a local server and opens the default browser. `floe` with no arguments behaves like `floe serve`. `floe serve --background` starts a detached server (own session on POSIX, `DETACHED_PROCESS` on Windows; output to a 0600 file in the logs dir) that survives closing the terminal and sleep, waits until it answers, prints a link and exits. One instance per user: the server process holds an exclusive lock file (`server.lock`, 0600, in the data dir; `flock` on POSIX, `msvcrt` byte lock on Windows) for its lifetime, taken before it binds; a live instance (lock held, or state file + live PID + verified health check) makes `floe serve` print a fresh link instead of starting another (in `--background` mode the child takes the lock, and the parent reports the existing instance if the child finds it taken). `floe show [--open]` prints status and a fresh one-time link from the running server; `floe stop` shuts it down (graceful, then terminate after 10 s — only a PID that proved it is this Floe via the verified health check is ever terminated; if the server doesn't answer, nothing is killed and the user is told the process may need stopping manually). The Qt app stays available as `floe-desktop` when the `desktop` extra is installed.
- Runs **only on the user's own machine**. No shared/multi-user server.
- Backend: FastAPI + uvicorn in a new `floe.web` package (no Qt imports). Frontend: server-rendered HTML + htmx + small vanilla JS, a SQL editor (CodeMirror) and a results grid. **All frontend assets are vendored in the package**: no CDN, no Node build step, works offline.
- Feature parity with the Mac app: profiles (create, edit, duplicate, delete, import `.env`, Test connection), branch picker (inactive tenants greyed), table tree with status icons and view names, Preview, Schema, SQL (run, cancel, row limit, lazy registration, reload notice, tenant-filter offer), catalog status, export gate, SQL history, Copy diagnostics, About (version + commit).
- Long operations run in server-side worker threads; the browser polls or streams status. Cancel maps to the existing per-query `CancelToken`.

### 15.2 Persistence (survives restarts)
- Profiles JSON, SQL history, logs and UI preferences live in the OS-standard per-user directories via `platformdirs` (`user_data_dir("Floe")`, `user_log_dir("Floe")`). On macOS this resolves to the same `~/Library/Application Support/Floe` as the v0.9 app, so both share profiles. `FLOE_HOME` still overrides everything (tests).
- Secrets use `keyring` on every OS (macOS Keychain, Windows Credential Manager, Linux Secret Service). If no usable backend exists (e.g. headless Linux), secrets are read from environment variables `FLOE_SECRET__<PROFILE>__<FIELD>` (profile name upper-cased, non-alphanumerics → `_`), the UI explains this, and saving a secret shows a clear error instead of silently failing. Secrets are never written to disk by Floe.
- UI state that the Qt app kept in QSettings (last profile, last branch per profile, last tab, editor text per profile) is stored in a small JSON prefs file in the data dir. Text only, never results.

### 15.3 Local server security
- Bind to `127.0.0.1` only (configurable port, default random free port).
- A random per-launch token is required on every request. `floe serve` opens `http://127.0.0.1:<port>/?token=…`, which sets an HttpOnly, SameSite=Strict cookie and returns a small page whose same-origin `<meta http-equiv="refresh">` strips the token from the URL (a same-origin navigation, so the Strict cookie is sent even though the launch started from a cross-site `file://` page; a plain 303 was not, in Chromium). The launch token is single-use and expires two minutes after launch; the cookie stays valid for the server's lifetime. `floe show` mints further single-use links (same two-minute expiry and exchange flow; at most 5 outstanding, oldest dropped); the cookie and the API key are the same for every link of a process, so open tabs keep working. The browser is opened on a private (0600, in a 0700 temp dir) HTML file that redirects to the link, so the token never appears on a process command line; the file is deleted once the token is used, expires, or at shutdown.
- Browsers send cookies to every port on 127.0.0.1, so every `/api/*` request also needs a per-launch API key in the `X-Floe-Auth` header. It is delivered once in that refresh URL's fragment (`/#k=…`), kept in `sessionStorage` (isolated per origin, port included) and removed from the URL; new tabs get it from open Floe tabs over a same-origin `BroadcastChannel`, else show "open Floe from the terminal link".
- Request bodies are limited to 2 MB (413); at most 16 unfinished jobs (429); finished results retained in memory are capped at 2,000,000 rows overall (oldest evicted first).
- Saved secrets are only sent to the saved profile's addresses: a Test connection whose form changes `nessie_uri`, `nessie_token_endpoint` or `adls_account` must supply the affected secrets. The Nessie token endpoint and the Ask base URL must be `https://` unless the host is loopback. Log directory 0700, log files 0600.
- Instance control: a state file `server.json` in the data dir (dir 0700, file 0600, atomic write) holds `{pid, port, started_at, version, control_key}` — never a launch token, cookie or API key — and is removed by its owner on clean shutdown (only if it still holds the owner's pid, start time and control key); stale ones are removed by the next `serve`/`show`/`stop` only while the instance lock is free (checked and deleted under the lock). `/_control/health`, `/_control/login-link` and `/_control/shutdown` need `X-Floe-Control: <control_key>` (constant-time compare, else 401) and refuse any request carrying `Origin` or `Sec-Fetch-*` headers (403), so browsers can't call them; they skip the cookie/API-key checks but not the Host check. Exception: `GET /_control/health?nonce=<urlsafe>` without a key answers with `HMAC-SHA256(control_key, nonce|pid|port)`. The CLI sends a fresh nonce first and checks that proof against the state file before it ever sends the control key, so whatever else listens on a stale port never receives it. A returned login link must be exactly `http://127.0.0.1:<port>/?token=<urlsafe base64>` or it is refused; for `--open` the CLI writes the private launch file itself (the server only deletes that `floe-launch-*` temp dir once the token is used or expires); anything else from the server is printed with control characters stripped. Only a fixed route label and the outcome are logged (never the raw request path); other request-controlled strings in logs use `%r`.
- Reject requests whose `Host` header isn't `127.0.0.1:<port>`/`localhost:<port>` (DNS-rebinding defence) and state-changing requests whose `Origin` doesn't match.
- Strict CSP (self only), no third-party requests from the page.
- All §9 rules still apply: read-only, secrets never in responses (secret fields return only "saved"/"not saved"), no results persisted, logs metadata only, redaction on all error text returned to the browser.

### 15.4 Ask (AI-assisted SQL via OpenRouter)
- An **Ask** box in the SQL view: the user types what they want in plain language; Floe asks an LLM via OpenRouter's chat-completions API and puts the generated SQL **into the editor. It is never executed automatically.** The user reviews and clicks Run, and the normal read-only guard and tenant scoping apply.
- **What is sent:** the user's question; view names; column names and types of the relevant views (the selected table, views named in the question or current editor text, capped in size); a list of other view names; fixed instructions (DuckDB dialect, read-only SELECT only, views are already tenant-filtered so don't add `data_source` filters, return one SQL statement).
- **What is never sent: any row data.** No previews, query results, sample values, distinct values, statistics, file paths, container/account names, branch head hashes, or secrets. This is enforced structurally: the prompt builder in `core/ask.py` accepts only the question plus schema objects (`ColumnInfo` name/type) and has no access to sessions, DataFrames or query results; schema is obtained via `DESCRIBE`, which reads metadata only. Tests assert that synthetic row values never appear in the outgoing request body.
- The exact prompt can be previewed before sending ("What will be sent"), using the first model in the fallback chain — the model actually used may change if Ask falls back.
- Settings (global, not per profile): enabled (default off), OpenRouter API key (stored like other secrets), optional base URL override, overall timeout (default 90s). There is no user-chosen model: `generate` always walks a fixed, ordered chain of free models (`core/ask.py`'s `MODEL_CHAIN`) with one dynamic slot filled from OpenRouter's public model list (fetched at most once per 24h, cached), falling over to the next model on a timeout, connection error, HTTP 429/5xx/404, or a 400/422 that says the model itself is unavailable, and on an empty or non-SQL reply; a bad key (401/403) or no credits (402) raises immediately with no fallback. If every model fails, Ask reports that free models are busy right now, with each model's outcome logged (model id and reason only, never response bodies). A note beside the Ask box reminds users not to type patient identifiers into the question, since free providers may log prompts.
- Output handling: extract the SQL from the response (strip code fences), run it through the read-only/function checks **without executing it** and show a warning if it would be rejected. A small note under the result says which model generated the SQL and how many fallbacks it took. Timeouts and HTTP errors are shown redacted.

### 15.5 Packaging, CI and releases
- `pyproject.toml`: base dependencies are cross-platform and exclude PySide6; the `desktop` extra adds PySide6 (and pytest-qt goes in `dev-desktop`). Qt tests skip when PySide6 isn't installed.
- CI matrix: ubuntu-latest, windows-latest, macos-14 for core + web tests; Qt tests on macos-14 only.
- Releases: web package on tags `v1.*` and later (wheel + sdist attached to the GitHub Release). The Mac `.app` workflow moves to tags `desktop-v*` so it no longer runs for web releases.

### 15.6 Web milestones
- **W1 Cross-platform core:** platformdirs paths, secrets env-var fallback, optional desktop extra, CI matrix. Mac app still passes.
- **W2 Server:** FastAPI app, token/Host/Origin protection, profiles API, Test connection, sessions and background jobs with cancel.
- **W3 Browser UI:** layout, profiles page, branch picker, tree, Preview/Schema/SQL, catalog status, export, history, diagnostics, prefs.
- **W4 Packaging:** `floe serve`, release workflow for wheels, install docs for all three OSes; desktop workflow retagged.
- **W5 Ask:** OpenRouter client, prompt builder, settings, preview, generated-SQL validation.

### 15.7 Timeline (W6) — data freshness

A **Timeline** tab in the web app shows how new the data is. Metadata only: no row data is read or displayed.

- **Freshness overview** (selected branch): one row per table (same tree scoping: tenant tables from the branch, shared tables from the main ref) with last data write (current Iceberg snapshot `timestamp-ms`), age ("3 h ago"), last operation (append/overwrite/delete/replace), rows added in that snapshot, total rows (from snapshot summary when present), and when the table was last published on the branch (Nessie commit that last changed its pointer, when known). Sortable, filterable by layer/name. No staleness thresholds or colouring — just times and ages. Tables whose pointer is not found/corrupt/out of scope show their status instead.
- **Branch timeline**: Nessie commit log for the selected branch (`GET /trees/<ref>/history`, paged), newest first: commit time, message (shown prominently — pipeline commits carry useful content), author/committer, short hash. Request `fetch=ALL` to include operations; when the server returns them, show the table keys each commit touched (click a table to open its history). If operations are not returned or the parameter is rejected, degrade silently to message-only. Paging with "Load more".
- **Table history** (from the overview, the timeline, or the tree context menu): the table's Iceberg snapshot list — time, operation, rows/files added and removed, total rows — plus a small inline SVG chart of total rows over time with operations marked. Snapshot data comes from the table's current `metadata.json` (resolved through Nessie, with the normal tenant container check), parsed server-side; cache by metadata location (immutable).
- All reads go through the existing scoping rules (pointer resolution, container check, restricted file access). History/snapshot endpoints never expose file paths, container or account names beyond what the UI already shows (table keys, view names).
- Reads happen in background jobs with cancel; results are in memory only.
