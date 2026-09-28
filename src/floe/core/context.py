"""DuckDB connection cache, lazy view registration and read-only querying (SPEC §6).

Main entry point for the UI: `FloeSession(profile, secrets, nessie_client=None)`.

- One in-memory DuckDB connection (`Context`) per *data version*, cached in a bounded LRU
  (`ConnectionCache`) keyed by `(profile_name, mode, ref, ref_head, main_head, fixture_dir)`.
- Views are registered lazily: when a table is previewed, its schema is shown, or its view
  name appears in user SQL.
- Tenant scoping (SPEC §6.3 as amended by PLAN "Resolved open questions"):
  * tenant branch → container must be the branch's container (override map → tenant
    registry → branch name); main / shared namespaces → container ∈ `shared_containers`;
  * shared namespaces are always resolved from the main ref and hidden on tenant branches;
  * the `data_source` filter applies only on tenant branches, only to non-shared tables,
    and only while the tenant filter is on.
- The app is read-only: user SQL must be a single SELECT-type statement.
- User SQL can't bypass tenant scoping (defence in depth): `check_read_only` rejects table
  functions outside a small allowlist (`read_parquet`, `iceberg_scan`, `glob`, ...) and
  direct file/URL references (`FROM 'x.parquet'`); and every connection is restricted
  after setup (`allowed_directories` = the ref's container + the shared containers,
  `enable_external_access = false`, `lock_configuration = true`).
- Each query holds its connection's (re-entrant) lock from view resolution through
  execution, so concurrent queries with different tenant-filter modes can't swap a view
  underneath each other.
- Cancellation: pass a `CancelToken` as `cancel_token=` to `query` / `preview` / `schema` /
  `register` / `resolve_sql_references` and call `token.cancel()` from any thread.
  `interrupt(ref)` / `interrupt_all()` cancel every active query of a ref / of the session.

No Qt here. No secret or row data is ever logged.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple, TypeVar

import duckdb
import pandas as pd

from floe.core import diagnostics
from floe.core.errors import (
    AdlsAuthError,
    CorruptPointer,
    DisallowedFunction,
    ExtensionUnavailable,
    FloeError,
    MetadataReadError,
    MissingDataSourceColumn,
    QueryCancelled,
    QueryError,
    ReadOnlyViolation,
    TableNotFound,
    TenantScopeError,
    TimelineUnavailable,
    ViewNameCollision,
)
from floe.core.extensions import bundled_extension_dir
from floe.core.nessie import NessieClient, TableKey, container_of, metadata_file_name
from floe.core.profiles import Profile

log = logging.getLogger("floe.context")

T = TypeVar("T")

TableStatus = Literal[
    "unknown",
    "registered",
    "not_found",
    "corrupt",
    "scope_error",
    "missing_data_source",
    "error",
]

DATA_SOURCE_COLUMN = "data_source"
REDACTED = "***"
DEFAULT_CA_BUNDLE = "/etc/ssl/cert.pem"
REMOTE_EXTENSIONS = ("azure", "iceberg", "httpfs")
REGISTRY_RETRY_SECONDS = 5.0  # back-off before re-reading a tenant registry that failed


# --------------------------------------------------------------------------- data types


class ConnectionKey(NamedTuple):
    profile_name: str
    mode: str
    ref: str
    ref_head: str
    main_head: str
    fixture_dir: str

    def short(self) -> str:
        """Loggable form with hashes shortened to 8 characters."""
        return (
            f"({self.profile_name}, {self.mode}, {self.ref}, {self.ref_head[:8]}, "
            f"{self.main_head[:8]}, {self.fixture_dir or '-'})"
        )


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class BranchInfo:
    name: str
    active: bool | None  # None = unknown (no registry, or branch not in it)


@dataclass(frozen=True)
class TableInfo:
    key: TableKey
    view_name: str
    source_ref: str  # the ref the table is resolved from (main ref for shared tables)
    shared: bool
    status: TableStatus = "unknown"
    error: str | None = None  # redacted user-facing message when status is an error
    container: str | None = None  # known up front in local mode only

    @property
    def dotted(self) -> str:
        return self.key.dotted


@dataclass
class QueryResult:
    df: pd.DataFrame
    truncated: bool
    elapsed: float  # seconds
    reloaded: bool  # True if the catalog changed mid-query and the query was retried

    @property
    def row_count(self) -> int:
        return len(self.df)


@dataclass(frozen=True)
class RegistryEntry:
    branch: str
    container: str | None
    active: bool | None
    data_source: str | None = None


@dataclass(frozen=True)
class SetupStatement:
    sql: str
    loggable: str  # the same statement with secret values replaced by ***
    extension: str | None = None  # set for INSTALL / LOAD statements


class _MetadataGone(QueryError):
    """Internal: a referenced metadata.json no longer exists (catalog rebuilt)."""


class _ScopeChanged(_MetadataGone):
    """Internal: the ref's container mapping changed after its connection was restricted."""


# --------------------------------------------------------------------------- SQL helpers


def quote_literal(value: str) -> str:
    """Quote a SQL string literal (single quotes doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


def quote_identifier(name: str) -> str:
    """Quote a SQL identifier (double quotes doubled)."""
    return '"' + str(name).replace('"', '""') + '"'


_COMMENT_RE = re.compile(r"^\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/)", re.DOTALL)
_READ_KEYWORDS = frozenset(
    {"select", "with", "from", "values", "table", "describe", "show", "summarize"}
)


def _first_keyword(text: str) -> str:
    rest = text
    while True:
        m = _COMMENT_RE.match(rest)
        if not m:
            break
        rest = rest[m.end() :]
    rest = rest.lstrip().lstrip("(").lstrip()
    m = re.match(r"[A-Za-z_]+", rest)
    return m.group(0).lower() if m else ""


# Table functions user SQL may call. Everything else (read_parquet, read_csv*, read_json*,
# read_text, read_blob, glob, sniff_csv, parquet_*, iceberg_*, query, query_table,
# duckdb_secrets, ...) could read files outside the ref's container or reveal settings.
ALLOWED_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})
# Scalar functions that touch the environment or bind (and so open files for) other SQL.
DISALLOWED_SCALAR_FUNCTIONS = frozenset(
    {"getenv", "json_serialize_plan", "json_deserialize_sql", "json_execute_serialized_sql"}
)
# A table name with one of these characters is a DuckDB replacement scan of a file / URL
# (`FROM 'x.parquet'`, `FROM "s3://..."`), never one of Floe's views.
_FILE_REFERENCE_CHARS = frozenset("./\\:*?")

_parser_lock = threading.Lock()
_parser_conn: duckdb.DuckDBPyConnection | None = None


def _serialize_sql(sql: str) -> dict:
    """Parse `sql` with DuckDB's own parser into its JSON AST (no binding, no file access)."""
    global _parser_conn
    with _parser_lock:
        if _parser_conn is None:
            conn = duckdb.connect(":memory:")
            try:
                conn.execute("SET enable_external_access = false")
            except duckdb.Error:  # pragma: no cover - older DuckDB
                pass
            _parser_conn = conn
        raw = _parser_conn.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
    return json.loads(raw)


