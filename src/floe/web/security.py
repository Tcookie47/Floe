"""Local server protection (SPEC §15.3), as a pure ASGI middleware.

Order of checks for every request:

1. `Host` must be `127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding defence) → 400.
2. A declared `Content-Length` over `MAX_BODY_BYTES` → 413.
3. `GET /?token=<t>`: the launch token is **single-use** and expires `token_ttl` seconds
   after launch. The first constant-time match sets the HttpOnly, SameSite=Strict session
   cookie and returns a tiny 200 "signed in" page whose `<meta http-equiv="refresh">`
   moves on to `/#k=<api key>` (stripping the token from the URL); any later, expired or
   wrong token → 401 "open Floe from the terminal link" page.

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
4. State-changing methods (anything but GET/HEAD/OPTIONS) need an `Origin` (or, failing
   that, a `Referer`) of `http://127.0.0.1:<port>` / `http://localhost:<port>` → else 403.
5. Every other request needs the session cookie → 401 JSON for `/api/...`, else a small
   "open Floe from the link printed in the terminal" page.
6. `/api/...` additionally needs the per-launch API key in the `X-Floe-Auth` header → 401.
   Browsers don't isolate cookies by port, so any other server on 127.0.0.1 the browser
   talks to receives the session cookie; the API key is what such a server can't obtain.
   It reaches the page only in the sign-in page's refresh URL fragment (never sent over the network
   again); `app.js` moves it to `sessionStorage`, which *is* isolated per origin
   including the port. (A "fetch the key" endpoint guarded by cookie + `Sec-Fetch-Site`
   was rejected: a non-browser client holding a leaked cookie can forge those headers.)
7. Request bodies are buffered up to `MAX_BODY_BYTES` (also for chunked uploads) → 413.

Every response gets a strict CSP, `nosniff`, `no-referrer` and `Cache-Control: no-store`.
Neither the token, the API key nor the cookie value is ever logged.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import secrets
import time
import urllib.parse
from collections.abc import Callable
from http.cookies import SimpleCookie
from typing import Any

log = logging.getLogger("floe.web.security")

COOKIE_NAME = "floe_session"
CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
API_KEY_HEADER = "x-floe-auth"
MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_TOKEN_TTL = 120.0

UNAUTHORIZED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Floe</title></head>
<body><h1>Floe</h1>
<p>Open Floe from the link printed in the terminal where you ran <code>floe serve</code>.</p>
<p>That link works once, within two minutes of starting Floe. If it was already used
(or has expired), stop Floe with Ctrl+C and run <code>floe serve</code> again.</p>
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


class SecurityMiddleware:
    """See the module docstring. `token` is the per-launch launch token from `floe serve`;
    `api_key` the per-launch API key (random if not given); `on_token_used` is called once
    when the launch token has been exchanged (e.g. to delete the browser launch file)."""

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
    ) -> None:
        if not token:
            raise ValueError("a non-empty token is required")
        self.app = app
        self._token = token.encode()
        self._token_used = False
        self._token_deadline = time.monotonic() + token_ttl
        self._on_token_used = on_token_used
        self._cookie_value = secrets.token_urlsafe(32)
        self._api_key = api_key or secrets.token_urlsafe(32)
        self._max_body = max_body
        self._hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self._origins = {f"http://{h}" for h in self._hosts}

    # ----- helpers -------------------------------------------------------------
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
        if path.startswith("/api/") or path == "/api":
            type_name = {
                400: "BadHost", 401: "Unauthorized", 413: "RequestTooLarge"
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

        query = urllib.parse.parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if path == "/" and method == "GET" and "token" in query:
            given = query["token"][0].encode()
            if not hmac.compare_digest(given, self._token):
                log.warning("Rejected a launch link with a wrong token")
                await self._reject(send, 401, path, "Invalid token.")
                return
            if self._token_used or time.monotonic() > self._token_deadline:
                log.warning("Rejected a launch link that was already used or has expired")
                await self._reject(send, 401, path, "The launch link was already used.")
                return
            # Single use: no await between the check above and this assignment.
            self._token_used = True
            if self._on_token_used is not None:
                try:
                    self._on_token_used()
                except Exception:  # noqa: BLE001 - cleanup must not block sign-in
                    log.warning("Launch-link cleanup failed")
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
            log.warning("Rejected a %s request with a missing or foreign Origin", method)
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
