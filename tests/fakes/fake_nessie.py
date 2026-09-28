"""In-process fake Nessie (REST v2) + OAuth2 token endpoint for tests (SPEC §13.1).

Everything is synthetic. Tests script scenarios by mutating `FakeNessie.state`
(through the helper methods on `FakeNessie`) while the server is running.

Usage::

    with FakeNessie() as fake:
        fake.set_branch("eg-test1", "a" * 16)
        fake.add_table("eg-test1", ("silver", "ns", "t1"), metadata_location=...)
        profile = fake.profile()
"""

from __future__ import annotations

import itertools
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from floe.core.profiles import Profile

API_PREFIX = "/api/v2"
TOKEN_PATH = "/oauth2/token"
KEY_ELEMENT_SEP = "\u001d"

DEFAULT_CLIENT_ID = "fake-client-id"
DEFAULT_CLIENT_SECRET = "fake-client-secret-value"
DEFAULT_SCOPE = "api://fake-nessie/.default"


@dataclass(frozen=True)
class RequestRecord:
    method: str
    path: str
    query: str
    has_auth: bool
    status: int


@dataclass
class _Failure:
    remaining: int
    status: int
    path_prefix: str | None


@dataclass
class FakeNessieState:
    client_id: str = DEFAULT_CLIENT_ID
    client_secret: str = DEFAULT_CLIENT_SECRET
    scope: str = DEFAULT_SCOPE
    auth_required: bool = True
    token_expires_in: int = 3600
    token_failure_status: int | None = None
    page_size: int | None = None
    # ref name -> commit hash
    branches: dict[str, str] = field(default_factory=dict)
    # ref name -> key elements -> content dict
    tables: dict[str, dict[tuple[str, ...], dict[str, Any]]] = field(default_factory=dict)
    # ref name -> namespace key elements
    namespaces: dict[str, set[tuple[str, ...]]] = field(default_factory=dict)
    # token -> expiry (time.monotonic)
    tokens: dict[str, float] = field(default_factory=dict)
    # ref name -> commit log, oldest first (see `FakeNessie.commit`)
    commits: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # history knobs: reject `fetch=ALL` with HTTP 400 / ignore it (no operations)
    reject_fetch_all: bool = False
    omit_operations: bool = False
    history_page_size: int | None = None
    failures: list[_Failure] = field(default_factory=list)
    requests: list[RequestRecord] = field(default_factory=list)
    token_requests: int = 0


def decode_key(path_key: str) -> tuple[str, ...]:
    """Decode a Nessie v2 path key: split on '.', then \\u001D -> '.' in each element."""
    raw = unquote(path_key)
    return tuple(part.replace(KEY_ELEMENT_SEP, ".") for part in raw.split("."))


def _nessie_error(status: int, reason: str, message: str, code: str) -> dict[str, Any]:
    return {"status": status, "reason": reason, "message": message, "errorCode": code}


