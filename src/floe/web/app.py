"""FastAPI app factory for Floe's local web app (SPEC §15).

`create_app(token=..., port=...)` wires the security middleware, the JSON API under
`/api/...`, and the browser UI: the HTML page at `/` plus `/static` (W3). The injectables
(`store`, `session_factory`, `ask_opener`, `connection_tester`, `history`, `prefs`) exist
for tests; the defaults use the real per-user data directory and keyring.
"""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from floe import __version__
from floe.core import diagnostics
from floe.core.errors import (
    AdlsAuthError,
    AdlsTlsError,
    FloeError,
    KeychainError,
    NessieAuthError,
    NessieUnreachable,
)
from floe.core.history import HistoryStore
from floe.core.profiles import Profile, ProfileStore
from floe.web import api_ask, api_data, api_profiles, control
from floe.web.jobs import JobManager, TooManyJobs
from floe.web.prefs import PrefsStore
from floe.web.security import DEFAULT_TOKEN_TTL, LaunchTokens, SecurityMiddleware
from floe.web.serialize import error_payload
from floe.web.sessions import ProfileNotFound, SessionFactory, SessionManager
from floe.web.state import ApiError, ConnectionTester, WebState

log = logging.getLogger("floe.web")

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATES_DIR = WEB_DIR / "templates"


def _error_response(status: int, payload: dict[str, Any]) -> JSONResponse:
    payload = {**payload, "message": diagnostics.redact(str(payload.get("message", "")))}
    return JSONResponse({"error": payload}, status_code=status)


def _floe_status(exc: FloeError) -> int:
    if isinstance(exc, ProfileNotFound):
        return 404
    if isinstance(exc, NessieUnreachable | NessieAuthError | AdlsAuthError | AdlsTlsError):
        return 502
    if isinstance(exc, KeychainError):
        return 503
    return 400


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(
            exc.status, {"type": exc.type_name, "message": exc.message, **exc.extra}
        )

    @app.exception_handler(TooManyJobs)
    async def _too_many_jobs(_request: Request, exc: TooManyJobs) -> JSONResponse:
        return _error_response(429, {"type": "TooManyJobs", "message": str(exc)})

    @app.exception_handler(FloeError)
    async def _floe_error(_request: Request, exc: FloeError) -> JSONResponse:
        return _error_response(_floe_status(exc), error_payload(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo the submitted values back (they could include secrets).
        parts = []
        for err in exc.errors()[:5]:
            loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            parts.append(f"{loc or 'body'}: {err.get('msg', 'invalid')}")
        return _error_response(
            422, {"type": "ValidationError", "message": "Invalid request: " + "; ".join(parts)}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error_response(exc.status_code, {"type": "HTTPError", "message": str(exc.detail)})

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
        log.error("Unhandled %s in a request", type(exc).__name__)
        return _error_response(
            500, {"type": "InternalError", "message": f"Unexpected error ({type(exc).__name__})."}
        )


def create_app(
    *,
    token: str,
    port: int,
    store: ProfileStore | None = None,
    session_factory: SessionFactory | None = None,
    ask_opener: Any = None,
    connection_tester: ConnectionTester | None = None,
    history: HistoryStore | None = None,
    prefs: PrefsStore | None = None,
    max_workers: int = 4,
    keep_jobs_per_profile: int = 20,
    max_unfinished_jobs: int = 16,
    max_retained_rows: int = 2_000_000,
    api_key: str | None = None,
    token_ttl: float = DEFAULT_TOKEN_TTL,
    on_token_used: Callable[[], None] | None = None,
    control_key: str | None = None,
    on_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    """Build the app. `token` is the single-use startup launch token; `port` the port
    it's served on (used for the Host / Origin allow-lists); `api_key` the per-launch key
    required in the `X-Floe-Auth` header of every API request (random if not given, and
    available as `app.state.api_key`). `control_key` enables the `/_control/...` routes
    (`floe show` / `floe stop`); `/_control/shutdown` calls `on_shutdown`. The launch
    tokens are `app.state.launch_tokens`."""
    api_key = api_key or secrets.token_urlsafe(32)
    diagnostics.register_secret(api_key)
    diagnostics.register_secret(control_key)
    tokens = LaunchTokens(token_ttl)
    store = store if store is not None else ProfileStore()
    state = WebState(
        store=store,
        sessions=SessionManager(store, session_factory),
        jobs=JobManager(
            max_workers=max_workers,
            keep_per_profile=keep_jobs_per_profile,
            max_unfinished=max_unfinished_jobs,
            max_retained_rows=max_retained_rows,
        ),
        history=history if history is not None else HistoryStore(),
        prefs=prefs if prefs is not None else PrefsStore(),
        ask_opener=ask_opener,
    )
    if connection_tester is not None:
        state.connection_tester = connection_tester

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        tokens.clear()  # removes any launch files still waiting
        state.shutdown()

    app = FastAPI(
        title="Floe",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.floe = state
    _install_error_handlers(app)
    app.include_router(api_profiles.router)
    app.include_router(api_data.router)
    app.include_router(api_ask.router)
    if control_key:
        app.state.control = control.Control(
            tokens=tokens, port=port, control_key=control_key, on_shutdown=on_shutdown
        )
        app.include_router(control.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # Field defaults for the profile form's "New" (non-secret fields only).
    profile_defaults = json.dumps(Profile(name="").to_dict(), sort_keys=True)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"version": __version__, "profile_defaults": profile_defaults},
        )

    app.state.api_key = api_key
    app.state.launch_tokens = tokens
    app.add_middleware(
        SecurityMiddleware,
        token=token,
        port=port,
        api_key=api_key,
        token_ttl=token_ttl,
        on_token_used=on_token_used,
        tokens=tokens,
        control_key=control_key,
    )
    return app
