"""Local server protection (SPEC §15.3), as a pure ASGI middleware.

Order of checks for every request:

1. `Host` must be `127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding defence) → 400.
2. A declared `Content-Length` over `MAX_BODY_BYTES` → 413.
3. `/_control/...` (used by `floe show` / `floe stop`, never by a browser): any request
   carrying `Origin` or `Sec-Fetch-*` headers (i.e. browser-originated) → 403; a missing
   or wrong `X-Floe-Control` key (constant-time compare against the per-process control
   key from the state file) → 401. Exception: `GET /_control/health?nonce=<urlsafe>`
   without any key is let through; the route then answers with an HMAC proof of the
   key (never the key), so the CLI can authenticate the server first. These routes skip
   the cookie / API-key checks below, but nothing above. Without a control key the
   routes don't exist (404). Request paths are never logged verbatim (a fixed route
   label is logged instead), so a crafted path can't inject log lines.
4. `GET /?token=<t>`: launch tokens are **single-use** and expire `token_ttl` seconds
   after they were minted (the startup token, plus any minted later by
   `POST /_control/login-link`; at most `MAX_OUTSTANDING_TOKENS` are outstanding, the
   oldest is dropped first). A constant-time match sets the HttpOnly, SameSite=Strict session
   cookie and returns a tiny 200 "signed in" page whose `<meta http-equiv="refresh">`
   moves on to `/#k=<api key>` (stripping the token from the URL); any later, expired or
   wrong token → 401 "open Floe from the terminal link" page. The cookie and the API key
   are the same for every link the process issues, so tabs opened earlier keep working.

   Why not a 303 straight to `/`: `floe serve` opens a private `file://` launch page that
   meta-refreshes to the token URL, so that navigation chain is *cross-site*. Chromium
   (Brave, Chrome, Edge) keeps treating every redirect in a cross-site-initiated chain as
   cross-site, so the SameSite=Strict cookie set on the token response was **not** sent
   on the redirected `GET /` and the user saw the "open Floe…" page. The refresh on the
   interstitial is a new navigation initiated by a document of our own origin, which is
   same-site in Chromium, Firefox and Safari alike, so the Strict cookie is sent. This
   keeps SameSite=Strict (rather than Lax, which would also work but lets any site's
   top-level GET navigation carry the cookie) and needs no script, so the CSP stays
   `default-src 'self'` with no inline scripts. The page is sent with `no-store` and
   `no-referrer` (as every response is), so the key in it isn't cached or leaked.
5. State-changing methods (anything but GET/HEAD/OPTIONS) need an `Origin` (or, failing
   that, a `Referer`) of `http://127.0.0.1:<port>` / `http://localhost:<port>` → else 403.
6. Every other request needs the session cookie → 401 JSON for `/api/...`, else a small
   "open Floe from the link printed in the terminal" page.
7. `/api/...` additionally needs the per-launch API key in the `X-Floe-Auth` header → 401.
   Browsers don't isolate cookies by port, so any other server on 127.0.0.1 the browser
   talks to receives the session cookie; the API key is what such a server can't obtain.
   It reaches the page only in the sign-in page's refresh URL fragment (never sent over the network
   again); `app.js` moves it to `sessionStorage`, which *is* isolated per origin
   including the port. (A "fetch the key" endpoint guarded by cookie + `Sec-Fetch-Site`
   was rejected: a non-browser client holding a leaked cookie can forge those headers.)
8. Request bodies are buffered up to `MAX_BODY_BYTES` (also for chunked uploads) → 413.

Every response gets a strict CSP, `nosniff`, `no-referrer` and `Cache-Control: no-store`.
Neither a token, the API key, the control key nor the cookie value is ever logged.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from http.cookies import SimpleCookie
from typing import Any

from floe.core import diagnostics
from floe.instance import NONCE_RE  # keyless health-check nonce (instance.verify)

log = logging.getLogger("floe.web.security")

COOKIE_NAME = "floe_session"
CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
API_KEY_HEADER = "x-floe-auth"
CONTROL_HEADER = "x-floe-control"
CONTROL_PREFIX = "/_control/"
# Headers only browsers send: their presence marks a request as browser-originated.
BROWSER_HEADERS = ("origin", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest",
                   "sec-fetch-user")
CONTROL_ROUTES = ("health", "login-link", "shutdown")
MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_TOKEN_TTL = 120.0
MAX_OUTSTANDING_TOKENS = 5

UNAUTHORIZED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Floe</title></head>
<body><h1>Floe</h1>
<p>Open Floe from the link printed in the terminal where you ran <code>floe serve</code>.</p>
<p>That link works once, within two minutes. If it was already used (or has expired),
run <code>floe show --open</code> (or <code>floe show</code> and paste the new link) in a
terminal while Floe is running.</p>
<p>If the address bar shows <code>#k=</code> after the address, the link was accepted but
your browser didn't send Floe's sign-in cookie. Click the address bar and press Enter to
retry. If that doesn't help, restart with <code>floe serve --no-browser</code> and paste
the printed link into the address bar yourself.</p>
</body></html>
"""

