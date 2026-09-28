""""Test connection" steps for the profile dialog (SPEC §8.3).

No Qt here: this module yields plain `StepResult`s that the UI renders from a
worker thread. Each step's message is passed through `diagnostics.redact`
before being returned, and no secret value is ever included in a message.
"""

from __future__ import annotations

import socket
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from floe.core import diagnostics
from floe.core.errors import FloeError
from floe.core.nessie import NessieClient
from floe.core.profiles import Profile

DEFAULT_TCP_TIMEOUT_SECONDS = 5.0
DEFAULT_HTTP_PORT = 80
DEFAULT_HTTPS_PORT = 443


@dataclass(frozen=True)
class StepResult:
    """One line of "Test connection" output."""

    name: str
    ok: bool
    message: str
    elapsed_ms: float


def _timed(name: str, fn: Callable[[], str]) -> StepResult:
    start = time.perf_counter()
    try:
        message = fn()
    except FloeError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return StepResult(name, False, diagnostics.redact(exc.user_message()), elapsed_ms)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user, always redacted
        elapsed_ms = (time.perf_counter() - start) * 1000
        return StepResult(name, False, diagnostics.redact(str(exc)), elapsed_ms)
    elapsed_ms = (time.perf_counter() - start) * 1000
    return StepResult(name, True, diagnostics.redact(message), elapsed_ms)


def _host_port(nessie_uri: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(nessie_uri)
    host = parsed.hostname or ""
    if parsed.port:
        port = parsed.port
    elif parsed.scheme == "https":
        port = DEFAULT_HTTPS_PORT
    else:
        port = DEFAULT_HTTP_PORT
    return host, port


def _default_tcp_check(host: str, port: int, timeout: float) -> None:
    """Raise `OSError` if a TCP connection to `host:port` cannot be made."""
    with socket.create_connection((host, port), timeout=timeout):
        pass


def _default_duckdb_probe(
    profile: Profile,
    secrets: Mapping[str, str | None],
    metadata_location: str,
) -> None:
    """Build a throwaway DuckDB connection with the storage secret and read a small object.

    Reads the given Iceberg `metadata.json` location as text, just to prove the
    configured storage credential can read from the account. Raises on failure.
    """
    # Imported lazily: `context.py` is being edited concurrently elsewhere, but its
    # pure SQL builder is stable public API we can reuse here.
    from floe.core.context import (
        apply_curl_ca_bundle,
        build_remote_setup,
        quote_literal,
        resolve_ca_bundle,
    )
    from floe.core.extensions import bundled_extension_dir

    ca_bundle = resolve_ca_bundle(profile)
    apply_curl_ca_bundle(ca_bundle)
    conn = duckdb.connect(":memory:")
    try:
        # Same extension source as the real session: the frozen app's bundled copies.
        ext_dir = bundled_extension_dir()
        if ext_dir is not None:
            conn.execute(f"SET extension_directory = {quote_literal(str(ext_dir))}")
        for stmt in build_remote_setup(profile, secrets, ca_bundle, storage=True):
            conn.execute(stmt.sql)
        conn.execute(
            "SELECT count(*) FROM read_text(?)",
            [metadata_location],
        )
    finally:
        conn.close()


def _tcp_step(
    profile: Profile,
    tcp_check: Callable[[str, int, float], None],
    timeout: float,
) -> StepResult:
    host, port = _host_port(profile.nessie_uri)

    def run() -> str:
        if not host:
            raise ValueError(f"Could not parse a host from nessie_uri: {profile.nessie_uri!r}")
        try:
            tcp_check(host, port, timeout)
        except OSError as exc:
            raise ConnectionError(
                f"Can't reach {host}:{port} — are you on VPN? ({exc})"
            ) from None
        return f"Reached {host}:{port}"

    return _timed("TCP reachability", run)


def _token_step(profile: Profile, client: NessieClient | None) -> StepResult:
    if profile.nessie_auth == "none":
        return StepResult("Token acquisition", True, "n/a (nessie_auth=none)", 0.0)

    def run() -> str:
        assert client is not None
        client.ensure_token()
        return "Acquired a token"

    return _timed("Token acquisition", run)


def _head_step(profile: Profile, client: NessieClient | None) -> StepResult:
    def run() -> str:
        assert client is not None
        head = client.head(profile.nessie_main_ref)
        return f"{profile.nessie_main_ref} @ {head[:12]}"

    return _timed(f"GET /trees/{profile.nessie_main_ref}", run)


def _storage_step(
    profile: Profile,
    secrets: Mapping[str, str | None],
    client: NessieClient | None,
    duckdb_probe: Callable[[Profile, Mapping[str, str | None], str], None],
) -> StepResult:
    def run() -> str:
        assert client is not None
        keys = client.list_tables(profile.nessie_main_ref)
        if not keys:
            return "No tables found on main ref to probe; skipped"
        key = keys[0]
        pointer = client.pointer(profile.nessie_main_ref, key)
        duckdb_probe(profile, secrets, pointer.metadata_location)
        return f"Read metadata for {key.dotted}"

    return _timed("ADLS read", run)


def _local_step(profile: Profile) -> StepResult:
    def run() -> str:
        if not profile.local_fixture_dir:
            raise ValueError("local mode requires local_fixture_dir")
        root = Path(profile.local_fixture_dir)
        if not root.is_dir():
            raise ValueError(f"Directory does not exist: {root}")
        containers = [d for d in root.iterdir() if d.is_dir()]
        if not containers:
            raise ValueError(f"No container directories found under {root}")
        return f"Found {len(containers)} container dir(s) under {root}"

    return _timed("Local fixture directory", run)


def run_connection_test(
    profile: Profile,
    secrets: Mapping[str, str | None] | None = None,
    *,
    nessie_client_factory: Callable[[Profile, str | None], NessieClient] = NessieClient,
    duckdb_probe: Callable[
        [Profile, Mapping[str, str | None], str], None
    ] = _default_duckdb_probe,
    tcp_check: Callable[[str, int, float], None] = _default_tcp_check,
    tcp_timeout: float = DEFAULT_TCP_TIMEOUT_SECONDS,
    **_: Any,
) -> Iterator[StepResult]:
    """Run the "Test connection" steps (SPEC §8.3), stopping at the first failure.

    Local-mode profiles get a single step checking `local_fixture_dir`. Remote-mode
    profiles get, in order: TCP reachability, token acquisition (skipped for
    `nessie_auth=none`), `GET /trees/<nessie_main_ref>`, and an ADLS read of the
    first table's metadata.json on main (injectable via `duckdb_probe` so tests can
    run against the fake Nessie without touching real Azure).
    """
    secrets = secrets or {}
    for value in secrets.values():
        diagnostics.register_secret(value)

    if profile.mode == "local":
        yield _local_step(profile)
        return

    step = _tcp_step(profile, tcp_check, tcp_timeout)
    yield step
    if not step.ok:
        return

    client = nessie_client_factory(profile, secrets.get("nessie_client_secret"))

    step = _token_step(profile, client)
    yield step
    if not step.ok:
        return

    step = _head_step(profile, client)
    yield step
    if not step.ok:
        return

    step = _storage_step(profile, secrets, client, duckdb_probe)
    yield step


__all__ = ["StepResult", "run_connection_test"]
