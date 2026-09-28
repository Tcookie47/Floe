"""Read-only Nessie REST v2 client: token cache, refs, heads, entries, pointers, commit log.

SPEC §5 and §15.7 (branch timeline).

Uses stdlib `urllib` only. No write operations exist here, by design.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, Literal

from floe.core import diagnostics
from floe.core.errors import (
    CorruptPointer,
    FloeError,
    NessieAuthError,
    NessieUnreachable,
    TableNotFound,
)
from floe.core.profiles import Profile

log = logging.getLogger("floe.nessie")

TOKEN_REFRESH_MARGIN_SECONDS = 60.0
ENTRIES_PAGE_SIZE = 500
HISTORY_PAGE_SIZE = 50
MAX_HISTORY_PAGE_SIZE = 250
KEY_ELEMENT_SEP = "\u001d"

CatalogStatus = Literal["unknown", "ok", "stale"]


class NessieHTTPError(FloeError):
    """A non-auth, non-404-handled HTTP error from Nessie (e.g. 5xx)."""

    def __init__(self, path: str, status: int) -> None:
        self.path = path
        self.status = status
        super().__init__(f"Nessie returned HTTP {status} for {path}")

    def user_message(self) -> str:
        return f"Nessie error (HTTP {self.status})"


@dataclass(frozen=True)
class Ref:
    name: str
    type: str
    hash: str


@dataclass(frozen=True)
class TableKey:
    elements: tuple[str, ...]

    @classmethod
    def parse(cls, dotted: str) -> TableKey:
        return cls(tuple(dotted.split(".")))

    @property
    def dotted(self) -> str:
        return ".".join(self.elements)

    @property
    def view_name(self) -> str:
        return "_".join(self.elements)

    @property
    def layer(self) -> str:
        return self.elements[0]

    def path_segment(self) -> str:
        """Nessie v2 path encoding: '.'-joined, in-element '.' → \\u001D, URL-quoted."""
        joined = ".".join(e.replace(".", KEY_ELEMENT_SEP) for e in self.elements)
        return urllib.parse.quote(joined, safe="")


@dataclass(frozen=True)
class CommitInfo:
    """One Nessie commit. `touched` is None when the server didn't return operations."""

    hash: str
    time: datetime | None  # commit time, UTC
    author: str | None
    committer: str | None
    message: str
    touched: tuple[TableKey, ...] | None
    author_time: datetime | None = None


@dataclass(frozen=True)
class HistoryPage:
    entries: list[CommitInfo]
    next_token: str | None
    operations_available: bool


_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_time(value: object) -> datetime | None:
    """ISO-8601 instant (Nessie's `commitTime`, up to nanoseconds) → aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = _FRACTION.sub(r"\1", value.strip())
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


_TOUCHING_OPS = frozenset({"PUT", "DELETE"})
_NON_TABLE_CONTENT = frozenset({"NAMESPACE", "ICEBERG_VIEW", "DELTA_LAKE_TABLE", "UDF"})


def _touched_keys(operations: object) -> tuple[TableKey, ...]:
    """Table keys a commit's operations PUT or DELETE (namespaces / views skipped)."""
    keys: list[TableKey] = []
    if not isinstance(operations, list):
        return ()
    for op in operations:
        if not isinstance(op, dict) or str(op.get("type", "")).upper() not in _TOUCHING_OPS:
            continue
        content = op.get("content")
        if isinstance(content, dict) and str(content.get("type", "")).upper() in _NON_TABLE_CONTENT:
            continue
        elements = (op.get("key") or {}).get("elements")
        if not isinstance(elements, list) or not elements:
            continue
        key = TableKey(tuple(str(e) for e in elements))
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def parse_log_entry(entry: dict[str, Any], with_operations: bool) -> CommitInfo:
    meta = entry.get("commitMeta") or {}
    authors = meta.get("authors")
    if isinstance(authors, list) and authors:
        author = ", ".join(str(a) for a in authors if a)
    else:
        author = meta.get("author")
    committer = meta.get("committer")
    touched = (
        _touched_keys(entry["operations"])
        if with_operations and entry.get("operations") is not None
        else None
    )
    return CommitInfo(
        hash=str(meta.get("hash") or entry.get("hash") or ""),
        time=parse_time(meta.get("commitTime")),
        author=str(author) if author else None,
        committer=str(committer) if committer else None,
        message=str(meta.get("message") or ""),
        touched=touched,
        author_time=parse_time(meta.get("authorTime")),
    )


