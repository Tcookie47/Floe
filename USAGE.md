# Floe — Usage Guide (web app)

Floe is a personal, read-only browser and query tool for Apache Iceberg tables
on ADLS Gen2, catalogued by Nessie and queried with DuckDB. It runs entirely
on your own machine. This guide covers the **web app** (`floe serve`); the
frozen Qt desktop app is documented separately in the README.

---

## 1. Install & start

**Prerequisites:** Python 3.12+ and [pipx](https://pipx.pypa.io/). See the
README for OS-specific notes.

Install the latest release wheel:

```
pipx install https://github.com/Tcookie47/Floe/releases/download/vX.Y.Z/floe-X.Y.Z-py3-none-any.whl
```

or track `main`:

```
pipx install "git+https://github.com/Tcookie47/Floe@main"
```

Then run:

```
floe serve
```

**What you'll see:** Floe binds to `127.0.0.1` on a random free port (unless
you pass `--port`), prints a one-time link of the form
`http://127.0.0.1:<port>/?token=…`, and opens it in your default browser.
In this (foreground) mode, keep the terminal open — closing it, or pressing
**Ctrl+C**, stops the server.

### Background mode: `floe serve --background`

```
floe serve --background
```

starts Floe as a detached process that keeps running after you close the
terminal (and across laptop sleep). It waits until the server answers, prints
a one-time link (and opens it unless you add `--no-browser`), then returns:

```
Floe is running in the background (PID 12345). Use `floe show` to get a new link, `floe stop` to stop it.
```

Only one Floe runs at a time: if one is already running, `floe serve` (with
or without `--background`) doesn't start another — it says so and prints a
fresh link, like `floe show`. The server's output goes to
`server-output.log` in Floe's logs directory (owner-only, never contains a
link or token).

### `floe show` — get a fresh link

```
floe show          # print a new one-time link
floe show --open   # ...and open it in your default browser
```

prints `Floe <version> is running (PID …, since …, port …)` and a **new**
one-time link. Like every Floe link it works **once, within two minutes**;
run `floe show` again whenever you need another. `--open` hands the browser a
private (owner-only) file that redirects to the link, so the token never
appears on a command line. If Floe isn't running, `floe show` says
`Floe isn't running. Start it with floe serve --background.` and exits with
status 1. `floe show` works for a foreground `floe serve` too (from another
terminal).

### `floe stop`

```
floe stop
```

asks the running Floe (foreground or background) to shut down cleanly
(running queries are cancelled), waits up to 10 seconds, and terminates it if
it didn't exit.

### `floe serve --no-browser`

Use this when:
- your browser didn't open automatically,
- you're on a remote desktop or an SSH session without a browser tunnel and
  want to open the link from a browser on the same machine some other way,
- or you simply want to choose which browser opens it.

With `--no-browser`, Floe only prints the link — it does not try to launch a
browser. **Copy the printed link into the address bar within two minutes**:
the launch token expires after 2 minutes, and the link is **single-use** —
once a browser has loaded it, the token is consumed (the private redirect
file is deleted immediately after use, on expiry, or at shutdown). If it
expired, `floe show` prints a new one.

### Other flags

| Flag | Meaning |
|---|---|
| `--port N` | Listen on port `N` instead of a random free port. |
| `--host 127.0.0.1` | Must be `127.0.0.1` or `localhost` — Floe refuses any other value (it serves health data and only runs locally). |
| `--no-browser` | Don't open a browser; just print the link. |
| `--background` | Run detached from the terminal (see above). |
| `--version` | Print the version and commit, then exit. |

Running `floe` with no subcommand behaves exactly like `floe serve`.

### Sleep, closed tabs and reopening

- Laptop sleep doesn't sign you out: when the machine wakes, open Floe tabs
  keep working as long as the server is still running (the session cookie
  lasts for the server's lifetime; each tab keeps its API key in
  `sessionStorage`).
- A new tab opened by hand picks up the session from an open Floe tab.
- After closing **every** Floe tab (or in another browser), run
  `floe show --open` (or `floe show` and paste the link) to get back in.
  Tabs you already have open keep working when you open a new link.
- `floe stop` (or **Ctrl+C** for a foreground `floe serve`) stops the server;
  after that, start it again with `floe serve --background`.

