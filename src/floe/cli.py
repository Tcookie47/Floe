"""Entry point for the `floe` console script (SPEC §15.1, §15.3).

    floe                      same as `floe serve`
    floe serve [--port N] [--no-browser] [--host 127.0.0.1]
    floe --version

`floe serve` binds 127.0.0.1 only (a random free port unless `--port` is given),
generates a single-use launch token (valid for two minutes), prints
`http://127.0.0.1:<port>/?token=…` and opens it in the default browser. The browser is
pointed at a private (0600, in a 0700 temp dir) HTML file that redirects to the link, so
the token never appears on a process command line (like Jupyter); the file is deleted
once the token is used, when it expires, or at shutdown. The token is registered with
the redaction filter and uvicorn's access log is off, so it never reaches a log. The
session cookie stays valid for the server's lifetime; opening Floe again after the link
was used needs a restart of `floe serve`. The Qt desktop app remains available
separately as `floe-desktop` (the `desktop` extra).
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from floe import __commit__, __version__

LOCAL_HOSTS = ("127.0.0.1", "localhost")
BIND_ADDRESS = "127.0.0.1"
TOKEN_TTL_SECONDS = 120.0

_LAUNCH_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0;url={url}">
<title>Opening Floe</title></head>
<body><p>Opening Floe… If nothing happens, <a href="{url}">click here</a>.</p></body></html>
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="floe", description="Browse and query Iceberg tables in your browser."
    )
    parser.add_argument(
        "--version", action="version", version=f"floe {__version__} ({__commit__})"
    )
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="Start the local web app (the default).")
    serve.add_argument("--port", type=int, default=0, help="Port (default: a free one).")
    serve.add_argument("--host", default=BIND_ADDRESS, help="Must be 127.0.0.1 or localhost.")
    serve.add_argument(
        "--no-browser", action="store_true", help="Don't open the browser; just print the URL."
    )
    return parser


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((BIND_ADDRESS, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


class LaunchFile:
    """A private HTML file that redirects the browser to the tokenized URL, so the
    token isn't on the browser's command line (visible to every user via `ps`)."""

    def __init__(self, url: str) -> None:
        self._lock = threading.Lock()
        self.dir: Path | None = Path(tempfile.mkdtemp(prefix="floe-launch-"))  # 0700
        os.chmod(self.dir, 0o700)
        self.path = self.dir / "open-floe.html"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(self.path, flags, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(_LAUNCH_PAGE.format(url=html.escape(url, quote=True)).encode("utf-8"))

    @property
    def uri(self) -> str:
        return self.path.as_uri()

    def remove(self) -> None:
        with self._lock:
            if self.dir is None:
                return
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None


def _configure_uvicorn_logging() -> None:
    """uvicorn's own loggers: warnings only, redacted, to stderr; access log off."""
    from floe.core.diagnostics import RedactionFilter

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactionFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = [handler] if name == "uvicorn" else []
        logger.setLevel(logging.WARNING)
        logger.propagate = name != "uvicorn"
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.disabled = True
    access.propagate = False


def serve(port: int = 0, host: str = BIND_ADDRESS, open_browser: bool = True) -> int:
    if host not in LOCAL_HOSTS:
        print(
            f"floe: refusing to listen on {host!r}. Floe serves health data and only runs on "
            "this machine: use --host 127.0.0.1 (the default) or localhost.",
            file=sys.stderr,
        )
        return 2
    if not 0 <= port <= 65535:
        print(f"floe: invalid port {port}", file=sys.stderr)
        return 2

    import uvicorn

    from floe.core import diagnostics
    from floe.web.app import create_app

    token = secrets.token_urlsafe(32)
    diagnostics.register_secret(token)
    log = diagnostics.setup_logging()
    try:
        sock = _bind(port)
    except OSError as exc:
        print(f"floe: can't listen on {BIND_ADDRESS}:{port} ({exc.strerror})", file=sys.stderr)
        return 1
    actual_port = sock.getsockname()[1]
    url = f"http://{BIND_ADDRESS}:{actual_port}/?token={token}"

    launch: LaunchFile | None = None
    if open_browser:
        try:
            launch = LaunchFile(url)
        except OSError:
            launch = None

    def token_used() -> None:
        if launch is not None:
            launch.remove()

    app = create_app(
        token=token, port=actual_port, token_ttl=TOKEN_TTL_SECONDS, on_token_used=token_used
    )
    _configure_uvicorn_logging()
    config = uvicorn.Config(
        app,
        log_config=None,
        log_level="warning",
        access_log=False,
        lifespan="on",
        server_header=False,
    )
    server = uvicorn.Server(config)
    log.info("Floe %s serving on %s:%d", __version__, BIND_ADDRESS, actual_port)
    print(f"Floe {__version__} is running. Open this link (keep it private):\n\n  {url}\n")
    print(
        "The link works once, within two minutes. To open Floe again later (e.g. after\n"
        "closing its tab), stop Floe with Ctrl+C and run `floe serve` again."
    )
    print("Press Ctrl+C to stop.", flush=True)
    expiry: threading.Timer | None = None
    if launch is not None:
        expiry = threading.Timer(TOKEN_TTL_SECONDS, launch.remove)
        expiry.daemon = True
        expiry.start()
        try:
            opened = webbrowser.open(launch.uri)
        except Exception:  # noqa: BLE001 - the printed link still works
            opened = False
        if not opened:
            print("(Couldn't open a browser; open the link above yourself.)")
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
        if expiry is not None:
            expiry.cancel()
        if launch is not None:
            launch.remove()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command in (None, "serve"):
        return serve(
            port=getattr(args, "port", 0),
            host=getattr(args, "host", BIND_ADDRESS),
            open_browser=not getattr(args, "no_browser", False),
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