class FakeNessie:
    """Fake Nessie server; a context manager that starts/stops the HTTP server."""

    def __init__(self, state: FakeNessieState | None = None) -> None:
        self.state = state or FakeNessieState()
        self.lock = threading.RLock()
        self._content_ids = itertools.count(1)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ----- lifecycle -------------------------------------------------------
    def start(self) -> FakeNessie:
        fake = self

        class Handler(_Handler):
            owner = fake

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> FakeNessie:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None, "server not started"
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def nessie_uri(self) -> str:
        return self.base_url + API_PREFIX

    @property
    def token_endpoint(self) -> str:
        return self.base_url + TOKEN_PATH

    def profile(self, **overrides: Any) -> Profile:
        """A synthetic profile pointed at this fake."""
        values: dict[str, Any] = {
            "name": "fake",
            "nessie_uri": self.nessie_uri,
            "nessie_auth": "oauth2",
            "nessie_token_endpoint": self.token_endpoint,
            "nessie_client_id": self.state.client_id,
            "nessie_scope": self.state.scope,
            "adls_account": "acct",
        }
        values.update(overrides)
        return Profile(**values)

    # ----- scenario knobs --------------------------------------------------
    def set_branch(self, name: str, commit_hash: str | None = None) -> str:
        """Create a branch or move its head. Returns the new hash.

        Moving an existing branch's head also records a (synthetic) commit in its log."""
        with self.lock:
            new_hash = commit_hash or secrets.token_hex(16)
            if name in self.state.branches and self.state.branches[name] != new_hash:
                self._record_commit(name, new_hash, "Move head", "fake-bot", (), ())
            self.state.branches[name] = new_hash
            self.state.tables.setdefault(name, {})
            self.state.namespaces.setdefault(name, set())
            return new_hash

    move_head = set_branch

    def commit(
        self,
        ref: str,
        message: str,
        *,
        author: str = "fake-author",
        puts: tuple[tuple[str, ...] | str, ...] = (),
        deletes: tuple[tuple[str, ...] | str, ...] = (),
        commit_time: float | None = None,
        move_head: bool = True,
    ) -> str:
        """Record a commit on `ref` (and move its head to it unless `move_head=False`)."""
        with self.lock:
            if ref not in self.state.branches:
                self.set_branch(ref)
            new_hash = secrets.token_hex(16)
            self._record_commit(ref, new_hash, message, author, puts, deletes, commit_time)
            if move_head:
                self.state.branches[ref] = new_hash
            return new_hash

    def commits(self, ref: str) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.state.commits.get(ref, []))

    def _record_commit(
        self,
        ref: str,
        commit_hash: str,
        message: str,
        author: str,
        puts: tuple[tuple[str, ...] | str, ...],
        deletes: tuple[tuple[str, ...] | str, ...],
        commit_time: float | None = None,
    ) -> None:
        def elements(k: tuple[str, ...] | str) -> list[str]:
            return list(k.split(".")) if isinstance(k, str) else list(k)

        log = self.state.commits.setdefault(ref, [])
        when = time.time() if commit_time is None else commit_time
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when))
        stamp += f".{int((when % 1) * 1_000_000):06d}123Z"  # nanosecond precision, like Java
        operations = [
            {
                "type": "PUT",
                "key": {"elements": elements(k)},
                "content": dict(
                    self.state.tables.get(ref, {}).get(tuple(elements(k)))
                    or {"type": "ICEBERG_TABLE"}
                ),
            }
            for k in puts
        ] + [{"type": "DELETE", "key": {"elements": elements(k)}} for k in deletes]
        log.append(
            {
                "commitMeta": {
                    "hash": commit_hash,
                    "committer": "fake-committer",
                    "authors": [author],
                    "allSignedOffBy": [],
                    "message": message,
                    "commitTime": stamp,
                    "authorTime": stamp,
                    "properties": {},
                    "parentCommitHashes": [log[-1]["commitMeta"]["hash"]] if log else [],
                },
                "parentCommitHash": log[-1]["commitMeta"]["hash"] if log else "0" * 32,
                "operations": operations,
            }
        )

    def remove_branch(self, name: str) -> None:
        with self.lock:
            self.state.branches.pop(name, None)
            self.state.tables.pop(name, None)
            self.state.namespaces.pop(name, None)

    def add_namespace(self, ref: str, elements: tuple[str, ...]) -> None:
        with self.lock:
            self.state.namespaces.setdefault(ref, set()).add(tuple(elements))

    def add_table(
        self,
        ref: str,
        elements: tuple[str, ...] | str,
        metadata_location: str | None = None,
        snapshot_id: int = 1,
        content_id: str | None = None,
        *,
        commit_message: str | None = None,
        commit_time: float | None = None,
    ) -> dict[str, Any]:
        """Put a table on `ref`. Also records a commit touching it in the ref's log
        (without moving the head, so existing head hashes stay put)."""
        if isinstance(elements, str):
            elements = tuple(elements.split("."))
        elements = tuple(elements)
        if metadata_location is None:
            metadata_location = (
                "abfss://ref-a@acct.dfs.core.windows.net/"
                + "/".join(elements)
                + "/metadata/00001-abc.metadata.json"
            )
        content = {
            "type": "ICEBERG_TABLE",
            "id": content_id or f"cid-{next(self._content_ids)}",
            "metadataLocation": metadata_location,
            "snapshotId": snapshot_id,
            "schemaId": 0,
            "specId": 0,
            "sortOrderId": 0,
        }
        with self.lock:
            if ref not in self.state.branches:
                self.set_branch(ref)
            self.state.tables.setdefault(ref, {})[elements] = content
            self._record_commit(
                ref,
                secrets.token_hex(16),
                commit_message or f"Update table {'.'.join(elements)}",
                "fake-author",
                (elements,),
                (),
                commit_time,
            )
        return content

    def remove_table(self, ref: str, elements: tuple[str, ...] | str) -> None:
        if isinstance(elements, str):
            elements = tuple(elements.split("."))
        with self.lock:
            self.state.tables.get(ref, {}).pop(tuple(elements), None)

    def fail_next(self, n: int, status: int = 503, path_prefix: str | None = None) -> None:
        """Make the next `n` matching requests fail with `status`.

        `path_prefix` is matched against the request path (e.g. "/api/v2/trees/main").
        """
        with self.lock:
            self.state.failures.append(_Failure(n, status, path_prefix))

    def expire_tokens(self) -> None:
        """Mark every issued token as expired (server-side)."""
        with self.lock:
            for tok in self.state.tokens:
                self.state.tokens[tok] = 0.0

    def revoke_tokens(self) -> None:
        with self.lock:
            self.state.tokens.clear()

    def issued_tokens(self) -> list[str]:
        with self.lock:
            return list(self.state.tokens)

    def requests(
        self, path_prefix: str | None = None, method: str | None = None
    ) -> list[RequestRecord]:
        with self.lock:
            return [
                r
                for r in self.state.requests
                if (path_prefix is None or r.path.startswith(path_prefix))
                and (method is None or r.method == method)
            ]

    def api_requests(self) -> list[RequestRecord]:
        return self.requests(API_PREFIX)

    def reset_log(self) -> None:
        with self.lock:
            self.state.requests.clear()
            self.state.token_requests = 0

    # ----- internals used by the handler ------------------------------------
    def _take_failure(self, path: str) -> int | None:
        with self.lock:
            for failure in self.state.failures:
                if failure.remaining > 0 and (
                    failure.path_prefix is None or path.startswith(failure.path_prefix)
                ):
                    failure.remaining -= 1
                    status = failure.status
                    self.state.failures = [f for f in self.state.failures if f.remaining > 0]
                    return status
        return None

    def _issue_token(self) -> str:
        token = "fake-tok-" + secrets.token_urlsafe(24)
        with self.lock:
            self.state.tokens[token] = time.monotonic() + self.state.token_expires_in
        return token

    def _token_valid(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        token = header[len("Bearer ") :]
        with self.lock:
            expiry = self.state.tokens.get(token)
        return expiry is not None and expiry > time.monotonic()


class _Handler(BaseHTTPRequestHandler):
    owner: FakeNessie
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    # ----- helpers ---------------------------------------------------------
    def _send_json(self, status: int, body: Any) -> None:
        # Record before responding so the log is complete when the client returns.
        parts = urlsplit(self.path)
        self._record(parts.path, parts.query, status)
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, path: str, query: str, status: int) -> None:
        with self.owner.lock:
            self.owner.state.requests.append(
                RequestRecord(
                    method=self.command,
                    path=path,
                    query=query,
                    has_auth=self.headers.get("Authorization") is not None,
                    status=status,
                )
            )

    # ----- verbs -----------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        if parts.path != TOKEN_PATH:
            self._send_json(405, _nessie_error(405, "Method Not Allowed", "read-only", "BAD"))
            return
        failure = self.owner._take_failure(parts.path)
        if failure is not None:
            self._send_json(failure, {"error": "server_error"})
            return
        self._handle_token(body)

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        self._handle_get(parts.path, parse_qs(parts.query))

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_write()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_write()

    def _reject_write(self) -> None:
        self._send_json(405, _nessie_error(405, "Method Not Allowed", "read-only fake", "BAD"))

    # ----- token -----------------------------------------------------------
    def _handle_token(self, body: str) -> None:
        state = self.owner.state
        with self.owner.lock:
            state.token_requests += 1
            forced = state.token_failure_status
        if forced is not None:
            self._send_json(forced, {"error": "invalid_client"})
            return
        form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
        if form.get("grant_type") != "client_credentials":
            self._send_json(400, {"error": "unsupported_grant_type"})
            return
        if (
            form.get("client_id") != state.client_id
            or form.get("client_secret") != state.client_secret
        ):
            self._send_json(401, {"error": "invalid_client"})
            return
        if form.get("scope") != state.scope:
            self._send_json(400, {"error": "invalid_scope"})
            return
        token = self.owner._issue_token()
        self._send_json(
            200,
            {"token_type": "Bearer", "expires_in": state.token_expires_in, "access_token": token},
        )

    # ----- nessie ----------------------------------------------------------
    def _handle_get(self, path: str, query: dict[str, list[str]]) -> None:
        fake = self.owner
        if not path.startswith(API_PREFIX + "/"):
            self._send_json(404, _nessie_error(404, "Not Found", "no such endpoint", "UNKNOWN"))
            return
        failure = fake._take_failure(path)
        if failure is not None:
            self._send_json(
                failure, _nessie_error(failure, "Server Error", "injected failure", "UNKNOWN")
            )
            return
        if fake.state.auth_required and not fake._token_valid(self.headers.get("Authorization")):
            self._send_json(401, _nessie_error(401, "Unauthorized", "unauthorized", "UNKNOWN"))
            return

        segments = path[len(API_PREFIX) + 1 :].split("/")
        if segments == ["trees"]:
            with fake.lock:
                refs = [
                    {"type": "BRANCH", "name": n, "hash": h}
                    for n, h in sorted(fake.state.branches.items())
                ]
            self._send_json(200, {"references": refs})
            return
        if segments[0] != "trees" or len(segments) < 2:
            self._send_json(404, _nessie_error(404, "Not Found", "no such endpoint", "UNKNOWN"))
            return

        ref = unquote(segments[1]).split("@", 1)[0]
        with fake.lock:
            ref_hash = fake.state.branches.get(ref)
        if ref_hash is None:
            self._send_json(
                404,
                _nessie_error(404, "Not Found", f"Named reference '{ref}' not found",
                              "REFERENCE_NOT_FOUND"),
            )
            return
        reference = {"type": "BRANCH", "name": ref, "hash": ref_hash}

        if len(segments) == 2:
            self._send_json(200, {"reference": reference})
        elif len(segments) == 3 and segments[2] == "entries":
            self._handle_entries(ref, reference, query)
        elif len(segments) == 3 and segments[2] == "history":
            self._handle_history(ref, query)
        elif len(segments) == 4 and segments[2] == "contents":
            elements = decode_key(segments[3])
            with fake.lock:
                content = fake.state.tables.get(ref, {}).get(elements)
                content = dict(content) if content is not None else None
            if content is None:
                self._send_json(
                    404,
                    _nessie_error(404, "Not Found",
                                  f"Could not find content for key '{'.'.join(elements)}'",
                                  "CONTENT_NOT_FOUND"),
                )
                return
            self._send_json(200, {"content": content, "effectiveReference": reference})
        else:
            self._send_json(404, _nessie_error(404, "Not Found", "no such endpoint", "UNKNOWN"))

    def _handle_entries(
        self, ref: str, reference: dict[str, Any], query: dict[str, list[str]]
    ) -> None:
        fake = self.owner
        with fake.lock:
            entries: list[dict[str, Any]] = [
                {
                    "type": "NAMESPACE",
                    "name": {"elements": list(ns)},
                    "contentId": f"ns-{'.'.join(ns)}",
                    "content": None,
                }
                for ns in fake.state.namespaces.get(ref, set())
            ]
            entries += [
                {
                    "type": "ICEBERG_TABLE",
                    "name": {"elements": list(key)},
                    "contentId": content["id"],
                    "content": None,
                }
                for key, content in fake.state.tables.get(ref, {}).items()
            ]
            page_size = fake.state.page_size
        entries.sort(key=lambda e: e["name"]["elements"])

        if "max-records" in query:
            requested = int(query["max-records"][0])
            page_size = requested if page_size is None else min(page_size, requested)
        start = int(query["page-token"][0]) if "page-token" in query else 0
        end = len(entries) if page_size is None else start + page_size
        page = entries[start:end]
        has_more = end < len(entries)
        self._send_json(
            200,
            {
                "token": str(end) if has_more else None,
                "entries": page,
                "effectiveReference": reference,
                "hasMore": has_more,
            },
        )

    def _handle_history(self, ref: str, query: dict[str, list[str]]) -> None:
        fake = self.owner
        fetch_all = query.get("fetch", [""])[0].upper() == "ALL"
        with fake.lock:
            if fetch_all and fake.state.reject_fetch_all:
                reject = True
            else:
                reject = False
                entries = [json.loads(json.dumps(e)) for e in fake.state.commits.get(ref, [])]
                omit = fake.state.omit_operations
                page_size = fake.state.history_page_size
        if reject:
            self._send_json(
                400, _nessie_error(400, "Bad Request", "fetch option not supported", "BAD_REQUEST")
            )
            return
        entries.reverse()  # newest first
        for entry in entries:
            if not fetch_all or omit:
                entry.pop("operations", None)
        if "max-records" in query:
            requested = int(query["max-records"][0])
            page_size = requested if page_size is None else min(page_size, requested)
        start = int(query["page-token"][0]) if "page-token" in query else 0
        end = len(entries) if page_size is None else start + page_size
        has_more = end < len(entries)
        self._send_json(
            200,
            {"logEntries": entries[start:end], "hasMore": has_more,
             "token": str(end) if has_more else None},
        )