### Troubleshooting "Open Floe from the link printed in the terminal"

If a browser tab shows this message instead of the app, it means the tab has
no valid token/API key (a new tab opened by hand, a bookmark, or a token that
already expired or was used). Fixes:
- Reuse a tab that is already open on Floe (new tabs pick up the session
  automatically over a same-origin broadcast, as long as one Floe tab is
  still open).
- Otherwise run `floe show --open` for a fresh link. If that says Floe isn't
  running, start it with `floe serve --background`.

---

## 2. Profiles

A **profile** stores everything needed to connect to one Nessie/ADLS
environment (or a local fixture directory). Open **Profiles…** in the top
bar to manage them.

### Add a profile

1. Click **Profiles…** in the top bar.
2. Click **New**.
3. Fill in the form (grouped as General / Storage (ADLS) / Nessie / Tenant
   scoping / DuckDB / Safety — see the field reference below). Fields that
   don't apply to your `mode` / `adls_auth` / `nessie_auth` choice are hidden
   automatically.
4. Click **Test connection** to check it (see §4).
5. Click **Save**.

### Import from a `.env` file

Click **Import .env…**, pick a `.env` file (must be ≤ 256 KB). Floe reads it
and fills the form — **nothing is saved until you click Save**. The mapping:

| `.env` variable | Profile field | Notes |
|---|---|---|
| `ADLS_ACCOUNT` | `adls_account` | |
| `ADLS_ACCOUNT_KEY` (alias `AZURE_STORAGE_KEY`) | `adls_account_key` (secret) | Either name works; sets `adls_auth = account_key`. |
| `ADLS_TENANT_ID` | `adls_tenant_id` | |
| `ADLS_CLIENT_ID` | `adls_client_id` | |
| `ADLS_CLIENT_SECRET` | `adls_client_secret` (secret) | If set (with no account key), `adls_auth = service_principal`. |
| `ADLS_CA_CERT_FILE` | `adls_ca_cert_file` | |
| `NESSIE_URI` | `nessie_uri` | |
| `NESSIE_CLIENT_ID` | `nessie_client_id` | |
| `NESSIE_TOKEN_ENDPOINT` | `nessie_token_endpoint` | |
| `NESSIE_SCOPE` | `nessie_scope` | |
| `NESSIE_CLIENT_SECRET` | `nessie_client_secret` (secret) | |
| `NESSIE_AUTH_MODE` | `nessie_auth` | |
| `NESSIE_MAIN_REF` | `nessie_main_ref` | |
| `NESSIE_HEAD_TTL_SECONDS` | `nessie_head_ttl_seconds` | Parsed as an integer. |
| `GOLD_REF_CONTAINER` | `shared_containers` | Appended to the list (existing entries kept, duplicates skipped). |
| `DUCKDB_MEMORY_LIMIT` | `duckdb_memory_limit` | |
| `DUCKDB_THREADS` | `duckdb_threads` | Parsed as an integer. |
| `CONN_CACHE_MAX` | `conn_cache_max` | Parsed as an integer. |

`NESSIE_KEY_VAULT` and `NESSIE_SECRET_NAME` are **ignored** — Floe shows a
note that Key Vault auth isn't supported from a laptop; enter the Nessie
client secret directly instead. Imported secrets are held in server memory
for 10 minutes (tied to that import) and are only written to the keyring
when you click **Save**.

### Duplicate / Rename / Delete

- **Duplicate**: select a profile, click **Duplicate**, give the copy a new
  name. Its saved secrets are copied too.
- **Rename**: change the **Name** field and click **Save** — Floe moves its
  keyring entries to the new name.
- **Delete**: select a profile, click **Delete**. This also deletes its
  saved secrets and its SQL history.

### Where profiles and secrets are stored

Non-secret fields live in `profiles.json` in your OS's standard per-user
app-data directory (via `platformdirs`), the same directory the frozen Qt
desktop app uses on macOS:

| OS | Directory |
|---|---|
| macOS | `~/Library/Application Support/Floe` |
| Linux | `~/.local/share/Floe` (or `$XDG_DATA_HOME/Floe`) |
| Windows | `%LOCALAPPDATA%\Floe` |

Set the environment variable `FLOE_HOME` to override this (mainly for
tests/dev) — it redirects both the app-support and logs directories.

