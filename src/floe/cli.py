"""Entry point for the `floe` console script (SPEC §15.1, §15.3).

    floe                      same as `floe serve`
    floe serve [--port N] [--no-browser] [--host 127.0.0.1] [--background]
    floe show [--open] [--no-browser]
    floe stop
    floe --version

`floe serve` binds 127.0.0.1 only (a random free port unless `--port` is given),
generates a single-use launch token (valid for two minutes), prints
`http://127.0.0.1:<port>/?token=…` and opens it in the default browser. The browser is
pointed at a private (0600, in a 0700 temp dir) HTML file that redirects to the link, so
the token never appears on a process command line (like Jupyter); the file is deleted
once the token is used, when it expires, or at shutdown. Tokens are registered with the
redaction filter and uvicorn's access log is off, so they never reach a log.

Only one instance runs per user: the server process holds an exclusive lock file
(`server.lock`) for its lifetime, taken before it binds, and writes a state file
(`floe.instance`, 0600) with its PID, port and a per-process *control key* for the
`/_control/...` endpoints. If a live instance exists (or holds the lock), `floe serve`
doesn't start another; it prints a fresh link as `floe show` does. `floe show` mints a
new single-use link from the running server (`--open` writes a private launch file for
it); `floe stop` shuts it down. Before the CLI sends the control key anywhere, the server
on the recorded port must prove it knows the key (an HMAC over a fresh nonce, its pid
and port); links it returns are accepted only in the exact expected form, and nothing
the server sends is printed unsanitised. `floe stop` never kills a process that hasn't
proved it is this Floe.

`floe serve --background` starts the server as a detached process (its own session on
POSIX; `DETACHED_PROCESS` on Windows) that survives closing the terminal, waits until
it answers, prints a link and exits. The child's stdout/stderr go to a 0600 file in the
logs directory; it never prints a token.

The Qt desktop app remains available separately as `floe-desktop` (the `desktop` extra).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from floe import __commit__, __version__, instance
from floe.instance import LaunchFile, ServerState

__all__ = ["LaunchFile", "main", "serve", "show", "stop"]

LOCAL_HOSTS = ("127.0.0.1", "localhost")
BIND_ADDRESS = "127.0.0.1"
TOKEN_TTL_SECONDS = 120.0
BACKGROUND_READY_TIMEOUT = 15.0
STOP_TIMEOUT = 10.0
# How long a background parent waits for its child to exit once another (verified)
# instance answers: the child exits with ALREADY_RUNNING when it can't take the lock.
CHILD_EXIT_GRACE = 3.0
ALREADY_RUNNING = 3
CHILD_OUTPUT_NAME = "server-output.log"
LINK_NOTE = "The link works once, within two minutes."
NOT_RUNNING = "Floe isn't running. Start it with `floe serve --background`."


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="floe", description="Browse and query Iceberg tables in your browser."
    )
    parser.add_argument(
        "--version", action="version", version=f"floe {__version__} ({__commit__})"
    )
    sub = parser.add_subparsers(dest="command")
    serve_p = sub.add_parser("serve", help="Start the local web app (the default).")
    serve_p.add_argument("--port", type=int, default=0, help="Port (default: a free one).")
    serve_p.add_argument("--host", default=BIND_ADDRESS, help="Must be 127.0.0.1 or localhost.")
    serve_p.add_argument(
        "--no-browser", action="store_true", help="Don't open the browser; just print the URL."
    )
    serve_p.add_argument(
        "--background", action="store_true",
        help="Run detached from this terminal; use `floe show` / `floe stop` later.",
    )
    serve_p.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    show_p = sub.add_parser("show", help="Print a fresh one-time link to the running Floe.")
    show_p.add_argument("--open", action="store_true", help="Also open it in the browser.")
    show_p.add_argument("--no-browser", action="store_true", help="Never open a browser.")
    sub.add_parser("stop", help="Stop the running Floe.")
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


def _open_in_browser(uri: str) -> bool:
    try:
        return bool(webbrowser.open(uri))
    except Exception:  # noqa: BLE001 - the printed link still works
        return False


# --------------------------------------------------------------------------- show
def _describe(state: ServerState) -> str:
    try:
        since = datetime.fromtimestamp(state.started_at).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        since = "an unknown time"
    return (
        f"Floe {instance.safe_text(state.version, 40)} is running (PID {state.pid}, "
        f"since {since}, port {state.port})."
    )


def _print_link(state: ServerState, open_browser: bool) -> int:
    """Mint a fresh one-time link from the (verified) running server, print it and
    (optionally) open it via a private launch file written here from the validated URL,
    so the token is never on a command line. The server deletes the launch directory
    once the token is used or expires."""
    from floe.core import diagnostics

    diagnostics.register_secret(state.control_key)
    launch: LaunchFile | None = None
    if open_browser:
        try:
            launch = LaunchFile()  # the private 0700 directory; the page is written below
        except OSError:
            launch = None
    try:
        url = instance.login_link(
            state, launch_dir=str(launch.dir) if launch is not None else None
        )
    except instance.ControlError as exc:
        if launch is not None:
            launch.remove()
        print(f"floe: couldn't get a link from the running Floe "
              f"({instance.safe_text(exc)}).", file=sys.stderr)
        return 1
    diagnostics.register_secret(url.rsplit("=", 1)[-1])
    print(f"Open this link (keep it private):\n\n  {url}\n")
    print(f"{LINK_NOTE} Run `floe show` again for a new one.", flush=True)
    if open_browser:
        opened = False
        if launch is not None:
            try:
                launch.write(url)
                opened = _open_in_browser(launch.uri)
            except OSError:
                launch.remove()
        if not opened:
            print("(Couldn't open a browser; open the link above yourself.)")
    return 0


def show(open_browser: bool = False) -> int:
    state = instance.live_instance()
    if state is None:
        print(NOT_RUNNING)
        return 1
    print(_describe(state))
    return _print_link(state, open_browser)


def _already_running(state: ServerState, open_browser: bool) -> int:
    print(f"Floe is already running, so no new server was started.\n{_describe(state)}")
    return _print_link(state, open_browser)


def _existing_instance(open_browser: bool) -> int:
    """Another process holds the instance lock: wait for it to answer (it may still be
    starting) and report it like `_already_running`."""
    deadline = time.monotonic() + BACKGROUND_READY_TIMEOUT
    while True:
        running = instance.live_instance(clean_stale=False)
        if running is not None:
            return _already_running(running, open_browser)
        if time.monotonic() >= deadline or instance.lock_is_free():
            break
        time.sleep(0.2)
    print("floe: another Floe instance is already running but isn't answering. "
          "Try `floe show` in a moment, or `floe stop`.", file=sys.stderr)
    return ALREADY_RUNNING


# --------------------------------------------------------------------------- stop
def stop() -> int:
    """Stop the running Floe. Only a server that proved (HMAC over a fresh nonce) that
    it is the instance in the state file is asked to shut down, and only that pid is
    ever terminated. A server that doesn't answer is never killed."""
    state = instance.read_state()
    if state is None:
        if instance.state_path().exists():
            instance.remove_stale_state()
        print("Floe isn't running.")
        return 0
    pid = state.pid
    if not instance.pid_alive(pid):
        removed = instance.remove_stale_state(state)
        print("Floe isn't running (removed its stale state file)." if removed
              else "Floe isn't running.")
        return 0
    if instance.verify(state) is None:
        if instance.remove_stale_state(state) or instance.read_state() != state:
            print(f"Floe isn't responding; state file removed (process {pid} may still be "
                  "running — stop it manually if needed).")
        else:
            print(f"Floe isn't responding, and another process holds its instance lock "
                  f"(process {pid} may still be running — stop it manually if needed).")
        return 1
    if not instance.request_shutdown(state):
        print("floe: the running Floe didn't accept the stop request.", file=sys.stderr)
        return 1
    if instance.wait_for_exit(pid, STOP_TIMEOUT):
        instance.remove_stale_state(state)  # normally already removed by the server
        print(f"Floe (PID {pid}) stopped.")
        return 0
    # This pid proved moments ago that it is this Floe and accepted the shutdown request.
    if instance.terminate(pid):
        instance.remove_stale_state(state)
        print(f"Floe (PID {pid}) didn't stop in time and was terminated.")
        return 0
    print(f"floe: couldn't stop PID {pid}.", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- background
def _child_output_path() -> Path:
    from floe.core import paths

    return paths.logs_dir() / CHILD_OUTPUT_NAME


def _open_child_output() -> tuple[Path, int]:
    path = _child_output_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path, fd


def _output_tail(path: Path, lines: int = 20) -> str:
    from floe.core import diagnostics

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return diagnostics.redact("\n".join(text.strip().splitlines()[-lines:]))


def _spawn_child(port: int, out_fd: int) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "floe.cli", "serve", "--_child", "--no-browser",
           "--port", str(port)]
    env = dict(os.environ)
    if env.get("FLOE_HOME"):
        env["FLOE_HOME"] = os.path.abspath(env["FLOE_HOME"])
    env["PYTHONUNBUFFERED"] = "1"
    kwargs: dict = dict(
        stdin=subprocess.DEVNULL, stdout=out_fd, stderr=subprocess.STDOUT, close_fds=True,
        env=env,
    )
    if sys.platform == "win32":  # pragma: no cover - Windows only
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True  # no SIGHUP when the terminal closes
    return subprocess.Popen(cmd, **kwargs)  # noqa: S603 - our own interpreter and module