# Sent once, after a successful token exchange (see the module docstring, item 3).
# No script: the same-origin meta refresh carries the SameSite=Strict cookie.
SIGNED_IN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0;url={target}">
<title>Opening Floe</title></head>
<body><p>Signed in. Opening Floe&hellip; If nothing happens,
<a href="{target}">continue to Floe</a>.</p></body></html>
"""


def _security_headers() -> list[tuple[bytes, bytes]]:
    return [
        (b"content-security-policy", CSP.encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"DENY"),
        (b"cache-control", b"no-store"),
    ]


def _control_route(path: str) -> str:
    """A fixed label for a control route (request paths are never logged verbatim)."""
    return {f"{CONTROL_PREFIX}{name}": name for name in CONTROL_ROUTES}.get(path, "(other)")


class LaunchTokens:
    """The outstanding single-use launch tokens of this process. Each expires `ttl`
    seconds after it was minted; at most `max_outstanding` exist (the oldest is dropped).
    `on_done` callbacks (e.g. deleting a launch file) run when a token is used, expires
    or is dropped. Thread-safe: tokens are minted from worker threads and redeemed on
    the event loop."""

    def __init__(
        self,
        ttl: float = DEFAULT_TOKEN_TTL,
        max_outstanding: int = MAX_OUTSTANDING_TOKENS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_outstanding = max_outstanding
        self._clock = clock
        self._lock = threading.Lock()
        # token -> (deadline, on_done); insertion order is minting order.
        self._tokens: dict[str, tuple[float, Callable[[], None] | None]] = {}

    @staticmethod
    def _done(callbacks: list[Callable[[], None] | None]) -> None:
        for callback in callbacks:
            if callback is None:
                continue
            try:
                callback()
            except Exception:  # noqa: BLE001 - cleanup must not break sign-in
                log.warning("Launch-link cleanup failed")

    def _prune_locked(self) -> list[Callable[[], None] | None]:
        now = self._clock()
        expired = [t for t, (deadline, _) in self._tokens.items() if now > deadline]
        return [self._tokens.pop(t)[1] for t in expired]

    def add(self, token: str, on_done: Callable[[], None] | None = None) -> None:
        if not token:
            raise ValueError("a non-empty token is required")
        diagnostics.register_secret(token)
        with self._lock:
            done = self._prune_locked()
            while len(self._tokens) >= self.max_outstanding:
                oldest = next(iter(self._tokens))
                done.append(self._tokens.pop(oldest)[1])
            self._tokens[token] = (self._clock() + self.ttl, on_done)
        self._done(done)

    def mint(self, on_done: Callable[[], None] | None = None) -> str:
        token = secrets.token_urlsafe(32)
        self.add(token, on_done)
        return token

    def redeem(self, given: str) -> bool:
        """True (once) if `given` is an outstanding, unexpired token; it is then spent."""
        given_bytes = given.encode("utf-8", "replace")
        with self._lock:
            done = self._prune_locked()
            match = None
            for token in list(self._tokens):  # compare against all: no early exit
                if hmac.compare_digest(given_bytes, token.encode()):
                    match = token
            if match is not None:
                done.append(self._tokens.pop(match)[1])
        self._done(done)
        return match is not None

    def outstanding(self) -> int:
        with self._lock:
            done = self._prune_locked()
            count = len(self._tokens)
        self._done(done)
        return count

    def clear(self) -> None:
        with self._lock:
            done = [on_done for _, on_done in self._tokens.values()]
            self._tokens.clear()
        self._done(done)


class SecurityMiddleware:
    """See the module docstring. `token` is the startup launch token from `floe serve`
    (added to `tokens`, a `LaunchTokens`, created if not given); `api_key` the per-launch
    API key (random if not given); `on_token_used` is called once when the startup token
    has been used or has expired (e.g. to delete the browser launch file);
    `control_key` enables the `/_control/...` routes."""

    def __init__(
        self,
        app: Any,
        *,
        token: str,
        port: int,
        api_key: str | None = None,
        token_ttl: float = DEFAULT_TOKEN_TTL,
        on_token_used: Callable[[], None] | None = None,
        max_body: int = MAX_BODY_BYTES,
        tokens: LaunchTokens | None = None,
        control_key: str | None = None,
    ) -> None:
        if not token:
            raise ValueError("a non-empty token is required")
        self.app = app
        self.tokens = tokens if tokens is not None else LaunchTokens(token_ttl)
        self.tokens.add(token, on_token_used)
        self._control_key = control_key.encode() if control_key else None
        self._cookie_value = secrets.token_urlsafe(32)
        self._api_key = api_key or secrets.token_urlsafe(32)
        self._max_body = max_body
        self._hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self._origins = {f"http://{h}" for h in self._hosts}

    # ----- helpers -------------------------------------------------------------
    @staticmethod
    def _is_health_challenge(path: str, method: str, scope: dict[str, Any]) -> bool:
        if path != f"{CONTROL_PREFIX}health" or method != "GET":
            return False
        query = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin-1"))
        nonces = query.get("nonce", [])
        return len(nonces) == 1 and NONCE_RE.fullmatch(nonces[0]) is not None

    @staticmethod
    def _headers(scope: dict[str, Any]) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in scope.get("headers", []):
            name = key.decode("latin-1").lower()
            text = value.decode("latin-1")
            out[name] = f"{out[name]}; {text}" if name == "cookie" and name in out else text
        return out

    def _cookie_ok(self, headers: dict[str, str]) -> bool:
        raw = headers.get("cookie")
        if not raw:
            return False
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # noqa: BLE001 - a malformed cookie header is just "no cookie"
            return False
        morsel = jar.get(COOKIE_NAME)
        if morsel is None:
            return False
        return hmac.compare_digest(morsel.value.encode(), self._cookie_value.encode())

    def _api_key_ok(self, headers: dict[str, str]) -> bool:
        given = headers.get(API_KEY_HEADER)
        if not given:
            return False
        return hmac.compare_digest(given.encode("latin-1"), self._api_key.encode())

    def _origin_ok(self, headers: dict[str, str]) -> bool:
        origin = headers.get("origin")
        if origin is not None:
            return origin in self._origins
        referer = headers.get("referer")
        if not referer:
            return False
        parts = urllib.parse.urlsplit(referer)
        return f"{parts.scheme}://{parts.netloc}" in self._origins

    @staticmethod
    async def _send(
        send: Any,
        status: int,
        body: bytes,
        content_type: bytes,
        extra: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        headers = [
            (b"content-type", content_type),
            (b"content-length", str(len(body)).encode()),
            *_security_headers(),
            *(extra or []),
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _reject(self, send: Any, status: int, path: str, message: str) -> None:
        if path.startswith(("/api/", CONTROL_PREFIX)) or path == "/api":
            type_name = {
                400: "BadHost", 401: "Unauthorized", 404: "NotFound", 413: "RequestTooLarge"
            }.get(status, "Forbidden")
            body = json.dumps({"error": {"type": type_name, "message": message}}).encode()
            await self._send(send, status, body, b"application/json")
        elif status == 401:
            await self._send(send, status, UNAUTHORIZED_PAGE.encode(), b"text/html; charset=utf-8")
        else:
            await self._send(send, status, message.encode(), b"text/plain; charset=utf-8")

    async def _buffer_body(self, receive: Any) -> Any:
        """Read the whole request body (at most `max_body` bytes, however it is framed)
        and return a `receive` that replays it; None if the body is too large."""
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                first = message  # e.g. http.disconnect: pass it on
                break
            body = message.get("body", b"")
            size += len(body)
            if size > self._max_body:
                return None
            chunks.append(body)
            if not message.get("more_body", False):
                first = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
                break
        replayed = False

        async def replay() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return first
            return await receive()

        return replay

    # ----- ASGI ----------------------------------------------------------------
    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = self._headers(scope)
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()

        if headers.get("host", "").lower() not in self._hosts:
            log.warning("Rejected request with an unexpected Host header")
            await self._reject(send, 400, path, "Bad Host header.")
            return

        declared = headers.get("content-length")
        if declared is not None and declared.strip().isdigit() and int(declared) > self._max_body:
            await self._reject(send, 413, path, "Request body too large.")
            return

        if path.startswith(CONTROL_PREFIX):
            if self._control_key is None:
                await self._reject(send, 404, path, "Not found.")
                return
            route = _control_route(path)
            if any(name in headers for name in BROWSER_HEADERS):
                log.warning("Rejected a browser request to control route %s", route)
                await self._reject(send, 403, path, "Control requests from a browser are refused.")
                return
            if CONTROL_HEADER not in headers and self._is_health_challenge(path, method, scope):
                # Keyless health check with a nonce: the route answers with an HMAC proof
                # of the control key, so the CLI can check this server before it sends
                # the key (see floe.instance.verify).
                await self.app(scope, receive, send)
                return
            given_key = headers.get(CONTROL_HEADER, "").encode("latin-1")
            if not hmac.compare_digest(given_key, self._control_key):
                log.warning("Rejected a control request to %s with a missing or wrong key", route)
                await self._reject(send, 401, path, "Missing or wrong control key.")
                return
            receive = await self._buffer_body(receive)
            if receive is None:
                await self._reject(send, 413, path, "Request body too large.")
                return
            await self.app(scope, receive, send)
            return

        query = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if path == "/" and method == "GET" and "token" in query:
            # Single use: redeem() checks and spends the token atomically.
            if not self.tokens.redeem(query["token"][0]):
                log.warning("Rejected a launch link that is wrong, already used or expired")
                await self._reject(send, 401, path, "Invalid, used or expired launch link.")
                return
            cookie = (
                f"{COOKIE_NAME}={self._cookie_value}; HttpOnly; SameSite=Strict; Path=/"
            ).encode()
            target = html.escape(f"/#k={self._api_key}", quote=True)
            await self._send(
                send,
                200,
                SIGNED_IN_PAGE.format(target=target).encode(),
                b"text/html; charset=utf-8",
                [(b"set-cookie", cookie)],
            )
            return

        if method not in SAFE_METHODS and not self._origin_ok(headers):
            log.warning("Rejected a %r request with a missing or foreign Origin", method[:16])
            await self._reject(send, 403, path, "Cross-origin request refused.")
            return

        if not self._cookie_ok(headers):
            await self._reject(send, 401, path, "Not signed in: open Floe from the terminal link.")
            return

        if (path.startswith("/api/") or path == "/api") and not self._api_key_ok(headers):
            await self._reject(
                send, 401, path, "Missing or wrong API key: open Floe from the terminal link."
            )
            return

        receive = await self._buffer_body(receive)
        if receive is None:
            await self._reject(send, 413, path, "Request body too large.")
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in _security_headers() if k not in names]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, send_with_headers)