Secrets (`adls_account_key`, `adls_client_secret`, `nessie_client_secret`,
and the OpenRouter API key for Ask) are stored in your OS keyring — macOS
Keychain, Windows Credential Manager, or the Linux Secret Service — **never**
in `profiles.json`.

### Headless Linux (no Secret Service running)

When no usable OS keyring exists, Floe reads secrets from environment
variables instead, named:

```
FLOE_SECRET__<PROFILE>__<FIELD>
```

`<PROFILE>` and `<FIELD>` are the profile name and field name, upper-cased,
with every character that isn't a letter or digit turned into `_`.

**Example:** a profile named `eg-test1` needing its `account_key` set:

```
export FLOE_SECRET__EG_TEST1__ACCOUNT_KEY="..."
```

The profile form shows a note when it detects no usable keyring, naming the
exact variable(s) to set for that profile's secret fields, and attempting to
save a secret from the UI in that state shows a clear error instead of
silently failing.

---

## 3. Profile field reference

Secret fields are password inputs that are **never pre-filled**: they show
placeholder text `(saved)` when a value already exists in the keyring;
typing a new value replaces it on Save, and each has a **Clear** button that
removes it on Save.

### General

| Label | Field | Required / shown when | Default | Meaning | Example |
|---|---|---|---|---|---|
| Name | `name` | Required always | — | Unique profile name. May not contain `:` or control characters. | `eg-my-profile` |
| Mode | `mode` | Always | `remote` | `remote` connects to Nessie/ADLS; `local` reads fixture parquet from disk (no credentials needed). | |
| Local fixture directory | `local_fixture_dir` | Required when Mode = local | — | Root directory of local fixture data (container subdirectories, each holding table directories of `.parquet` files). | `/path/to/fixtures` |

### Storage (ADLS) — hidden in local mode

| Label | Field | Required / shown when | Default | Meaning | Example |
|---|---|---|---|---|---|
| Storage account | `adls_account` | remote mode | — | ADLS Gen2 storage account name. | `mystorageacct` |
| Auth mode | `adls_auth` | remote mode | `account_key` | `account_key` or `service_principal`. | |
| Account key | `adls_account_key` (secret) | shown when Auth mode = account_key | — | Storage account key. Write-only; shows "(saved)". | |
| Tenant ID | `adls_tenant_id` | shown when Auth mode = service_principal | — | Azure AD tenant ID. | `<tenant-id>` |
| Client ID | `adls_client_id` | shown when Auth mode = service_principal | — | Service principal client ID. | |
| Client secret | `adls_client_secret` (secret) | shown when Auth mode = service_principal | — | Service principal secret. Write-only. | |
| CA cert file | `adls_ca_cert_file` | remote mode | macOS/Linux: system bundle (or certifi's, if no system bundle is found); Windows: the Windows certificate store | CA bundle (PEM) for the azure/curl transport. Setting it forces the curl transport with this bundle on every OS. | `/path/to/ca-bundle.pem` |

### Nessie — hidden in local mode

| Label | Field | Required / shown when | Default | Meaning | Example |
|---|---|---|---|---|---|
| Nessie URI | `nessie_uri` | remote mode | — | Base URL of the Nessie API. | `https://nessie.example.internal/api/v2` |
| Auth mode | `nessie_auth` | remote mode | `oauth2` | `oauth2` (client-credentials token) or `none` (unauthenticated). | |
| Token endpoint | `nessie_token_endpoint` | shown when Auth mode = oauth2 | — | OAuth2 token URL. Must be `https://` unless it points at this machine. | `https://auth.example.internal/oauth2/token` |
| Client ID | `nessie_client_id` | shown when Auth mode = oauth2 | — | OAuth2 client ID. | |
| Scope | `nessie_scope` | shown when Auth mode = oauth2 | — | OAuth2 scope string. | |
| Client secret | `nessie_client_secret` (secret) | shown when Auth mode = oauth2 | — | OAuth2 client secret. Write-only. | |
| Main ref | `nessie_main_ref` | remote mode | `main` | The branch holding shared reference content. | `main` |
| Head TTL (seconds) | `nessie_head_ttl_seconds` | remote mode | `45` | How long a branch's cached head hash is trusted before re-checking. | |

### Tenant scoping — shown in both modes

| Label | Field | Required? | Default | Meaning | Example |
|---|---|---|---|---|---|
| Shared containers (one per line) | `shared_containers` | Optional | `[]` | Containers holding shared reference content on the main ref. | `shared-ref`, `registry-ref` |
| Shared namespaces (one per line) | `shared_namespaces` | Optional | `[]` | Top-level namespaces that are shared reference content: always read from the main ref, hidden on tenant branches, shown under "Reference (main)" in the table tree. | `gold_ref`, `registry` |
| Tenant registry table | `tenant_registry_table` | Optional | — | Dotted key of a table on the main ref listing tenants (branch, container, active, data_source columns). | `registry.tenants` |
| Tenant → container (`key=value` per line) | `tenant_container_map` | Optional | `{}` | Override: branch name → ADLS container name. | `eg-tenant1=shared-ref` |
| Tenant → data_source (`key=value` per line) | `tenant_data_source_map` | Optional | `{}` | Override: branch name → `data_source` column value. | `eg-tenant1=eg-tenant1` |

**Tenant scoping concepts:**

- **Branch = tenant.** Each Nessie branch (other than the main ref) is one
  tenant. The **branch picker** in the top bar lists every branch.
- **Container = branch name, by default.** A tenant branch's ADLS container
  is resolved in this order: the `tenant_container_map` override for that
  branch name → the tenant registry table's `container` column (if
  configured) → the branch name itself.