def _serve_background(port: int, open_browser: bool) -> int:
    out_path, out_fd = _open_child_output()
    try:
        child = _spawn_child(port, out_fd)
    except OSError as exc:
        print(f"floe: couldn't start Floe in the background ({exc.strerror}).",
              file=sys.stderr)
        return 1
    finally:
        os.close(out_fd)
    # The child takes the instance lock itself; if another instance owns it, the child
    # exits with ALREADY_RUNNING and we report that instance instead.
    deadline = time.monotonic() + BACKGROUND_READY_TIMEOUT
    state: ServerState | None = None
    other_since: float | None = None
    while time.monotonic() < deadline:
        if child.poll() is not None:
            break
        candidate = instance.read_state()
        if candidate is not None and instance.verify(candidate, timeout=1.0) is not None:
            if candidate.pid == child.pid:
                state = candidate
                break
            # Another instance answers. Normally our child is about to exit because it
            # lost the lock; if it keeps running, the pid differs only because of an
            # interpreter launcher (Windows venvs), so it is ours.
            other_since = other_since or time.monotonic()
            if time.monotonic() - other_since >= CHILD_EXIT_GRACE:
                state = candidate
                break
        time.sleep(0.1)
    if state is None and child.poll() == ALREADY_RUNNING:
        return _existing_instance(open_browser)
    if state is None:
        if child.poll() is None:
            child.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=5)
        tail = _output_tail(out_path)
        print("floe: Floe didn't start in the background.", file=sys.stderr)
        if tail:
            print(f"Its output ({out_path}):\n{tail}", file=sys.stderr)
        return 1
    # The child keeps running on its own; don't warn that it outlives this Popen.
    child.returncode = 0
    print(f"Floe {state.version} started on port {state.port}.")
    code = _print_link(state, open_browser)
    print(
        f"\nFloe is running in the background (PID {state.pid}). "
        "Use `floe show` to get a new link, `floe stop` to stop it."
    )
    return code


