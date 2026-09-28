"""`/_control/...`: the local control channel used by `floe show` and `floe stop`
(SPEC §15.1, §15.3). Authentication (the `X-Floe-Control` key, no browser headers, Host)
is done by `SecurityMiddleware` before a request gets here. The one exception is
`GET /_control/health?nonce=…` *without* a key: the answer then carries
`HMAC(control_key, nonce|pid|port)`, which lets the CLI check that this server knows the
key from the state file before it sends the key itself. Only the route and its outcome
are logged, never a key or a token.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request

from floe import __version__
from floe.instance import NONCE_RE, health_proof, remove_launch_dir
from floe.web.security import LaunchTokens

log = logging.getLogger("floe.web.control")

router = APIRouter(prefix="/_control")


@dataclass
class Control:
    tokens: LaunchTokens
    port: int
    control_key: str
    on_shutdown: Callable[[], None] | None = None

    def link(self, token: str) -> str:
        return f"http://127.0.0.1:{self.port}/?token={token}"


def _control(request: Request) -> Control:
    return request.app.state.control


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    control = _control(request)
    pid = os.getpid()
    out: dict[str, Any] = {"ok": True, "version": __version__, "pid": pid}
    nonce = request.query_params.get("nonce")
    if nonce is not None and NONCE_RE.fullmatch(nonce):
        out["port"] = control.port
        out["proof"] = health_proof(control.control_key, nonce, pid, control.port)
    return out


@router.post("/login-link")
async def login_link(request: Request) -> dict[str, Any]:
    control = _control(request)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    launch_dir = body.get("launch_dir") if isinstance(body, dict) else None
    if isinstance(launch_dir, str) and launch_dir:
        # The CLI writes the launch page itself (from the URL we return) into a private
        # directory it created; we delete it once the token is used, expires or is
        # dropped (remove_launch_dir only touches a `floe-launch-*` temp dir).
        def remove_dir() -> None:
            remove_launch_dir(launch_dir)

        token = control.tokens.mint(on_done=remove_dir)
        timer = threading.Timer(control.tokens.ttl, remove_dir)
        timer.daemon = True
        timer.start()
    else:
        launch_dir = None
        token = control.tokens.mint()
    log.info("Control login-link: minted a launch link%s",
             " with a launch file" if launch_dir else "")
    return {"url": control.link(token), "expires_in": control.tokens.ttl}


@router.post("/shutdown")
def shutdown(request: Request) -> dict[str, Any]:
    control = _control(request)
    log.info("Control shutdown: stopping")
    if control.on_shutdown is not None:
        control.on_shutdown()
    return {"ok": True}
