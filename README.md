# Floe

Floe is a personal, read-only app for browsing and querying Apache Iceberg
tables on ADLS Gen2, with Nessie as the catalog and DuckDB as the query
engine. It runs entirely on your own machine — no shared server, no writes to
Iceberg or Nessie.

Floe ships two front ends that share the same core:

- **Web app** (all platforms) — `floe serve` starts a local server and opens
  your browser. This is where new features land.
- **Desktop app** (macOS only, frozen at v0.9) — the original PySide6/Qt app.
  See [Desktop app](#desktop-app-macos-frozen-at-v09) below.

## Install (web app — macOS, Linux, Windows)

New to Floe? Follow the step-by-step [INSTALL.md](INSTALL.md).

See [USAGE.md](USAGE.md) for a full setup and usage guide.

**Prerequisites:** Python 3.12 or later, and [pipx](https://pipx.pypa.io/).

Install the latest release:

```
pipx install https://github.com/Tcookie47/Floe/releases/download/vX.Y.Z/floe-X.Y.Z-py3-none-any.whl
```

(replace `vX.Y.Z` / `X.Y.Z` with the version from the
[Releases page](../../releases)), or track the latest commit on `main`:

```
pipx install "git+https://github.com/Tcookie47/Floe@main"
```

Then run:

```
floe serve
```

or `floe serve --background` to keep Floe running after you close the terminal;
`floe show` then prints a fresh one-time link (`floe show --open` opens it) and
`floe stop` stops it.

**What happens:** Floe binds a server to `127.0.0.1` only (never reachable
from other machines), prints a one-time tokenized URL, and opens it in your
default browser. With plain `floe serve`, keep the terminal window open while
you use Floe — closing it (or `Ctrl+C`) stops the server. If the browser doesn't open, copy the
printed URL manually.

**Where your data lives:**

- Profiles, SQL history, logs and UI preferences are stored in your OS's
  standard per-user app-data directory (via `platformdirs`): e.g.
  `~/Library/Application Support/Floe` on macOS, `~/.local/share/Floe` on
  Linux, `%LOCALAPPDATA%\Floe` on Windows. Set `FLOE_HOME` to override this
  (mainly for tests).
- Secrets (account keys, client secrets, the OpenRouter API key) are stored
  in your OS keyring — macOS Keychain, Windows Credential Manager, or the
  Linux Secret Service — never in `profiles.json` or any file Floe writes.
- **Headless Linux** (no Secret Service running): Floe falls back to reading
  secrets from environment variables named
  `FLOE_SECRET__<PROFILE>__<FIELD>`, where `<PROFILE>` is the profile name
  upper-cased with non-alphanumeric characters replaced by `_`. For example,
  a profile named `eg-test1` needing an `account_key` field:

  ```
  export FLOE_SECRET__EG_TEST1__ACCOUNT_KEY="..."
  ```

  The UI tells you when it's using this fallback, and saving a secret from
  the UI shows a clear error on a headless box instead of failing silently.

**DuckDB extensions:** the first time Floe queries an Iceberg table, DuckDB
downloads its `iceberg`/`azure`/`httpfs` extensions from
`extensions.duckdb.org`. This needs network access once; after that they're
cached locally.

**Upgrading:**

```
pipx upgrade floe
```

or reinstall a newer release wheel with `pipx install --force <url>`.

## Using Floe

- **Profiles.** Create, duplicate, edit, or delete a connection profile (Nessie
  + ADLS details, or a local fixture directory for `local` mode), or import one
  from a `.env` file. **Test connection** checks it before you save. The profile
  and branch you last had open are remembered across restarts.
- **Branch picker.** Lists every Nessie branch, including inactive tenants
  (greyed out, still selectable) when the profile has a `tenant_registry_table`
  configured.
- **Table tree.** Grouped by layer (bronze / silver / gold, then anything else),
  with a filter box and status icons; shared reference tables read from the main
  ref appear under **Reference (main)**.
- **Preview / Schema / SQL.** Preview shows the first `preview_row_limit` rows;
  Schema lists columns; SQL runs ad-hoc read-only queries against the tables as
  views, with a row limit, cancel, and lazy table registration.
- **Tenant filter.** On by default for tenant branches; restricts tenant tables
  to the current branch's own rows. Can be disabled per query or per table when a
  table has no `data_source` column.
- **Read-only, always.** Floe has no write path to Iceberg or Nessie. Every
  query is checked to be a single read-only statement before it runs.
- **Export CSV…** is off by default; a profile needs `allow_export` enabled
  (Safety settings) before exporting does anything, and exporting always shows
  the row count and destination first.
- **Query history** stores SQL text only, per profile — never results — and can
  be cleared at any time.
- **Diagnostics.** Copy diagnostics puts version, platform, the active
  profile's non-secret fields, catalog status, and the last 200 (redacted) log
  lines on the clipboard.
- **Restrict file access.** An escape hatch in a profile's Safety settings; when
  disabled, DuckDB isn't limited to the current ref's containers (direct
  file/URL functions in SQL stay blocked either way). Leave it on unless you
  have a specific reason not to.

## Ask (AI-assisted SQL)

The SQL view has an **Ask** box: type what you want in plain language, and
Floe asks an LLM (via [OpenRouter](https://openrouter.ai)) to generate SQL,
which is put into the editor for you to review — **it is never run
automatically.**

**Setup:** create an OpenRouter account, generate an API key, and pick a
model ID. Check
[OpenRouter's model list](https://openrouter.ai/models) — filtering to free
models is a good starting point — and enter whichever model ID you choose in
Floe's Ask settings; Floe doesn't require or hard-code a specific model.

**Privacy — what is sent:** your question; the names and types of the
relevant views' columns; a list of other view names; and fixed instructions
(DuckDB dialect, read-only `SELECT` only). You can preview the exact prompt
before it's sent ("What will be sent").

**What is never sent:** row data, query results, previews, sample or distinct
values, statistics, file paths, container/storage-account names, branch head
hashes, or secrets.

Free OpenRouter providers may log prompts, so **don't include patient
identifiers or other sensitive values in your question.** Generated SQL is
always shown in the editor for review — Floe never executes it for you.

## Desktop app (macOS, frozen at v0.9)

The original Qt desktop app still works but gets no new features (see
[SPEC.md](SPEC.md) §15). It's released separately under `desktop-v*` tags —
see the [Releases page](../../releases) for `desktop-v0.9.x` builds.

1. Download the zip from the matching `desktop-v*` release and unzip it.
2. Move `Floe.app` to `/Applications`.
3. The app isn't notarized, so clear the quarantine flag once:
   ```
   xattr -dr com.apple.quarantine /Applications/Floe.app
   ```
   (or right-click the app → **Open** → **Open**).
4. After updating to a new version, macOS may ask to allow Keychain access.
   Choose **Always Allow** — each build is ad-hoc signed.

Profiles and secrets live outside the `.app` (same data dir the web app
uses), so replacing the app on update keeps them.

## Development & releasing

`floe.core` has no Qt dependency (SPEC §15, CLAUDE.md) and runs on macOS,
Linux and Windows; the Qt desktop app is an optional `desktop` extra.

```
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"        # core + web; Qt tests skip
QT_QPA_PLATFORM=offscreen pytest
ruff check
```

To also run the Qt desktop app and its tests:

```
pip install -e ".[dev-desktop]"
QT_QPA_PLATFORM=offscreen pytest
```

Browser end-to-end tests (`tests/web/test_e2e.py`, marked `e2e`) need
Playwright's Chromium:

```
playwright install chromium
pytest -m e2e
```

They're skipped by default (`pytest -m "not e2e"` in the main CI jobs) and
run in their own CI job.

### Releasing

- **`vX.Y.Z`** — triggers `.github/workflows/release-web.yml`: installs the
  `dev` extra, lints, runs tests (excluding `e2e`), writes
  `src/floe/_build_info.py` from the tag, builds a wheel + sdist with
  `python -m build`, smoke-tests the wheel in a fresh venv, and publishes them
  to a GitHub Release.
- **`desktop-vX.Y.Z`** — triggers `.github/workflows/release.yml`: builds the
  Mac `Floe.app` with PyInstaller, ad-hoc signs it, zips it with `ditto`, and
  publishes it as a GitHub Release asset.

```
git tag v1.0.0 && git push origin v1.0.0            # web release
git tag desktop-v0.9.1 && git push origin desktop-v0.9.1   # mac app release
```