@dataclass(frozen=True)
class Pointer:
    metadata_location: str
    snapshot_id: int
    content_id: str | None


def container_of(location: str, local_root: Path | None = None) -> str | None:
    """Return the storage container a table location lives in.

    - `abfss://<container>@<account>.dfs.core.windows.net/...` (also `abfs://`, `az://`
      with the same `container@account` form, or `az://<container>/...`) → `<container>`.
    - Local paths / `file://` URIs → the first path component under `local_root`
      if given and the path is inside it, else None.
    """
    parsed = urllib.parse.urlsplit(location)
    scheme = parsed.scheme.lower()
    if scheme in ("abfss", "abfs", "az", "azure"):
        netloc = parsed.netloc
        if "@" in netloc:
            container = netloc.split("@", 1)[0]
        else:
            container = netloc
        return container or None

    if local_root is None:
        return None
    raw = local_path_of(location)
    if raw is None:
        return None
    return first_component_under(Path(raw).resolve(), Path(local_root).resolve())


_DRIVE_PATH = re.compile(r"^/?([A-Za-z]:)([/\\].*)?$")


def local_path_of(location: str, *, windows: bool | None = None) -> str | None:
    """Local filesystem path of a plain path or `file:` URI, else None (remote schemes).

    Handles the Windows forms too (`windows` defaults to the running OS): `C:\\x`,
    `C:/x`, `file:///C:/x` (whose URI path is `/C:/x`), `file://C:/x` and UNC
    `file://host/share/x`. On POSIX a `file://host/...` URI for a non-local host is None.
    """
    if windows is None:
        windows = os.name == "nt"
    parsed = urllib.parse.urlsplit(location)
    scheme = parsed.scheme.lower()
    if scheme == "":
        return location
    if len(scheme) == 1:  # a Windows drive letter (`C:\\x`), or a relative path on POSIX
        return location
    if scheme != "file":
        return None
    path = urllib.parse.unquote(parsed.path)
    host = parsed.netloc
    if host.lower() in ("", "localhost"):
        if windows:
            m = _DRIVE_PATH.match(path)
            if m:  # `/C:/x` -> `C:/x`
                return m.group(1) + (m.group(2) or "/")
        return path
    if windows:
        if re.fullmatch(r"[A-Za-z]:", host):  # `file://C:/x`
            return host + (path or "/")
        return f"//{host}{path}"  # UNC
    return None