# --------------------------------------------------------------------------- serve
def serve(
    port: int = 0,
    host: str = BIND_ADDRESS,
    open_browser: bool = True,
    background: bool = False,
    child: bool = False,
) -> int:
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

    running = instance.live_instance()
    if running is not None:
        if child:
            print("floe: another Floe instance is already running.", file=sys.stderr)
            return ALREADY_RUNNING
        return _already_running(running, open_browser)
    if background and not child:
        return _serve_background(port, open_browser)

    # One server per data dir: the lock is held by this (server) process for its whole
    # lifetime and taken before binding; whoever can't get it defers to the owner.
    lock = instance.InstanceLock()
    try:
        acquired = lock.acquire()
    except OSError as exc:
        print(f"floe: can't create the instance lock {lock.path} ({exc.strerror})",
              file=sys.stderr)
        return 1
    if not acquired:
        if child:
            print("floe: another Floe instance is already running.", file=sys.stderr)
            return ALREADY_RUNNING
        return _existing_instance(open_browser)
    try:
        return _run_server(port, open_browser, child)
    finally:
        lock.release()


def _run_server(port: int, open_browser: bool, child: bool) -> int:
    """Bind, write the state file and run until shutdown (the caller holds the lock)."""
    import uvicorn

    from floe.core import diagnostics
    from floe.web.app import create_app

    token = secrets.token_urlsafe(32)
    control_key = instance.new_control_key()
    diagnostics.register_secret(token)
    diagnostics.register_secret(control_key)
    log = diagnostics.setup_logging()
    try:
        sock = _bind(port)
    except OSError as exc:
        print(f"floe: can't listen on {BIND_ADDRESS}:{port} ({exc.strerror})", file=sys.stderr)
        return 1
    actual_port = sock.getsockname()[1]
    url = f"http://{BIND_ADDRESS}:{actual_port}/?token={token}"

    launch: LaunchFile | None = None
    if open_browser and not child:
        try:
            launch = LaunchFile(url)
        except OSError:
            launch = None

    def token_used() -> None:
        if launch is not None:
            launch.remove()

    server: uvicorn.Server | None = None

    def request_exit() -> None:
        if server is not None:
            server.should_exit = True

    app = create_app(
        token=token,
        port=actual_port,
        token_ttl=TOKEN_TTL_SECONDS,
        on_token_used=token_used,
        control_key=control_key,
        on_shutdown=request_exit,
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
    pid = os.getpid()
    log.info("Floe %s serving on %s:%d%s", __version__, BIND_ADDRESS, actual_port,
             " (background)" if child else "")
    if child:
        # stdout is a log file here: never print the link.
        print(f"Floe {__version__} serving on {BIND_ADDRESS}:{actual_port} (PID {pid}).",
              flush=True)
    else:
        print(f"Floe {__version__} is running. Open this link (keep it private):\n\n  {url}\n")
        print(
            f"{LINK_NOTE} To open Floe again later (e.g. after closing its tab),\n"
            "run `floe show` in another terminal for a fresh link."
        )
        print("Press Ctrl+C to stop.", flush=True)
    expiry: threading.Timer | None = None
    own_state = ServerState(
        pid=pid, port=actual_port, started_at=time.time(), version=__version__,
        control_key=control_key,
    )
    try:
        instance.write_state(own_state)
        if launch is not None:
            expiry = threading.Timer(TOKEN_TTL_SECONDS, launch.remove)
            expiry.daemon = True
            expiry.start()
            if not _open_in_browser(launch.uri):
                print("(Couldn't open a browser; open the link above yourself.)")
        server.run(sockets=[sock])
    finally:
        instance.remove_state(own_state)  # only if it is still ours
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
            background=getattr(args, "background", False),
            child=getattr(args, "_child", False),
        )
    if args.command == "show":
        return show(open_browser=args.open and not args.no_browser)
    if args.command == "stop":
        return stop()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