- **`data_source` = branch name, by default.** The value used to filter a
  tenant's rows is resolved the same way: `tenant_data_source_map` override
  → the registry's `data_source` column → the branch name itself.
- **Shared containers** hold content (e.g. reference/lookup tables) that
  every tenant branch may read, always resolved from the main ref.
- **Shared namespaces** are top-level namespaces (like `gold_ref` or
  `registry`) whose tables are always read from the main ref and hidden from
  a tenant branch's own listing — they appear separately under **Reference
  (main)** in the table tree.
- **Tenant registry table** — an optional table on the main ref (e.g.
  `registry.tenants`) listing every tenant branch, with columns such as
  `branch`, `container`, `data_source`, and `active`. When configured, the
  **branch picker lists inactive tenants too**, greyed out but still
  selectable, instead of relying on branch-name guessing alone. If the
  registry can't be read, Floe falls back to the branch-name rules and
  retries the registry a few seconds later.
- **Main ref** (`nessie_main_ref`) is the branch that holds shared/reference
  content and (if configured) the tenant registry.

**Escape hatches and knobs:**

- **`restrict_file_access`** (Safety group) — when on (the default), DuckDB's
  file access on a connection is locked down to just that ref's container
  plus the shared containers (`allowed_directories`, `enable_external
  access = false`, `lock_configuration = true`); this is *in addition to* the
  SQL-level table-function blocklist, which always applies regardless of
  this setting. Turn it off only if legitimate reads fail with "Access
  denied", and only if you understand you're removing a defence-in-depth
  layer (not the primary read-only/tenant-scoping check).
- **`allow_export`** (Safety group) — gates the **Export CSV…** action; off
  by default. With it off, export does nothing (a message explains it's
  disabled for the profile).
- **`preview_row_limit`** (Safety group, default `1000`) — rows shown in the
  Preview tab.
- **DuckDB tuning** (DuckDB group): `duckdb_memory_limit` (e.g. `4GB`,
  unset by default), `duckdb_threads` (unset by default), and
  `conn_cache_max` (default `16`) — the maximum number of DuckDB connections
  (one per distinct data version) kept in the in-memory cache; evicting a
  connection never closes an in-flight query.

---

## 4. Test connection

Click **Test connection** in the profile form (works on unsaved changes
too). Steps run **in order, stopping at the first failure**:

**Local mode:** a single step checks that `local_fixture_dir` exists and
contains at least one container subdirectory.

**Remote mode**, in order:

1. **TCP reachability** — opens a raw TCP connection to the Nessie host/port
   parsed from `nessie_uri`. A failure here usually means **you're not on
   VPN** (or the host/port is wrong).
2. **Token acquisition** — requests an OAuth2 token (skipped, shown as "n/a",
   when Auth mode = none). A failure usually means the **client ID/secret,
   scope, or token endpoint** is wrong.