def first_component_under(path: PurePath, root: PurePath) -> str | None:
    """First path component of `path` below `root`, or None if it is not strictly inside.

    Uses the path flavour's own comparison, so Windows paths match case-insensitively.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


SYSTEM_CA_BUNDLE = "/etc/ssl/cert.pem"  # macOS system root bundle
_ssl_context_lock = threading.Lock()
_ssl_context: ssl.SSLContext | None = None


def default_ssl_context() -> ssl.SSLContext:
    """TLS context for Nessie / token requests, with a usable CA store when frozen.

    A PyInstaller-frozen app (or a python.org Python without "Install Certificates")
    carries an OpenSSL whose compiled-in CA path doesn't exist on the user's Mac, so the
    default context trusts nothing and every HTTPS request (e.g. the Azure AD token
    endpoint) fails certificate verification. If the default store is empty, fall back to
    the system bundle, then to certifi's. `SSL_CERT_FILE` / `SSL_CERT_DIR` still apply.
    """
    global _ssl_context
    with _ssl_context_lock:
        if _ssl_context is None:
            ctx = ssl.create_default_context()
            if ctx.cert_store_stats().get("x509_ca", 0) == 0:
                fallback: str | None = None
                if os.path.exists(SYSTEM_CA_BUNDLE):
                    fallback = SYSTEM_CA_BUNDLE
                else:
                    try:
                        import certifi

                        fallback = certifi.where()
                    except ImportError:
                        pass
                if fallback:
                    ctx.load_verify_locations(cafile=fallback)
                    log.info("Default TLS CA store is empty; using %s", fallback)
            _ssl_context = ctx
        return _ssl_context


def _api_base(uri: str) -> str:
    base = uri.rstrip("/")
    if base.endswith("/api/v2"):
        return base
    if base.endswith("/api/v1"):
        return base[: -len("/api/v1")] + "/api/v2"
    if "/api/" in base:
        return base
    return base + "/api/v2"


def _filename(location: str) -> str:
    return PurePosixPath(urllib.parse.urlsplit(location).path).name


def metadata_file_name(location: str) -> str:
    """The file name of a table location (no directories, no container / account)."""
    return _filename(location)


class NessieClient:
    """Thread-safe, read-only Nessie v2 client bound to one profile."""

    def __init__(
        self,
        profile: Profile,
        client_secret: str | None,
        *,
        timeout: float = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._profile = profile
        self._client_secret = client_secret
        diagnostics.register_secret(client_secret)
        self._timeout = timeout
        self._clock = clock
        self._base = _api_base(profile.nessie_uri)

        self._token_lock = threading.Lock()
        self._token: str | None = None
        self._token_expiry = 0.0

        self._head_lock = threading.Lock()
        self._heads: dict[str, tuple[str, float]] = {}  # ref -> (hash, fetched_at)
        self._stale_refs: set[str] = set()  # refs currently served from last-known head
        self._ever_ok = False  # at least one successful head refresh has ever happened

        self._pointer_lock = threading.Lock()
        self._pointers: dict[tuple[str, str], Pointer] = {}

        # False once the server rejected `fetch=ALL` or ignored it (no operations):
        # the commit log is then requested message-only from then on.
        self._history_operations: bool | None = None

    # ----- status ----------------------------------------------------------
    @property
    def catalog_status(self) -> CatalogStatus:
        with self._head_lock:
            if self._stale_refs:
                return "stale"
            if self._ever_ok:
                return "ok"
            return "unknown"

    def catalog_status_for(self, ref: str) -> CatalogStatus:
        with self._head_lock:
            if ref in self._stale_refs:
                return "stale"
            if ref in self._heads:
                return "ok"
            return "unknown"

    # ----- token -----------------------------------------------------------
    def _auth_enabled(self) -> bool:
        return self._profile.nessie_auth != "none"

    def ensure_token(self) -> str:
        """Acquire (and cache) a token. Raises `NessieAuthError` on failure.

        Public wrapper around the token acquisition used by "Test connection"
        (SPEC §8.3 step 2); no-op-ish for `nessie_auth=none` callers, who should
        check `NessieClient` isn't needed at all in that case.
        """
        return self._get_token()

    def _get_token(self, force: bool = False) -> str:
        with self._token_lock:
            if (
                not force
                and self._token is not None
                and self._token_expiry - self._clock() >= TOKEN_REFRESH_MARGIN_SECONDS
            ):
                return self._token
            # Held across the token request on purpose: concurrent callers wait
            # for one refresh instead of all requesting a token.
            token, expires_in = self._request_token()
            self._token = token
            self._token_expiry = self._clock() + expires_in
            return token

    def _request_token(self) -> tuple[str, float]:
        endpoint = self._profile.nessie_token_endpoint
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._profile.nessie_client_id,
                "client_secret": self._client_secret or "",
                "scope": self._profile.nessie_scope,
            }
        ).encode("ascii")
        req = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        status, payload = self._send(req, endpoint)
        if status != 200:
            raise NessieAuthError(endpoint, status)
        try:
            data = json.loads(payload)
            token = str(data["access_token"])
            expires_in = float(data.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError):
            raise NessieAuthError(endpoint, status) from None
        diagnostics.register_secret(token)
        return token, expires_in

    # ----- HTTP ------------------------------------------------------------
    def _send(self, req: urllib.request.Request, uri_for_errors: str) -> tuple[int, bytes]:
        """Perform one request; return (status, body). Logs method/path/status/ms only."""
        path = urllib.parse.urlsplit(req.full_url).path
        method = req.get_method()
        start = time.perf_counter()
        status = 0
        try:
            try:
                with urllib.request.urlopen(
                    req, timeout=self._timeout, context=default_ssl_context()
                ) as resp:
                    status = resp.status
                    return status, resp.read()
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    body = exc.read()
                except Exception:
                    body = b""
                finally:
                    exc.close()
                return status, body
            except urllib.error.URLError as exc:
                raise NessieUnreachable(uri_for_errors, _reason(exc.reason)) from None
            except (TimeoutError, ConnectionError, OSError) as exc:
                raise NessieUnreachable(uri_for_errors, _reason(exc)) from None
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            log.info("%s %s -> %s (%.0f ms)", method, path, status or "ERR", elapsed_ms)

    def _get_json(self, rel_path: str, query: dict[str, Any] | None = None) -> tuple[int, Any]:
        """GET `<base>/<rel_path>`; handles auth + retry-once on 401.

        Returns (status, parsed JSON or None) for 2xx/404. Raises for other errors.
        """
        url = f"{self._base}/{rel_path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        path = urllib.parse.urlsplit(url).path

        for attempt in range(2):
            headers = {"Accept": "application/json"}
            if self._auth_enabled():
                headers["Authorization"] = "Bearer " + self._get_token(force=attempt > 0)
            req = urllib.request.Request(url, method="GET", headers=headers)
            status, body = self._send(req, self._profile.nessie_uri)
            if status == 401 and self._auth_enabled():
                if attempt == 0:
                    continue
                raise NessieAuthError(self._profile.nessie_uri, status)
            if status == 401:
                raise NessieAuthError(self._profile.nessie_uri, status)
            if 200 <= status < 300 or status == 404:
                try:
                    return status, json.loads(body) if body else None
                except ValueError:
                    if status == 404:
                        return status, None
                    raise NessieHTTPError(path, status) from None
            raise NessieHTTPError(path, status)
        raise AssertionError("unreachable")  # pragma: no cover

    # ----- refs & heads ----------------------------------------------------
    def list_refs(self) -> list[Ref]:
        status, data = self._get_json("trees")
        if status != 200:
            raise NessieHTTPError("trees", status)
        return [
            Ref(name=r["name"], type=r.get("type", "BRANCH"), hash=r.get("hash", ""))
            for r in (data or {}).get("references", [])
        ]

    def head(self, ref: str) -> str:
        now = self._clock()
        ttl = self._profile.nessie_head_ttl_seconds
        with self._head_lock:
            cached = self._heads.get(ref)
            if cached is not None and now - cached[1] < ttl:
                return cached[0]

        try:
            status, data = self._get_json(f"trees/{urllib.parse.quote(ref, safe='')}")
            if status != 200:
                raise NessieHTTPError(f"trees/{ref}", status)
            new_hash = str(data["reference"]["hash"])
        except (NessieUnreachable, NessieHTTPError) as exc:
            transient = isinstance(exc, NessieUnreachable) or exc.status >= 500
            with self._head_lock:
                last = self._heads.get(ref)
                if transient and last is not None:
                    self._stale_refs.add(ref)
                    log.warning(
                        "Head refresh for %s failed (%s); using last known head %s",
                        ref,
                        type(exc).__name__,
                        last[0][:12],
                    )
                    return last[0]
            raise

        with self._head_lock:
            self._heads[ref] = (new_hash, self._clock())
            self._stale_refs.discard(ref)
            self._ever_ok = True
        return new_hash

    def expire_heads(self, ref: str | None = None) -> None:
        """Force the next `head()` to re-fetch, but keep the last known hash as a fallback."""
        with self._head_lock:
            for name in list(self._heads) if ref is None else [ref]:
                entry = self._heads.get(name)
                if entry is not None:
                    self._heads[name] = (entry[0], float("-inf"))

    def clear_heads(self, ref: str | None = None) -> None:
        with self._head_lock:
            if ref is None:
                self._heads.clear()
                self._stale_refs.clear()
            else:
                self._heads.pop(ref, None)
                self._stale_refs.discard(ref)

    # ----- tables ----------------------------------------------------------
    def list_tables(self, ref: str) -> list[TableKey]:
        keys: list[TableKey] = []
        page_token: str | None = None
        rel = f"trees/{urllib.parse.quote(ref, safe='')}/entries"
        while True:
            query: dict[str, Any] = {"max-records": ENTRIES_PAGE_SIZE}
            if page_token:
                query["page-token"] = page_token
            status, data = self._get_json(rel, query)
            if status != 200:
                raise NessieHTTPError(rel, status)
            data = data or {}
            for entry in data.get("entries", []):
                if entry.get("type") == "ICEBERG_TABLE":
                    keys.append(TableKey(tuple(entry["name"]["elements"])))
            page_token = data.get("token")
            if not data.get("hasMore") or not page_token:
                break
        return keys

    def pointer(self, ref: str, key: TableKey) -> Pointer:
        cache_key = (ref, key.dotted)
        with self._pointer_lock:
            cached = self._pointers.get(cache_key)
        if cached is not None:
            return cached

        rel = f"trees/{urllib.parse.quote(ref, safe='')}/contents/{key.path_segment()}"
        status, data = self._get_json(rel)
        if status == 404:
            raise TableNotFound(ref, key.dotted)
        content = (data or {}).get("content") or {}
        snapshot_id = int(content.get("snapshotId", -1))
        location = str(content.get("metadataLocation", ""))
        log.info(
            "Pointer %s @ %s: snapshotId=%s metadata=%s",
            key.dotted,
            ref,
            snapshot_id,
            _filename(location),
        )
        if snapshot_id == -1 or not location:
            raise CorruptPointer(ref, key.dotted)
        ptr = Pointer(
            metadata_location=location,
            snapshot_id=snapshot_id,
            content_id=content.get("id"),
        )
        with self._pointer_lock:
            self._pointers[cache_key] = ptr
        return ptr

    # ----- commit log ------------------------------------------------------
    @property
    def history_operations_supported(self) -> bool | None:
        """None = not known yet; False = the server doesn't return commit operations."""
        return self._history_operations

    def history(
        self,
        ref: str,
        *,
        page_token: str | None = None,
        max_records: int = HISTORY_PAGE_SIZE,
        with_operations: bool = True,
    ) -> HistoryPage:
        """One page of `ref`'s commit log, newest first (`GET /trees/<ref>/history`).

        With `with_operations`, asks for `fetch=ALL` so each commit carries the keys it
        changed. If the server rejects that (4xx) or returns no operations, the log is
        fetched message-only (`touched=None`) — and this client remembers it, so later
        pages don't retry. Only counts are logged, never commit messages.
        """
        rel = f"trees/{urllib.parse.quote(ref, safe='')}/history"
        size = max(1, min(int(max_records), MAX_HISTORY_PAGE_SIZE))
        query: dict[str, Any] = {"max-records": size}
        if page_token:
            query["page-token"] = page_token
        want_ops = with_operations and self._history_operations is not False
        data: Any = None
        if want_ops:
            try:
                status, data = self._get_json(rel, {**query, "fetch": "ALL"})
            except NessieHTTPError as exc:
                if not 400 <= exc.status < 500:
                    raise
                log.info(
                    "History of %s: fetch=ALL rejected (HTTP %s); using message-only log",
                    ref,
                    exc.status,
                )
                self._history_operations = False
                want_ops = False
            else:
                if status != 200:
                    raise NessieHTTPError(rel, status)
        if not want_ops:
            status, data = self._get_json(rel, query)
            if status != 200:
                raise NessieHTTPError(rel, status)
        data = data or {}
        raw = [e for e in data.get("logEntries") or [] if isinstance(e, dict)]
        if want_ops and raw and all(e.get("operations") is None for e in raw):
            if self._history_operations is None:
                log.info("History of %s: server returned no operations; message-only", ref)
            self._history_operations = False
            want_ops = False
        elif want_ops and raw:
            self._history_operations = True
        entries = [parse_log_entry(e, want_ops) for e in raw]
        token = data.get("token")
        next_token = str(token) if data.get("hasMore") and token else None
        log.info(
            "History of %s: %d commit(s)%s%s",
            ref,
            len(entries),
            " with operations" if want_ops else "",
            ", more available" if next_token else "",
        )
        return HistoryPage(entries, next_token, want_ops)

    def clear_pointers(self, ref: str | None = None) -> None:
        with self._pointer_lock:
            if ref is None:
                self._pointers.clear()
            else:
                for k in [k for k in self._pointers if k[0] == ref]:
                    del self._pointers[k]


def _reason(reason: object) -> str:
    if isinstance(reason, BaseException):
        return type(reason).__name__ + (f": {reason}" if str(reason) else "")
    return str(reason)
