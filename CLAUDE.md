# Floe — notes for Claude

Floe is a personal Mac app for browsing and querying Iceberg tables (via Nessie + DuckDB).
SPEC.md is the source of truth; PLAN.md sequences the work into phases.

## Layer rule
`floe.core` must never import Qt (PySide6). All data and auth logic must be testable
without a GUI. This is enforced by `tests/test_layering.py`, which imports every
`floe.core` module in a subprocess with PySide6 imports blocked.

## Read-only rule
The app is strictly read-only. No write paths to Iceberg or Nessie exist in the code —
do not add any.

## Secrets rule
Secrets (account keys, client secrets) live only in the macOS Keychain (via `keyring`)
and in process memory. They must never be written to `profiles.json`, logs, diagnostics
output, or exceptions shown in the UI. Every secret value must be registered with the
redaction filter (`core/diagnostics.py`) as soon as it is loaded, and all log handlers
and user-facing error text must pass through that filter.

## No hard-coded environment-specific values
No hostname, IP, storage account, container name, tenant branch, namespace, or table key
may be hard-coded anywhere in the app. The user enters all of these in a profile. Tests
and fixtures use synthetic names only (e.g. `eg-test1`, `ref-a`).

## Test data
Only synthetic data. Never commit real `.env` files, real credentials, real container
names, or real hostnames.

## Running tests and lint
```
QT_QPA_PLATFORM=offscreen pytest
ruff check
```