3. **`GET /trees/<main ref>`** — fetches the main ref's head. A failure here
   usually means the **Nessie ref name is wrong**, or the token doesn't have
   access to it.
4. **ADLS read** — builds a throwaway DuckDB connection with your storage
   credential and reads the `metadata.json` of the first table found on the
   main ref, as plain text. A failure usually means the **storage
   account key/client secret is wrong**, or — if the message says the HTTPS
   certificate couldn't be verified — see *HTTPS certificate errors* in
   Troubleshooting.

Each step shows a pass/fail line with its message and elapsed time.

---

## 5. Using Floe

- **Branch picker** (top bar) — lists every Nessie branch (or, in local
  mode, every subdirectory of the fixture root that isn't a shared
  container). Inactive tenants (per the tenant registry) are listed greyed
  out but still selectable.
- **Catalog status pill** (top bar) — shows whether the last catalog request
  succeeded; if Nessie is briefly unreachable, Floe keeps showing the last
  known head with a "stale" indication rather than failing outright.
- **Head hash** (top bar button) — the current branch's head commit hash,
  shortened; click it to copy the full hash.
- **Table tree** (left sidebar) — tables grouped by layer (bronze / silver /
  gold, then any other top-level namespace), with shared tables shown under
  **Reference (main)**. A filter box above the tree narrows by name (**Ctrl/
  Cmd+F** focuses it). Status icons flag tables that are not found, have a
  corrupt pointer, are out of tenant scope, or are missing the
  `data_source` column. Hovering a row shows its SQL view name and dotted
  key. **Double-click** a table to insert its view name into the SQL editor.
- **View names** — a table's key `layer.namespace.table` becomes the SQL
  view name `layer_namespace_table`, e.g. `silver.input_layer.medical_claim`
  → `silver_input_layer_medical_claim` (and `gold.<table>` → `gold_<table>`).
- **Tenant filter toggle** (top bar) — on by default on tenant branches;
  restricts each tenant table to its own rows via a `WHERE data_source = …`
  filter. Not applicable on the main branch (control is disabled there). Can
  be turned off per query, and a table with no `data_source` column offers
  to disable the filter for that query.
- **Preview** tab — shows the first `preview_row_limit` rows of the selected
  table.
- **Schema** tab — lists column name, type, and nullability.
- **SQL** tab — a CodeMirror editor plus a results grid.
  - **Ctrl/Cmd+Enter** runs the current selection, or the whole editor if
    nothing is selected.
  - **Esc** cancels the running query.
  - **Row limit** field (default `10000`) caps the rows returned; a
    truncation notice appears if the result hit the limit.
  - **History** dropdown recalls previous SQL text for this profile (SQL
    text only — never results); **Clear** wipes this profile's history.
  - Referenced view names are registered lazily the first time they're
    used.
  - **Read-only, always:** every query must be exactly one `SELECT` (or
    `WITH … SELECT` / `DESCRIBE` / `SHOW` / `SUMMARIZE`) statement. Anything
    else (`INSERT`, `CREATE`, `COPY`, `ATTACH`, `PRAGMA`, …) is rejected
    before it runs.
  - **Blocked file functions:** direct file/URL access
    (`read_parquet`, `read_csv`, `iceberg_scan`, quoted file paths, etc.) and
    a short list of environment/introspection functions are always blocked
    in user SQL — only Floe's own registered views and a small allowlist
    (`range`, `generate_series`, `unnest`) are permitted. This holds even if
    `restrict_file_access` is turned off in the profile.
  - If the catalog changes mid-query (a `metadata.json` no longer exists),
    Floe reloads and retries the query once automatically, with a
    non-blocking "Catalog changed — reloaded and retried" notice.
- **Export CSV…** — appears on a results grid; does nothing unless the
  active profile has **Allow CSV export** turned on (Safety settings).
  Clicking it always confirms the row count and that the file is saved by
  your browser (Floe keeps no copy) before writing.