def _check_sql_tree(tree: object) -> None:
    """Reject disallowed table functions, scalar functions and file references."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        ntype = node.get("type")
        if ntype == "TABLE_FUNCTION":
            function = node.get("function") or {}
            name = str(function.get("function_name", "")).lower()
            if name not in ALLOWED_TABLE_FUNCTIONS:
                raise DisallowedFunction(name or "<unknown>", "table function")
        elif ntype == "BASE_TABLE":
            name = str(node.get("table_name", ""))
            if any(ch in _FILE_REFERENCE_CHARS for ch in name):
                raise DisallowedFunction(name, "file")
        elif ntype == "FUNCTION" and node.get("class") == "FUNCTION":
            name = str(node.get("function_name", "")).lower()
            if name in DISALLOWED_SCALAR_FUNCTIONS:
                raise DisallowedFunction(name, "function")
        stack.extend(node.values())


def check_read_only(sql: str) -> str:
    """Validate that `sql` is exactly one read-only query; return its text.

    Uses DuckDB's own parser (`extract_statements`) for the statement type, and also
    rejects SELECT-rewritten statements that don't start with a query keyword
    (e.g. `PRAGMA ...`, which DuckDB rewrites to a SELECT). Then walks DuckDB's parse
    tree (`json_serialize_sql`) and raises `DisallowedFunction` for table functions other
    than `ALLOWED_TABLE_FUNCTIONS`, for `DISALLOWED_SCALAR_FUNCTIONS`, and for direct
    file / URL table references — these would bypass tenant scoping. (Consequence: a view
    whose name contains `.` can't be queried from SQL; view names join key elements
    with `_`.)
    """
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as exc:
        raise QueryError(diagnostics.redact(str(exc))) from None
    if not statements:
        raise QueryError("No SQL statement to run.")
    if len(statements) > 1:
        raise QueryError("Run one statement at a time.")
    stmt = statements[0]
    stype = getattr(stmt.type, "name", str(stmt.type))
    text = stmt.query.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    if stype != "SELECT":
        raise ReadOnlyViolation(stype)
    # Check the user's own text: DuckDB rewrites e.g. PRAGMA into a SELECT.
    keyword = _first_keyword(sql)
    if keyword not in _READ_KEYWORDS:
        raise ReadOnlyViolation(keyword.upper() or "unknown")
    try:
        tree = _serialize_sql(text)
    except (duckdb.Error, ValueError) as exc:
        raise QueryError(diagnostics.redact(f"Could not parse the query: {exc}")) from None
    if tree.get("error"):
        reason = diagnostics.redact(str(tree.get("error_message", "unsupported statement")))
        raise QueryError(f"Floe could not verify that this query is read-only ({reason}).")
    _check_sql_tree(tree.get("statements", []))
    return text


def _wrap_limit(sql: str, limit: int) -> str:
    return f"SELECT * FROM (\n{sql}\n) AS floe_q LIMIT {int(limit) + 1}"


# --------------------------------------------------------------------------- error mapping

_MISSING_FILE_MARKERS = (
    "no such file",
    "cannot open file",
    "404",
    "blobnotfound",
    "pathnotfound",
    "resourcenotfound",
    "does not exist",
)
_AUTH_MARKERS = (
    "authenticationfailed",
    "authorizationfailure",
    "authorizationpermissionmismatch",
    "invalidauthenticationinfo",
    "server failed to authenticate",
    "aadsts",
    "clientsecretcredential",
    "http 401",
    "http 403",
)


def is_missing_metadata_error(exc: BaseException) -> bool:
    """True if `exc` is a DuckDB IO error about a missing `*.metadata.json` file."""
    if not isinstance(exc, duckdb.IOException):
        return False
    msg = str(exc).lower()
    if "metadata.json" not in msg:
        return False
    return any(marker in msg for marker in _MISSING_FILE_MARKERS)


def is_storage_auth_error(exc: BaseException) -> bool:
    if not isinstance(exc, duckdb.Error):
        return False
    msg = str(exc).lower()
    return any(marker in msg for marker in _AUTH_MARKERS)


# --------------------------------------------------------------------------- remote setup


def resolve_ca_bundle(profile: Profile) -> str | None:
    """Profile value, else /etc/ssl/cert.pem if it exists, else certifi's bundle."""
    if profile.adls_ca_cert_file:
        return profile.adls_ca_cert_file
    if os.path.exists(DEFAULT_CA_BUNDLE):
        return DEFAULT_CA_BUNDLE
    try:
        import certifi
    except ImportError:
        return None
    return certifi.where()


_CURL_CA_ENV = "CURL_CA_INFO"
_ORIGINAL_CURL_CA_INFO = os.environ.get(_CURL_CA_ENV)  # the value the app was started with
_ca_env_lock = threading.Lock()


def apply_curl_ca_bundle(bundle: str | None) -> None:
    """Point curl (DuckDB's azure transport) at `bundle` through `CURL_CA_INFO`.

    The variable is process-global, so it is recomputed for every remote connection from
    that connection's profile and overwritten (never `setdefault`) when it differs: a
    profile without a custom bundle gets the default bundle back after a profile with one.
    `None` restores the value the process started with. Writes are serialised by a
    module-level lock and only happen when the value actually changes.

    Caveat (macOS especially): `setenv` is not thread-safe with respect to concurrent
    `getenv` calls made by native code (curl reads the variable when it creates a handle
    on a DuckDB worker thread), and two profiles querying at the same time can't have
    different bundles through the environment. The per-connection DuckDB `ca_cert_file`
    setting (see `build_remote_setup`) is the primary mechanism; this variable only covers
    code paths that ignore it, and it changes only when switching between profiles with
    different bundles.
    """
    target = bundle or _ORIGINAL_CURL_CA_INFO
    with _ca_env_lock:
        if os.environ.get(_CURL_CA_ENV) == target:
            return
        if target is None:
            os.environ.pop(_CURL_CA_ENV, None)
        else:
            os.environ[_CURL_CA_ENV] = target
    log.info("CURL_CA_INFO set to %s", target or "<unset>")


def build_remote_setup(
    profile: Profile,
    secrets: Mapping[str, str | None],
    ca_bundle: str | None,
    *,
    storage: bool = True,
) -> list[SetupStatement]:
    """Pure builder for the remote-mode connection setup SQL (SPEC §6.2).

    `storage=False` is an internal/test hook: only the iceberg extension and tuning are
    set up (no azure/httpfs extensions, no transport settings, no secret).
    """
    stmts: list[SetupStatement] = []

    def plain(sql: str, extension: str | None = None) -> None:
        stmts.append(SetupStatement(sql, sql, extension))

    extensions = REMOTE_EXTENSIONS if storage else ("iceberg",)
    for ext in extensions:
        plain(f"INSTALL {ext}", ext)
        plain(f"LOAD {ext}", ext)

    if profile.duckdb_memory_limit:
        plain(f"SET memory_limit = {quote_literal(profile.duckdb_memory_limit)}")
    if profile.duckdb_threads:
        plain(f"SET threads = {int(profile.duckdb_threads)}")

    if not storage:
        return stmts

    plain("SET azure_transport_option_type = 'curl'")
    if ca_bundle:
        plain(f"SET ca_cert_file = {quote_literal(ca_bundle)}")

    account = profile.adls_account
    if profile.adls_auth == "service_principal":
        client_secret = secrets.get("adls_client_secret")
        if client_secret:
            head = (
                "CREATE OR REPLACE SECRET adls (TYPE azure, PROVIDER service_principal, "
                f"TENANT_ID {quote_literal(profile.adls_tenant_id)}, "
                f"CLIENT_ID {quote_literal(profile.adls_client_id)}, CLIENT_SECRET "
            )
            tail = f", ACCOUNT_NAME {quote_literal(account)})"
            stmts.append(
                SetupStatement(
                    head + quote_literal(client_secret) + tail,
                    head + quote_literal(REDACTED) + tail,
                )
            )
    else:
        account_key = secrets.get("adls_account_key")
        if account_key:

            def conn_str(key: str) -> str:
                return (
                    f"DefaultEndpointsProtocol=https;AccountName={account};"
                    f"AccountKey={key};EndpointSuffix=core.windows.net"
                )

            head = "CREATE OR REPLACE SECRET adls (TYPE azure, PROVIDER config, CONNECTION_STRING "
            stmts.append(
                SetupStatement(
                    head + quote_literal(conn_str(account_key)) + ")",
                    head + quote_literal(conn_str(REDACTED)) + ")",
                )
            )
    return stmts


# --------------------------------------------------------------------------- cache + context


class Context:
    """One DuckDB connection for one data version, plus its lock and view bookkeeping."""

    def __init__(self, key: ConnectionKey, connection: duckdb.DuckDBPyConnection) -> None:
        self.key = key
        self.connection = connection
        # Guards `connection` (DuckDB connections aren't thread-safe). Re-entrant: a query
        # holds it from view resolution through execution, and registration re-enters it.
        self.lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._views: dict[str, tuple[TableKey, bool]] = {}  # view -> (key, filter applied)
        self._status: dict[str, tuple[TableStatus, str | None]] = {}
        self.tables: dict[str, TableInfo] | None = None  # view_name -> info (catalog listing)
        # view_name -> every key mapping to it, when more than one does (never registered)
        self.collisions: dict[str, tuple[TableInfo, ...]] = {}
        # Containers DuckDB may read on this connection (None = no file-access restriction).
        self.allowed_containers: frozenset[str] | None = None

    @property
    def ref(self) -> str:
        return self.key.ref

    def execute(self, sql: str) -> list[tuple]:
        with self.lock:
            return self.connection.execute(sql).fetchall()

    def interrupt(self) -> None:
        """Interrupt the running statement, if any. Deliberately does not take the lock."""
        self.connection.interrupt()

    # bookkeeping
    def registered(self, view_name: str) -> bool | None:
        """Filter mode the view is registered with, or None if it isn't registered."""
        with self._state_lock:
            entry = self._views.get(view_name)
            return None if entry is None else entry[1]

    def registered_key(self, view_name: str) -> TableKey | None:
        with self._state_lock:
            entry = self._views.get(view_name)
            return None if entry is None else entry[0]

    def registered_views(self) -> dict[str, bool]:
        with self._state_lock:
            return {view: filtered for view, (_, filtered) in self._views.items()}

    def mark_registered(self, view_name: str, filtered: bool, key: TableKey | None = None) -> None:
        with self._state_lock:
            if key is None:
                key = TableKey(tuple(view_name.split("_")))
            self._views[view_name] = (key, filtered)
            self._status[view_name] = ("registered", None)

    def mark_unregistered(self, view_name: str) -> None:
        with self._state_lock:
            self._views.pop(view_name, None)

    def set_status(self, view_name: str, status: TableStatus, error: str | None) -> None:
        with self._state_lock:
            self._status[view_name] = (status, error)

    def status_of(self, view_name: str) -> tuple[TableStatus, str | None]:
        with self._state_lock:
            return self._status.get(view_name, ("unknown", None))


class CancelToken:
    """Cancellation handle for one query. Thread-safe; `cancel()` never takes a query lock.

    Create one per query, pass it as `cancel_token=` to `FloeSession.query` / `preview` /
    `schema` / `register` / `resolve_sql_references`, and call `cancel()` from any thread
    (e.g. the UI thread). A cancelled token makes its query raise `QueryCancelled`: before
    the connection lock is acquired, right after it is acquired, and before every
    statement (view-registration statements included). The DuckDB connection is
    interrupted only while one of *this token's* statements is executing, so a cancel can
    never hit another query on the same connection; the interrupt is repeated until the
    statement returns, because DuckDB ignores an interrupt that arrives before the
    statement has actually started. A token can't be reset: use a fresh one per query.
    """

    RETRY_INTERVAL = 0.02  # seconds between repeated interrupts

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._executing: Context | None = None  # set only while one of our statements runs
        self._watching = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def executing(self) -> bool:
        """True while one of this token's statements is running on a connection."""
        with self._lock:
            return self._executing is not None

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise QueryCancelled()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            ctx = self._executing
            if ctx is None:
                return
            ctx.interrupt()
            if self._watching:
                return
            self._watching = True
        threading.Thread(target=self._keep_interrupting, name="floe-cancel", daemon=True).start()

    def _keep_interrupting(self) -> None:
        while True:
            time.sleep(self.RETRY_INTERVAL)
            with self._lock:
                ctx = self._executing
                if ctx is None:
                    self._watching = False
                    return
                ctx.interrupt()

    @contextlib.contextmanager
    def statement(self, ctx: Context) -> Iterator[None]:
        """Bracket one statement on `ctx` (caller holds `ctx.lock`); raise if cancelled."""
        with self._lock:
            if self._cancelled:
                raise QueryCancelled()
            self._executing = ctx
        try:
            yield
        finally:
            with self._lock:
                self._executing = None