- **Keyboard shortcuts** (also under Help ▾ → Keyboard shortcuts):

  | Shortcut | Action |
  |---|---|
  | Ctrl/Cmd+Enter | Run the selection, or the whole editor |
  | Esc | Cancel the running query |
  | Ctrl/Cmd+R | Refresh the catalog |
  | Ctrl/Cmd+F | Filter tables (outside the editor) |
  | Alt+1 / 2 / 3 | Preview / Schema / SQL tab |
  | Ctrl/Cmd+C | Copy selected cells as TSV (in a grid) |
  | Ctrl/Cmd+A | Select all cells (in a grid) |
  | Double-click a table | Insert its view name into the SQL editor |

- **Copy diagnostics** (Help ▾ → Copy diagnostics) — copies to the clipboard:
  version and commit, platform/architecture, the active profile's non-secret
  fields, catalog status, and the last 200 (redacted) log lines. Use this
  when reporting a bug.

---

## 6. Ask (AI-assisted SQL) with OpenRouter — free models only

The SQL tab has an **Ask…** panel: describe what you want in plain language,
and Floe asks an LLM via [OpenRouter](https://openrouter.ai) to draft SQL
for you. **The generated SQL is never run automatically** — it's placed in
the editor for you to review and run yourself.

There is no model to choose: Ask always tries a fixed, ordered list of free
models and automatically falls back to the next one if one is busy,
unavailable, or times out.

### Set up an OpenRouter account and key

1. Create a free account at [openrouter.ai](https://openrouter.ai).
2. Go to **Settings → Keys** and create an API key. No credit card is needed
   for free models.
3. Free-tier limits are roughly **20 requests/minute** and a daily cap
   (about 200/day as of September 2026 — check
   [OpenRouter's docs](https://openrouter.ai/docs) for the current numbers).

If a request fails with a message about **no endpoints matching your data
policy**, go to OpenRouter's **Settings → Privacy** and allow the
providers/data policies required by free models — free providers may log
prompts.

### The automatic model chain and fallback

Ask tries these free models, in order, moving to the next one whenever a
model times out, is rate-limited, returns a server error, or turns out to be
unavailable:

| Order | Model ID | Notes |
|---|---|---|
| 1 | `qwen/qwen3-coder:free` | Qwen3 Coder 480B A35B — strong code/SQL generation, very long context. |
| 2 | `openai/gpt-oss-120b:free` | Large, capable general model. |
| 3 | `openai/gpt-oss-20b:free` | Fast, good at SQL, 131K context. |
| 4 | *(dynamic)* | The best other free model Floe can currently find on OpenRouter — a coder/code model if one exists, else a general chat/instruct model from a well-known provider, picked by fetching OpenRouter's public model list (cached up to 24 hours) and skipped if that list can't be fetched. |
| 5 | `openrouter/free` | Last resort: OpenRouter picks a random available free model. Always tried last. |

Each attempt has its own ~25-second timeout, and the whole question has an
overall time budget (the **Timeout (s)** setting, default 90 seconds) split
across every model tried. A bad API key or an account with no credits stops
immediately — those aren't per-model problems, so there's no point trying
the rest of the chain. If every model in the chain fails, Ask reports "All
free models are busy or unavailable right now — try again in a minute."

Free model availability on OpenRouter changes over time; the dynamic slot
adapts to that automatically, so there's nothing for you to update here.

### Configure Ask in Floe

Open **Help ▾ → Ask settings…** (or **Ask…** panel → **Settings…**):

| Field | Meaning | Default |
|---|---|---|
| Enabled | Turns Ask on/off. | off |
| API key | Your OpenRouter key. Stored in the OS keyring, write-only (shows "(saved)"); without a usable keyring, set `FLOE_OPENROUTER_API_KEY` instead. | — |

Under **Advanced**:

| Field | Meaning | Default |
|---|---|---|
| Base URL | The OpenRouter API base. Must be `https://` unless it points at this machine (`127.0.0.1`/`localhost`). | `https://openrouter.ai/api/v1` |
| Timeout (s) | Overall time budget for a question, across every model tried, 1–600 seconds. | `90` |

### Using it

1. In the SQL tab, click **Ask…** to open the panel.
2. Type what you want in **Describe what you want**.
3. Choose **Replace editor** or **Insert at cursor** for where the SQL
   should land.
4. Click **What will be sent** to preview the exact request body first
   (schema only, no API key, no rows) — shown for the first model in the
   chain; the model actually used may change if Ask falls back.
5. Click **Generate SQL**. The result lands in the editor per your chosen
   mode; it is **never run for you** — review it and click **Run**
   yourself. A small note under the panel says **"Generated by
   &lt;model&gt;"**, plus how many models Ask had to fall back through, if
   any. Any warnings (e.g. the SQL looks like it would fail the read-only
   check) are shown under that.

### Privacy — what is sent

- Your question.
- The names of the relevant views (the selected table, and view names
  mentioned in the question or current editor text).
- Column **names and types** of those relevant views (obtained via
  `DESCRIBE` — metadata only).
- A list of the other view names in the catalog.
- Fixed instructions (DuckDB SQL dialect, generate exactly one read-only
  `SELECT`/`WITH…SELECT` statement, don't add `data_source` filters since
  views are already tenant-filtered).

### What is never sent

Row data, query results, previews, sample or distinct values, statistics,
file paths, container/storage-account names, branch head hashes, or
secrets. This is structural: the prompt builder (`core/ask.py`) only accepts
the question plus schema objects (view name, column name, column type) — it
has no access to sessions, DataFrames, or query results.

**Don't type patient identifiers or other sensitive values into your
question** — free OpenRouter providers may log prompts.

---

## 7. Troubleshooting & FAQ

**macOS Keychain prompts.** The first time Floe reads/writes a secret, macOS
may prompt for Keychain access. Choose **Always Allow** so you aren't
prompted on every request.

**DuckDB extensions need internet on first use.** The first time Floe
queries an Iceberg table, DuckDB downloads its `azure`/`iceberg`/`httpfs`
extensions from `extensions.duckdb.org`. This needs network access once;
after that they're cached locally. If the download fails, the error names
the extension and suggests checking your network connection.

**HTTPS certificate errors ("Couldn't verify the HTTPS certificate for the
storage account …").** The storage account's TLS certificate didn't chain to
a root Floe trusts. This is common on company laptops whose network inspects
HTTPS with a corporate root CA. Fix: export your company's root CA bundle as
PEM and set **Storage → CA cert file** in the profile (or `ADLS_CA_CERT_FILE`),
then re-run **Test connection** — or try another network.

- *Windows:* with no CA cert file set, Floe uses the azure extension's default
  transport (WinHTTP), which trusts the Windows certificate store, including
  corporate roots your IT department installed. Setting a CA cert file
  switches to the curl transport with only that bundle, so it must contain
  every root you need.
- *macOS/Linux:* Floe uses curl with `/etc/ssl/cert.pem` (or certifi's
  bundle). Corporate roots in the macOS Keychain are **not** read, so set the
  CA cert file on an inspected network.

**"Access denied" reading a file.** This usually means `restrict_file_access`
correctly blocked a read outside the current branch's expected container(s).
If you have a specific, understood reason to read elsewhere, turn off
**Restrict file access** in the profile's Safety settings — but note the SQL
table-function blocklist always applies regardless of this setting.

**Corrupt pointer / not-found icons in the table tree.** A "not found" icon
means the table's Nessie pointer 404s. A "corrupt pointer" icon means the
table's Nessie pointer has `snapshotId == -1` — a real state some tables can
be in; it isn't a Floe bug, but that table has no valid snapshot to read
right now.

**Catalog unreachable.** The catalog status pill shows this when Nessie
can't be reached; Floe keeps showing the last known branch head instead of
failing outright, and retries automatically per `nessie_head_ttl_seconds`.

**Where logs live.**

| OS | Directory |
|---|---|
| macOS | `~/Library/Logs/Floe` |
| Linux | `~/.local/state/Floe/log` (or `$XDG_STATE_HOME/Floe/log`) |
| Windows | `%LOCALAPPDATA%\Floe\Logs` |

The log file (`app.log`) rotates at 5 MB, keeping 5 backups, and is readable
only by your user account. Every log line passes through a redaction filter
that masks secret values, `Authorization: Bearer …` headers,
`AccountKey=…`/`client_secret=…` patterns, and anything that looks like a
JWT.

**Copy diagnostics for bug reports.** Use **Help ▾ → Copy diagnostics** to
put a redacted bundle (version, platform, active profile's non-secret
fields, catalog status, last 200 log lines) on the clipboard, ready to paste
into a bug report or a dev session.