class ConnectionCache:
    """Thread-safe bounded LRU of `Context`s. Eviction drops the reference, never closes."""

    def __init__(self, max_size: int = 16) -> None:
        self.max_size = max(1, int(max_size))
        self._lock = threading.Lock()
        self._items: OrderedDict[ConnectionKey, Context] = OrderedDict()

    def get(self, key: ConnectionKey) -> Context | None:
        with self._lock:
            ctx = self._items.get(key)
            if ctx is not None:
                self._items.move_to_end(key)
            return ctx

    def put(self, key: ConnectionKey, ctx: Context) -> list[Context]:
        """Insert `ctx`; return the evicted contexts (not closed)."""
        evicted: list[Context] = []
        with self._lock:
            self._items[key] = ctx
            self._items.move_to_end(key)
            while len(self._items) > self.max_size:
                _, old = self._items.popitem(last=False)
                evicted.append(old)
        for old in evicted:
            log.info("Evicted connection %s (not closed)", old.key.short())
        return evicted

    def discard(self, key: ConnectionKey, ctx: Context | None = None) -> None:
        """Remove `key` (only if it still maps to `ctx`, when given)."""
        with self._lock:
            current = self._items.get(key)
            if current is not None and (ctx is None or current is ctx):
                del self._items[key]

    def keys(self) -> list[ConnectionKey]:
        with self._lock:
            return list(self._items)

    def contexts(self) -> list[Context]:
        with self._lock:
            return list(self._items.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._items


# --------------------------------------------------------------------------- session


def _status_for(exc: FloeError) -> TableStatus:
    if isinstance(exc, TableNotFound):
        return "not_found"
    if isinstance(exc, CorruptPointer):
        return "corrupt"
    if isinstance(exc, TenantScopeError):
        return "scope_error"
    if isinstance(exc, MissingDataSourceColumn):
        return "missing_data_source"
    return "error"


def _find_parquet_tables(container_dir: Path) -> list[TableKey]:
    """Directories under `container_dir` that directly contain *.parquet files."""
    keys: list[TableKey] = []
    if not container_dir.is_dir():
        return keys
    for dirpath, dirnames, filenames in os.walk(container_dir):
        dirnames.sort()
        if any(f.endswith(".parquet") for f in filenames):
            rel = Path(dirpath).relative_to(container_dir)
            if rel.parts:
                keys.append(TableKey(tuple(rel.parts)))
            dirnames[:] = []  # a table directory is a leaf
    return keys


_UNSAFE_PATH_CHARS = frozenset("/\\\0:*?[")


def _safe_path_element(element: str) -> bool:
    """True if `element` is a plain single directory name (no traversal, no glob)."""
    return (
        bool(element)
        and element not in (".", "..")
        and not os.path.isabs(element)
        and not any(ch in _UNSAFE_PATH_CHARS for ch in element)
    )


class FloeSession:
    """Catalog + query façade for one profile. Thread-safe; used from UI worker threads.

    `secrets` maps secret field names (`adls_account_key`, `adls_client_secret`,
    `nessie_client_secret`) to values. `local_storage_root` and `skip_storage_setup` are
    test hooks: the former makes the container check work on local fixture paths
    (`<root>/<container>/...`), the latter skips the azure/httpfs extensions and secret.
    `clock` (monotonic seconds) is injectable for tests of the tenant-registry back-off.
    """

    def __init__(
        self,
        profile: Profile,
        secrets: Mapping[str, str | None] | None = None,
        nessie_client: NessieClient | None = None,
        *,
        cache: ConnectionCache | None = None,
        local_storage_root: str | Path | None = None,
        skip_storage_setup: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._profile = profile
        self._secrets: dict[str, str | None] = dict(secrets or {})
        for value in self._secrets.values():
            diagnostics.register_secret(value)
        self._cache = cache if cache is not None else ConnectionCache(profile.conn_cache_max)
        self._local_storage_root = Path(local_storage_root) if local_storage_root else None
        self._skip_storage_setup = skip_storage_setup
        self._clock = clock
        if self.is_local:
            self._nessie: NessieClient | None = None
            if not profile.local_fixture_dir:
                raise ValueError("local mode requires local_fixture_dir")
            self._fixture_dir = Path(profile.local_fixture_dir)
        else:
            self._nessie = nessie_client or NessieClient(
                profile, self._secrets.get("nessie_client_secret")
            )
            self._fixture_dir = None
        self._build_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active: dict[int, tuple[CancelToken, str]] = {}  # id -> (token, ref)
        self._active_ids = itertools.count()
        self._registry_lock = threading.Lock()
        self._registry: dict[str, RegistryEntry] | None = None
        self._registry_version: str | None = None  # main head the registry was loaded at
        self._registry_failed: tuple[str, float] | None = None  # (main head, clock) of failure

    # ----- properties ------------------------------------------------------
    @property
    def profile(self) -> Profile:
        return self._profile

    @property
    def nessie(self) -> NessieClient | None:
        return self._nessie

    @property
    def cache(self) -> ConnectionCache:
        return self._cache

    @property
    def is_local(self) -> bool:
        return self._profile.mode == "local"

    @property
    def main_ref(self) -> str:
        return self._profile.nessie_main_ref

    @property
    def catalog_status(self) -> str:
        return "ok" if self._nessie is None else self._nessie.catalog_status

    def is_tenant_ref(self, ref: str) -> bool:
        if self.is_local:
            return ref not in self._profile.shared_containers
        return ref != self.main_ref

    def _is_shared_key(self, key: TableKey) -> bool:
        return key.layer in self._profile.shared_namespaces

    # ----- tenant rules ----------------------------------------------------
    def container_for(self, ref: str) -> str:
        """Expected container for a tenant branch: override → registry → branch name."""
        if self.is_local:
            return ref
        override = self._profile.tenant_container_map.get(ref)
        if override:
            return override
        entry = (self._registry or {}).get(ref)
        if entry is not None and entry.container:
            return entry.container
        return ref

    def data_source_for(self, ref: str) -> str:
        """`data_source` value for a tenant branch: override → registry → branch name."""
        override = self._profile.tenant_data_source_map.get(ref)
        if override:
            return override
        entry = (self._registry or {}).get(ref)
        if entry is not None and entry.data_source:
            return entry.data_source
        return ref

    def _expected_containers(self, ref: str, *, shared: bool) -> list[str]:
        if shared or not self.is_tenant_ref(ref):
            return list(self._profile.shared_containers)
        return [self.container_for(ref)]

    def allowed_containers(self, ref: str) -> list[str]:
        """Every container a connection for `ref` may read: its own + the shared ones."""
        containers = list(self._profile.shared_containers)
        if self.is_tenant_ref(ref):
            containers.insert(0, self.container_for(ref))
        return list(dict.fromkeys(containers))

    # ----- connections -----------------------------------------------------
    def cache_key(self, ref: str) -> ConnectionKey:
        p = self._profile
        if self.is_local:
            return ConnectionKey(p.name, "local", ref, "local", "local", str(self._fixture_dir))
        assert self._nessie is not None
        ref_head = self._nessie.head(ref)
        main_head = ref_head if ref == self.main_ref else self._nessie.head(self.main_ref)
        return ConnectionKey(p.name, "remote", ref, ref_head, main_head, "")

    def context(self, ref: str) -> Context:
        """Get (or build) the Context for the current data version of `ref`."""
        key = self.cache_key(ref)
        with self._build_lock:
            ctx = self._cache.get(key)
            if ctx is not None:
                if self._scope_fits(ctx):
                    return ctx
                log.info("Container mapping for %s changed; rebuilding its connection", ref)
                self._cache.discard(key, ctx)
            start = time.perf_counter()
            conn = self._new_connection()
            ctx = Context(key, conn)
            try:
                if self._nessie is not None:
                    self._nessie.clear_pointers(ref)
                    self._nessie.clear_pointers(self.main_ref)
                self._restrict_file_access(ctx)
            except BaseException:
                conn.close()
                raise
            self._cache.put(key, ctx)
            log.info(
                "Built connection %s in %.0f ms", key.short(), (time.perf_counter() - start) * 1000
            )
            return ctx

    def _scope_fits(self, ctx: Context) -> bool:
        allowed = ctx.allowed_containers
        return allowed is None or set(self.allowed_containers(ctx.ref)) <= allowed

    def _allowed_locations(self, containers: list[str]) -> list[str]:
        """`allowed_directories` entries (prefixes; DuckDB appends a trailing '/')."""
        out: list[str] = []
        roots: list[Path] = []
        if self.is_local:
            assert self._fixture_dir is not None
            roots.append(self._fixture_dir)
        else:
            account = self._profile.adls_account
            for container in containers:
                if account:
                    for scheme in ("abfss", "abfs"):
                        for service in ("dfs", "blob"):
                            out.append(
                                f"{scheme}://{container}@{account}.{service}.core.windows.net/"
                            )
                out.extend((f"az://{container}/", f"azure://{container}/"))
            if self._local_storage_root is not None:
                roots.append(self._local_storage_root)
        for root in roots:
            for base in dict.fromkeys((root.absolute(), root.resolve())):
                for container in containers:
                    if _safe_path_element(container):
                        out.append((base / container).as_posix() + "/")
        return list(dict.fromkeys(out))

    def _restrict_file_access(self, ctx: Context) -> None:
        """Limit DuckDB file access on `ctx` to the ref's container(s), then lock the config.

        Defence in depth behind `check_read_only`: even SQL that slipped through can only
        read the ref's own container and the shared containers. Remote tenant refs need
        the container mapping first, so the tenant registry is loaded before locking.
        Skipped (with a warning) if this DuckDB version lacks the settings, or if the
        profile turned `restrict_file_access` off (escape hatch; the SQL table-function
        blocklist in `check_read_only` still applies).
        """
        if not self._profile.restrict_file_access:
            log.warning(
                "File-access restriction is turned off in profile %s; connection for %s can "
                "read outside its containers (SQL checks still apply)",
                self._profile.name,
                ctx.ref,
            )
            return
        if not self.is_local and self.is_tenant_ref(ctx.ref):
            self._load_registry(ctx)
        containers = self.allowed_containers(ctx.ref)
        locations = self._allowed_locations(containers)
        conn = ctx.connection
        try:
            conn.execute(
                "SET allowed_directories = [" + ", ".join(map(quote_literal, locations)) + "]"
            )
            conn.execute("SET enable_external_access = false")
            conn.execute("SET lock_configuration = true")
        except duckdb.Error as exc:
            log.warning(
                "DuckDB file-access restriction unavailable (%s); relying on SQL checks only",
                type(exc).__name__,
            )
            return
        ctx.allowed_containers = frozenset(containers)
        log.info("Connection for %s restricted to containers %s", ctx.ref, containers)

    def _new_connection(self) -> duckdb.DuckDBPyConnection:
        conn = duckdb.connect(":memory:")
        try:
            if self.is_local:
                p = self._profile
                if p.duckdb_memory_limit:
                    conn.execute(f"SET memory_limit = {quote_literal(p.duckdb_memory_limit)}")
                if p.duckdb_threads:
                    conn.execute(f"SET threads = {int(p.duckdb_threads)}")
                return conn
            ca_bundle = resolve_ca_bundle(self._profile)
            apply_curl_ca_bundle(ca_bundle)
            ext_dir = bundled_extension_dir()
            if ext_dir is not None:
                log.info("Using bundled DuckDB extension directory: %s", ext_dir)
                conn.execute(f"SET extension_directory = {quote_literal(str(ext_dir))}")
            for stmt in build_remote_setup(
                self._profile, self._secrets, ca_bundle, storage=not self._skip_storage_setup
            ):
                log.debug("Connection setup: %s", stmt.loggable)
                try:
                    conn.execute(stmt.sql)
                except duckdb.Error as exc:
                    reason = diagnostics.redact(str(exc).splitlines()[0])
                    if stmt.extension:
                        log.error(
                            "DuckDB extension %s failed to install/load (%s): %s",
                            stmt.extension,
                            type(exc).__name__,
                            reason,
                        )
                        raise ExtensionUnavailable(stmt.extension, reason) from None
                    log.error("Connection setup failed: %s (%s)", stmt.loggable, reason)
                    raise QueryError(f"Connection setup failed: {reason}") from None
            return conn
        except BaseException:
            conn.close()
            raise

    def _invalidate(self, ctx: Context) -> None:
        """Drop `ctx` and forget pointers + heads for its ref and the main ref."""
        self._cache.discard(ctx.key, ctx)
        if self._nessie is not None:
            for ref in {ctx.ref, self.main_ref}:
                self._nessie.clear_pointers(ref)
                self._nessie.clear_heads(ref)
        log.info("Invalidated connection %s", ctx.key.short())

    def _with_rebuild(self, ref: str, fn: Callable[[Context], T]) -> tuple[T, bool]:
        """Run `fn(ctx)`; on a missing metadata.json, rebuild and retry exactly once."""
        ctx = self.context(ref)
        try:
            return fn(ctx), False
        except _MetadataGone as exc:
            log.warning(
                "Connection for %s is stale (%s); catalog changed — rebuilding and retrying once",
                ref,
                "container mapping changed"
                if isinstance(exc, _ScopeChanged)
                else "metadata.json missing",
            )
            self._invalidate(ctx)
        ctx = self.context(ref)
        try:
            return fn(ctx), True
        except _MetadataGone as exc:
            raise QueryError(exc.message) from None

    # ----- activity / cancellation -----------------------------------------
    @contextlib.contextmanager
    def _activity(self, ref: str, token: CancelToken) -> Iterator[None]:
        """Track `token` as an active query on `ref` (for `interrupt` / `is_running`)."""
        entry = next(self._active_ids)
        with self._active_lock:
            self._active[entry] = (token, ref)
        try:
            token.raise_if_cancelled()
            yield
        finally:
            with self._active_lock:
                self._active.pop(entry, None)

    def _locked(self, ctx: Context, token: CancelToken, fn: Callable[[], T]) -> T:
        """Run `fn` holding `ctx.lock`, checking `token` before and after acquiring it."""
        token.raise_if_cancelled()
        with ctx.lock:
            token.raise_if_cancelled()
            return fn()

    def _exec(self, ctx: Context, token: CancelToken, fn: Callable[[], T]) -> T:
        """Run one DuckDB statement (caller holds `ctx.lock`) as a cancellable step."""
        try:
            with token.statement(ctx):
                return fn()
        except duckdb.Error as exc:
            raise self._map_duckdb_error(exc) from None

    # ----- catalog ---------------------------------------------------------
    def _table_info(self, ref: str, key: TableKey, container: str | None = None) -> TableInfo:
        shared = self._is_shared_key(key)
        if self.is_local:
            source_ref = ref
        else:
            source_ref = self.main_ref if (shared or ref == self.main_ref) else ref
        return TableInfo(
            key=key,
            view_name=key.view_name,
            source_ref=source_ref,
            shared=shared,
            container=container,
        )

    def _list_infos(self, ref: str) -> list[TableInfo]:
        infos: list[TableInfo] = []
        shared_ns = set(self._profile.shared_namespaces)
        if self.is_local:
            assert self._fixture_dir is not None
            for key in _find_parquet_tables(self._fixture_dir / ref):
                if self.is_tenant_ref(ref) and key.layer in shared_ns:
                    continue
                infos.append(self._table_info(ref, key, container=ref))
            if self.is_tenant_ref(ref) and shared_ns:
                for container in self._profile.shared_containers:
                    for key in _find_parquet_tables(self._fixture_dir / container):
                        if key.layer in shared_ns:
                            infos.append(self._table_info(ref, key, container=container))
            return infos

        assert self._nessie is not None
        if ref == self.main_ref:
            return [self._table_info(ref, k) for k in self._nessie.list_tables(ref)]
        for key in self._nessie.list_tables(ref):
            if key.layer not in shared_ns:
                infos.append(self._table_info(ref, key))
        if shared_ns:
            for key in self._nessie.list_tables(self.main_ref):
                if key.layer in shared_ns:
                    infos.append(self._table_info(ref, key))
        return infos

    def _ensure_catalog(self, ctx: Context) -> dict[str, TableInfo]:
        """The ref's catalog; keys whose view names collide go to `ctx.collisions`."""
        tables = ctx.tables
        if tables is not None:
            return tables
        groups: dict[str, list[TableInfo]] = {}
        for info in self._list_infos(ctx.ref):
            bucket = groups.setdefault(info.view_name, [])
            if all(other.key != info.key for other in bucket):
                bucket.append(info)  # the same key twice (e.g. two shared containers): first wins
        tables = {}
        collisions: dict[str, tuple[TableInfo, ...]] = {}
        for view, infos in groups.items():
            if len(infos) == 1:
                tables[view] = infos[0]
            else:
                collisions[view] = tuple(infos)
                log.warning(
                    "View name %s is ambiguous on %s (%s); none of these tables is registered",
                    view,
                    ctx.ref,
                    ", ".join(sorted(i.dotted for i in infos)),
                )
        ctx.collisions = collisions
        ctx.tables = tables
        return tables

    @staticmethod
    def _collision_error(
        view: str, infos: tuple[TableInfo, ...] | list[TableInfo]
    ) -> ViewNameCollision:
        return ViewNameCollision(view, sorted({i.dotted for i in infos}))

    def list_tables(self, ref: str) -> list[TableInfo]:
        """Tables visible on `ref` (tenant tables + shared tables from main), with status.

        Tables whose view names collide are listed with status "error" (and a message);
        they can't be registered.
        """
        ctx = self.context(ref)
        tables = self._ensure_catalog(ctx)
        out: list[TableInfo] = []
        for info in tables.values():
            status, error = ctx.status_of(info.view_name)
            out.append(dataclasses.replace(info, status=status, error=error))
        for view, infos in ctx.collisions.items():
            message = diagnostics.redact(self._collision_error(view, infos).user_message())
            for info in infos:
                out.append(dataclasses.replace(info, status="error", error=message))
        out.sort(key=lambda i: (i.shared, i.key.elements))
        return out

    def branches(self) -> list[BranchInfo]:
        """Branches for the picker; `active` comes from the tenant registry if available."""
        if self.is_local:
            assert self._fixture_dir is not None
            names = sorted(
                d.name
                for d in self._fixture_dir.iterdir()
                if d.is_dir() and d.name not in self._profile.shared_containers
            )
            ctx = self.context(names[0]) if names else None
        else:
            assert self._nessie is not None
            names = [r.name for r in self._nessie.list_refs()]
            ctx = self.context(self.main_ref) if self._profile.tenant_registry_table else None
        registry = self._load_registry(ctx) if ctx is not None else None
        result = []
        for name in names:
            entry = registry.get(name) if registry else None
            result.append(BranchInfo(name, entry.active if entry is not None else None))
        return result

    # ----- tenant registry -------------------------------------------------
    def _load_registry(
        self, ctx: Context, token: CancelToken | None = None
    ) -> dict[str, RegistryEntry] | None:
        """Load the tenant registry (if configured) once per main-ref data version.

        A failure is not cached for the version: the registry is retried on a later call,
        after `REGISTRY_RETRY_SECONDS` (branch-name rules apply meanwhile).
        """
        table = self._profile.tenant_registry_table
        if not table:
            return None
        version = ctx.key.main_head
        with self._registry_lock:
            if self._registry_version == version:
                return self._registry
            failed = self._registry_failed
            if (
                failed is not None
                and failed[0] == version
                and self._clock() - failed[1] < REGISTRY_RETRY_SECONDS
            ):
                return None
        token = token if token is not None else CancelToken()
        registry: dict[str, RegistryEntry] = {}
        try:
            key = TableKey.parse(table)
            info = dataclasses.replace(self._table_info(self.main_ref, key), shared=True)
            if self.is_local:
                info = self._locate_local_shared(info)
            source, container = self._resolve_source(info)
            self._check_container(self.main_ref, info, container, shared=True)
            with ctx.lock:
                df = self._exec(
                    ctx, token, lambda: ctx.connection.execute(f"SELECT * FROM {source}").df()
                )
            cols = {c.lower(): c for c in df.columns}
            for row in df.to_dict("records"):
                branch = row.get(cols.get("branch", "branch"))
                if branch is None or (isinstance(branch, float) and pd.isna(branch)):
                    continue
                active = row.get(cols.get("active", "active"))
                container_val = row.get(cols.get("container", "container"))
                ds = row.get(cols["data_source"]) if "data_source" in cols else None
                registry[str(branch)] = RegistryEntry(
                    branch=str(branch),
                    container=str(container_val) if isinstance(container_val, str) else None,
                    active=None if active is None or pd.isna(active) else bool(active),
                    data_source=str(ds) if isinstance(ds, str) else None,
                )
        except QueryCancelled:
            raise
        except (FloeError, duckdb.Error) as exc:
            log.warning(
                "Tenant registry %s unavailable (%s); using branch-name rules, retrying in %.0f s",
                table,
                type(exc).__name__,
                REGISTRY_RETRY_SECONDS,
            )
            with self._registry_lock:
                self._registry = None
                self._registry_version = None
                self._registry_failed = (version, self._clock())
            return None
        log.info("Loaded tenant registry %s: %d branches", table, len(registry))
        with self._registry_lock:
            self._registry = registry
            self._registry_version = version
            self._registry_failed = None
        return registry

    # ----- registration ----------------------------------------------------
    def _local_table_dir(self, container: str | None, info: TableInfo) -> tuple[Path, str]:
        """Resolved directory of a local table and the container it really lives in.

        Rejects key elements that could escape the container (empty, `.`, `..`, absolute,
        containing a path separator or glob character) and anything resolving outside
        `local_fixture_dir` (e.g. through a symlink).
        """
        assert self._fixture_dir is not None
        expected = self._expected_containers(info.source_ref, shared=info.shared)
        elements = (container or "", *info.key.elements)
        if not all(_safe_path_element(e) for e in elements):
            raise TenantScopeError(info.source_ref, info.dotted, "<invalid path>", expected)
        root = self._fixture_dir.resolve()
        table_dir = root.joinpath(*elements).resolve()
        actual = container_of(str(table_dir), local_root=root)
        if actual is None:
            raise TenantScopeError(
                info.source_ref, info.dotted, "<outside local_fixture_dir>", expected
            )
        return table_dir, actual

    def _locate_local_shared(self, info: TableInfo) -> TableInfo:
        for container in self._profile.shared_containers:
            table_dir, _ = self._local_table_dir(container, info)
            if table_dir.is_dir():
                return dataclasses.replace(info, container=container)
        raise TableNotFound(info.source_ref, info.dotted)

    def _resolve_source(self, info: TableInfo) -> tuple[str, str | None]:
        """Return (SQL table expression, container of the files)."""
        if self.is_local:
            table_dir, container = self._local_table_dir(info.container, info)
            if not table_dir.is_dir():
                raise TableNotFound(info.source_ref, info.dotted)
            pattern = (table_dir / "*.parquet").as_posix()
            return f"read_parquet({quote_literal(pattern)})", container
        assert self._nessie is not None
        pointer = self._nessie.pointer(info.source_ref, info.key)
        location = pointer.metadata_location
        container = container_of(location, local_root=self._local_storage_root)
        return f"iceberg_scan({quote_literal(location)})", container

    def _check_container(
        self, ref: str, info: TableInfo, container: str | None, *, shared: bool
    ) -> None:
        expected = self._expected_containers(ref, shared=shared)
        if container is None or container not in expected:
            raise TenantScopeError(ref, info.dotted, container or "<unknown>", expected)

    def _register_in(
        self,
        ctx: Context,
        info: TableInfo,
        tenant_filter: bool,
        token: CancelToken | None = None,
    ) -> str:
        token = token if token is not None else CancelToken()
        ref = ctx.ref
        view = info.view_name
        apply_filter = bool(tenant_filter) and self.is_tenant_ref(ref) and not info.shared
        current = ctx.registered_key(view)
        if current is not None and current != info.key:
            raise self._collision_error(view, [info, self._table_info(ref, current)])
        if current is not None and ctx.registered(view) == apply_filter:
            return view
        try:
            if view in ctx.collisions:
                raise self._collision_error(view, ctx.collisions[view])
            with ctx.lock:
                if self.is_tenant_ref(ref):
                    self._load_registry(ctx, token)  # preferred mapping source, if configured
                source, container = self._resolve_source(info)
                self._check_container(ref, info, container, shared=info.shared)
                if ctx.allowed_containers is not None and container not in ctx.allowed_containers:
                    raise _ScopeChanged(f"Container mapping for {ref} changed")
                sql = f"CREATE OR REPLACE VIEW {quote_identifier(view)} AS SELECT * FROM {source}"
                try:
                    if apply_filter:
                        cols = self._exec(
                            ctx,
                            token,
                            lambda: ctx.connection.execute(
                                f"DESCRIBE SELECT * FROM {source}"
                            ).fetchall(),
                        )
                        if DATA_SOURCE_COLUMN not in {str(c[0]).lower() for c in cols}:
                            raise MissingDataSourceColumn(ref, info.dotted)
                        sql += f" WHERE {DATA_SOURCE_COLUMN} = "
                        sql += quote_literal(self.data_source_for(ref))
                    self._exec(ctx, token, lambda: ctx.connection.execute(sql))
                except BaseException:
                    # Never leave a stale (e.g. unfiltered) view behind after a failure.
                    try:
                        ctx.connection.execute(f"DROP VIEW IF EXISTS {quote_identifier(view)}")
                    except duckdb.Error:
                        pass
                    ctx.mark_unregistered(view)
                    raise
                ctx.mark_registered(view, apply_filter, info.key)
        except (QueryCancelled, _MetadataGone):
            raise
        except FloeError as exc:
            ctx.set_status(view, _status_for(exc), diagnostics.redact(exc.user_message()))
            log.info("Registration of %s @ %s failed: %s", info.dotted, ref, type(exc).__name__)
            raise
        log.info(
            "Registered %s @ %s as %s (tenant filter %s)",
            info.dotted,
            info.source_ref,
            view,
            "on" if apply_filter else "off",
        )
        return view

    def _info_for_key(self, ctx: Context, key: TableKey) -> TableInfo:
        tables = self._ensure_catalog(ctx)
        view = key.view_name
        if view in ctx.collisions:
            raise self._collision_error(view, ctx.collisions[view])
        info = tables.get(view)
        if info is not None:
            if info.key == key:
                return info
            raise self._collision_error(view, [info, self._table_info(ctx.ref, key)])
        info = self._table_info(ctx.ref, key)
        if self.is_local:
            if info.shared:
                info = self._locate_local_shared(info)
            else:
                info = dataclasses.replace(info, container=ctx.ref)
        return info

    def register(
        self,
        ref: str,
        key: TableKey | str,
        tenant_filter: bool = True,
        *,
        cancel_token: CancelToken | None = None,
    ) -> str:
        """Register `key`'s view on `ref`'s current connection; return the view name."""
        table_key = TableKey.parse(key) if isinstance(key, str) else key
        token = cancel_token if cancel_token is not None else CancelToken()

        def run(ctx: Context) -> str:
            self._ensure_catalog(ctx)
            return self._locked(
                ctx,
                token,
                lambda: self._register_in(
                    ctx, self._info_for_key(ctx, table_key), tenant_filter, token
                ),
            )

        with self._activity(ref, token):
            view, _ = self._with_rebuild(ref, run)
        return view

    def _enforce_filter_mode(self, ctx: Context, tenant_filter: bool, token: CancelToken) -> None:
        """Drop registered views whose filter mode differs (caller holds `ctx.lock`)."""
        tables = ctx.tables or {}
        for view, filtered in ctx.registered_views().items():
            info = tables.get(view)
            shared = info.shared if info is not None else False
            wanted = bool(tenant_filter) and self.is_tenant_ref(ctx.ref) and not shared
            if filtered != wanted:
                self._exec(
                    ctx,
                    token,
                    lambda v=view: ctx.connection.execute(
                        f"DROP VIEW IF EXISTS {quote_identifier(v)}"
                    ),
                )
                ctx.mark_unregistered(view)

    def _resolve_in(
        self, ctx: Context, sql: str, tenant_filter: bool, token: CancelToken
    ) -> list[str]:
        """Register the views `sql` references (caller holds `ctx.lock`)."""
        tables = self._ensure_catalog(ctx)
        self._enforce_filter_mode(ctx, tenant_filter, token)

        def referenced(view: str) -> bool:
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(view) + r"(?![A-Za-z0-9_])"
            return re.search(pattern, sql, flags=re.IGNORECASE) is not None

        for view, infos in ctx.collisions.items():
            if referenced(view):
                raise self._collision_error(view, infos)
        registered: list[str] = []
        for view, info in tables.items():
            if not referenced(view):
                continue
            if ctx.registered(view) is None:
                registered.append(view)
            self._register_in(ctx, info, tenant_filter, token)
        return registered

    def resolve_sql_references(
        self,
        ref: str,
        sql: str,
        tenant_filter: bool = True,
        *,
        cancel_token: CancelToken | None = None,
    ) -> list[str]:
        """Register every known view referenced in `sql` (whole word, case-insensitive).

        Returns the view names that were newly registered.
        """
        token = cancel_token if cancel_token is not None else CancelToken()

        def run(ctx: Context) -> list[str]:
            self._ensure_catalog(ctx)
            return self._locked(
                ctx, token, lambda: self._resolve_in(ctx, sql, tenant_filter, token)
            )

        with self._activity(ref, token):
            result, _ = self._with_rebuild(ref, run)
        return result

    # ----- queries ---------------------------------------------------------
    def _map_duckdb_error(self, exc: BaseException) -> FloeError:
        if isinstance(exc, duckdb.InterruptException):
            return QueryCancelled()
        message = diagnostics.redact(str(exc))
        if is_missing_metadata_error(exc):
            return _MetadataGone(message)
        if isinstance(exc, duckdb.PermissionException):
            return QueryError(
                "Access denied: Floe only reads files in this branch's container and the "
                f"shared containers. ({message})"
            )
        if not self.is_local and is_storage_auth_error(exc):
            return AdlsAuthError(self._profile.adls_account, self._profile.adls_auth)
        return QueryError(message)

    def _execute_df(self, ctx: Context, sql: str, token: CancelToken) -> pd.DataFrame:
        """Run the user's statement (caller holds `ctx.lock`)."""
        df = self._exec(ctx, token, lambda: ctx.connection.execute(sql).df())
        token.raise_if_cancelled()  # cancelled while finishing: the user asked to stop
        return df

    def query(
        self,
        ref: str,
        sql: str,
        limit: int | None = None,
        tenant_filter: bool = True,
        *,
        cancel_token: CancelToken | None = None,
    ) -> QueryResult:
        """Run one read-only statement on `ref`, registering referenced views first.

        View resolution, filter-mode enforcement and execution happen atomically under the
        connection's lock. Pass `cancel_token=CancelToken()` and call `token.cancel()` from
        another thread to cancel this query only; it then raises `QueryCancelled`.
        """
        text = check_read_only(sql)
        run_sql = text if limit is None else _wrap_limit(text, limit)
        token = cancel_token if cancel_token is not None else CancelToken()
        start = time.perf_counter()

        def run(ctx: Context) -> pd.DataFrame:
            self._ensure_catalog(ctx)  # may hit Nessie: done before taking the lock

            def body() -> pd.DataFrame:
                self._resolve_in(ctx, text, tenant_filter, token)
                return self._execute_df(ctx, run_sql, token)

            return self._locked(ctx, token, body)

        try:
            with self._activity(ref, token):
                df, reloaded = self._with_rebuild(ref, run)
        except FloeError as exc:
            log.info(
                "Query on %s failed after %.0f ms: %s",
                ref,
                (time.perf_counter() - start) * 1000,
                type(exc).__name__,
            )
            raise
        truncated = limit is not None and len(df) > limit
        if truncated:
            df = df.iloc[: int(limit)].reset_index(drop=True)
        elapsed = time.perf_counter() - start
        log.info(
            "Query on %s: %.0f ms, %d rows%s%s",
            ref,
            elapsed * 1000,
            len(df),
            " (truncated)" if truncated else "",
            " (reloaded)" if reloaded else "",
        )
        return QueryResult(df=df, truncated=truncated, elapsed=elapsed, reloaded=reloaded)

    def preview(
        self,
        ref: str,
        view_name: str,
        tenant_filter: bool = True,
        *,
        cancel_token: CancelToken | None = None,
    ) -> QueryResult:
        """First `preview_row_limit` rows of a view."""
        return self.query(
            ref,
            f"SELECT * FROM {quote_identifier(view_name)}",
            limit=self._profile.preview_row_limit,
            tenant_filter=tenant_filter,
            cancel_token=cancel_token,
        )

    def schema(
        self,
        ref: str,
        view_name: str,
        tenant_filter: bool = True,
        *,
        cancel_token: CancelToken | None = None,
    ) -> list[ColumnInfo]:
        """Columns of a view (registering it first if it's a known table)."""
        token = cancel_token if cancel_token is not None else CancelToken()

        def run(ctx: Context) -> list[ColumnInfo]:
            tables = self._ensure_catalog(ctx)

            def body() -> pd.DataFrame:
                info = tables.get(view_name)
                if info is not None:
                    self._enforce_filter_mode(ctx, tenant_filter, token)
                    self._register_in(ctx, info, tenant_filter, token)
                elif view_name in ctx.collisions:
                    raise self._collision_error(view_name, ctx.collisions[view_name])
                elif ctx.registered(view_name) is None:
                    raise TableNotFound(ref, view_name)
                return self._execute_df(ctx, f"DESCRIBE {quote_identifier(view_name)}", token)

            df = self._locked(ctx, token, body)
            return [
                ColumnInfo(str(r["column_name"]), str(r["column_type"]), r["null"] != "NO")
                for r in df.to_dict("records")
            ]

        with self._activity(ref, token):
            columns, _ = self._with_rebuild(ref, run)
        return columns

    # ----- table metadata (Timeline, SPEC §15.7) ----------------------------
    def with_table_metadata(
        self,
        ref: str,
        key: TableKey | str,
        load: Callable[[str, Callable[[], bytes]], T],
        *,
        cancel_token: CancelToken | None = None,
    ) -> tuple[TableInfo, T]:
        """Resolve `key`'s current metadata.json on `ref` and hand it to `load`.

        Resolution is exactly view registration's (SPEC §6.3): the key's catalog entry
        (shared namespaces from the main ref), its Nessie pointer (`TableNotFound`,
        `CorruptPointer`) and the container check (`TenantScopeError`). Then
        `load(location, read)` runs, where `read()` returns the raw bytes of that one
        metadata.json, read through DuckDB on a cursor of the ref's restricted connection
        (same `allowed_directories`, no connection lock needed). `location` is an opaque
        cache key for `load`; it must never leave the core layer. Only files named
        `*.metadata.json` are ever read — never manifests or data files. A metadata.json
        that disappeared (catalog rebuilt) is retried once on a fresh connection.
        """
        if self.is_local or self._nessie is None:
            raise TimelineUnavailable()
        table_key = TableKey.parse(key) if isinstance(key, str) else key
        token = cancel_token if cancel_token is not None else CancelToken()
        nessie = self._nessie

        def run(ctx: Context) -> tuple[TableInfo, T]:
            self._ensure_catalog(ctx)
            info = self._info_for_key(ctx, table_key)
            if self.is_tenant_ref(ctx.ref):
                self._load_registry(ctx, token)
            token.raise_if_cancelled()
            location = nessie.pointer(info.source_ref, info.key).metadata_location
            container = container_of(location, local_root=self._local_storage_root)
            self._check_container(ctx.ref, info, container, shared=info.shared)
            if ctx.allowed_containers is not None and container not in ctx.allowed_containers:
                raise _ScopeChanged(f"Container mapping for {ctx.ref} changed")
            return info, load(
                location, lambda: self._read_metadata_file(ctx, info, location, token)
            )

        with self._activity(ref, token):
            result, _ = self._with_rebuild(ref, run)
        return result

    def _read_metadata_file(
        self, ctx: Context, info: TableInfo, location: str, token: CancelToken
    ) -> bytes:
        """Bytes of one `*.metadata.json` via `read_blob` on a cursor of `ctx`'s connection.

        The cursor shares the connection's database config, so the file-access
        restriction (`allowed_directories`, locked) applies. Errors never carry the path.
        """
        name = metadata_file_name(location)
        if not name.endswith(".metadata.json") or any(ch in location for ch in "*?[]{}"):
            raise MetadataReadError(info.dotted, "not a metadata.json file")
        cursor = ctx.connection.cursor()
        reader = Context(ctx.key, cursor)  # so a cancel interrupts this cursor only
        try:
            with token.statement(reader):
                rows = cursor.execute(
                    "SELECT content FROM read_blob(?)", [location]
                ).fetchall()
        except duckdb.InterruptException:
            raise QueryCancelled() from None
        except duckdb.PermissionException:
            raise MetadataReadError(info.dotted, "access denied") from None
        except duckdb.Error as exc:
            if is_missing_metadata_error(exc):
                raise _MetadataGone(
                    f"The metadata of {info.dotted} no longer exists (catalog changed)."
                ) from None
            if not self.is_local and is_storage_auth_error(exc):
                raise AdlsAuthError(self._profile.adls_account, self._profile.adls_auth) from None
            raise MetadataReadError(info.dotted, type(exc).__name__) from None
        finally:
            try:
                cursor.close()
            except duckdb.Error:  # pragma: no cover
                pass
        token.raise_if_cancelled()
        if not rows:
            raise _MetadataGone(
                f"The metadata of {info.dotted} no longer exists (catalog changed)."
            )
        if len(rows) != 1:
            raise MetadataReadError(info.dotted, "ambiguous location")
        content = rows[0][0]
        log.info("Read metadata of %s @ %s (%d bytes)", info.dotted, info.source_ref, len(content))
        return bytes(content)

    # ----- heads / refresh -------------------------------------------------
    def current_head(self, ref: str) -> str | None:
        """The ref's current head hash (the data version), or None in local mode.

        Uses the Nessie client's head cache (TTL `nessie_head_ttl_seconds`), so it may do
        I/O: call it from a worker thread. Returns None if the head can't be determined.
        """
        if self._nessie is None:
            return None
        try:
            return self._nessie.head(ref)
        except FloeError as exc:
            log.warning("Head of %s unavailable (%s)", ref, type(exc).__name__)
            return None

    def refresh(self) -> None:
        """Expire cached heads (remote) / drop catalog listings (local) so the next call re-reads.

        Heads are re-fetched on next use; the last known hash stays as the fallback if Nessie
        is unreachable ("stale" catalog status). A moved head builds a fresh connection (and
        so fresh pointers) automatically; in local mode the connection key never changes, so
        the listing is dropped instead.
        """
        if self._nessie is not None:
            self._nessie.expire_heads()
            return
        for ctx in self._cache.contexts():
            ctx.tables = None
            ctx.collisions = {}

    def interrupt(self, ref: str) -> None:
        """Cancel every active (running or waiting) query on `ref`. Never takes a query lock."""
        with self._active_lock:
            targets = [token for token, r in self._active.values() if r == ref]
        for token in targets:
            token.cancel()

    def interrupt_all(self) -> None:
        """Cancel every active query of this session."""
        with self._active_lock:
            targets = [token for token, _ in self._active.values()]
        for token in targets:
            token.cancel()

    def is_running(self, ref: str | None = None) -> bool:
        """True while a query on `ref` (any ref if None) is active (running or waiting)."""
        with self._active_lock:
            return any(ref is None or r == ref for _, r in self._active.values())
