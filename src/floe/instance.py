"""Running-instance bookkeeping for `floe serve` / `floe show` / `floe stop` (SPEC §15.1,
§15.3). Standard library only (plus `floe.core.paths`), so the CLI stays quick.

* **State file** `paths.app_support_dir()/server.json` (dir 0700, file 0600, written
  atomically): `{pid, port, started_at, version, control_key}`. `control_key` is a random
  secret used only for the `/_control/*` endpoints; it is distinct from the launch
  tokens, the session cookie and the `X-Floe-Auth` API key, none of which is ever
  written to disk. The file is removed by its owner on clean shutdown; a stale one is
  removed by the next `floe serve` / `show` / `stop`, but only while nobody holds the
  instance lock (so a live server's file is never deleted).
* **Instance lock** `server.lock` (0600) next to it: held (`flock` on POSIX, `msvcrt`
  byte lock on Windows) by the server process for its whole lifetime and taken *before*
  it binds, so at most one server runs per data dir. Never deleted.
* **Control client**: plain HTTP to `127.0.0.1:<port>`, no proxies, no `Origin` /
  `Sec-Fetch-*` headers (the server rejects browser requests). Before the control key is
  ever sent, the server must prove it knows it: the client sends a random nonce to
  `GET /_control/health` *without* the key and checks the reply's
  `HMAC(control_key, nonce | pid | port)` against the state file (`verify`). Whatever
  else might be listening on a stale port never sees the key.
* **LaunchFile**: a private HTML file that redirects the browser to a tokenized URL, so
  the token is never on a process command line. For `floe show --open` the CLI writes
  it itself from a strictly validated URL; the server only deletes its (validated)
  directory once the token is used or expires.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from floe.core import paths

STATE_FILE_NAME = "server.json"
LOCK_FILE_NAME = "server.lock"
CONTROL_HEADER = "X-Floe-Control"
LOCALHOST = "127.0.0.1"
HEALTH_TIMEOUT = 3.0
LAUNCH_DIR_PREFIX = "floe-launch-"
LAUNCH_FILE_NAME = "open-floe.html"
# A health nonce: URL-safe base64 characters only (what `secrets.token_urlsafe` makes).
NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_TOKEN_CHARS = r"[A-Za-z0-9_-]{16,256}"
# Control characters (C0, DEL, C1) and Unicode format characters such as bidi overrides.
_UNSAFE_CHARS = re.compile(
    "[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]"
)

_LAUNCH_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0;url={url}">
<title>Opening Floe</title></head>
<body><p>Opening Floe… If nothing happens, <a href="{url}">click here</a>.</p></body></html>
"""


# --------------------------------------------------------------------------- launch file
class LaunchFile:
    """A private HTML file that redirects the browser to the tokenized URL, so the
    token isn't on the browser's command line (visible to every user via `ps`)."""

    def __init__(self, url: str | None = None) -> None:
        """Create the private directory; with `url`, also write the file (else call
        `write(url)` later, e.g. once the URL is known)."""
        self._lock = threading.Lock()
        self.dir: Path | None = Path(tempfile.mkdtemp(prefix=LAUNCH_DIR_PREFIX))  # 0700
        os.chmod(self.dir, 0o700)
        self.path = self.dir / LAUNCH_FILE_NAME
        if url is not None:
            self.write(url)

    def write(self, url: str) -> None:
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


def remove_launch_dir(path: str) -> bool:
    """Server side of `floe show --open`: delete a launch directory the CLI created.
    Only a plain (non-symlink) `floe-launch-*` directory directly in this user's temp
    dir is touched, and only its `open-floe.html` file and the (then empty) directory
    itself are removed; anything else is left alone. True if it's gone."""
    try:
        candidate = Path(path)
        if not candidate.is_absolute() or candidate.is_symlink():
            return False
        temp_root = Path(tempfile.gettempdir()).resolve()
        resolved = candidate.resolve()
        if resolved.parent != temp_root or not resolved.name.startswith(LAUNCH_DIR_PREFIX):
            return False
        if not resolved.is_dir():
            return not resolved.exists()
        page = resolved / LAUNCH_FILE_NAME
        if page.is_symlink() or page.is_file():
            page.unlink()
        resolved.rmdir()
    except (OSError, ValueError):
        return False
    return True


def safe_text(value: object, limit: int = 200) -> str:
    """`value` as a single line safe to print on a terminal: control and format
    characters (escape sequences, newlines, bidi overrides) removed, length capped."""
    return _UNSAFE_CHARS.sub("", str(value))[:limit]


def valid_login_url(url: object, port: int) -> str | None:
    """`url` if it is exactly `http://127.0.0.1:<port>/?token=<urlsafe base64>`, else None."""
    if not isinstance(url, str):
        return None
    pattern = rf"http://127\.0\.0\.1:{int(port)}/\?token={_TOKEN_CHARS}"
    return url if re.fullmatch(pattern, url, flags=re.ASCII) else None


# --------------------------------------------------------------------------- state file
@dataclass(frozen=True)
class ServerState:
    pid: int
    port: int
    started_at: float  # UNIX time
    version: str
    control_key: str

    @property
    def base_url(self) -> str:
        return f"http://{LOCALHOST}:{self.port}"


def state_path() -> Path:
    return paths.app_support_dir() / STATE_FILE_NAME


def new_control_key() -> str:
    return secrets.token_urlsafe(32)


def write_state(state: ServerState) -> Path:
    """Atomically write the state file (0600, in a 0700 directory)."""
    target = state_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(target.parent, 0o700)
    tmp = target.with_name(f".{STATE_FILE_NAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(asdict(state), indent=2).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    return target


def read_state() -> ServerState | None:
    """The state file's contents, or None if it's missing or unreadable/malformed."""
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
        state = ServerState(
            pid=int(raw["pid"]),
            port=int(raw["port"]),
            started_at=float(raw["started_at"]),
            version=str(raw["version"]),
            control_key=str(raw["control_key"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if state.pid <= 0 or not 0 < state.port <= 65535 or not state.control_key:
        return None
    return state


def _same_owner(a: ServerState, b: ServerState) -> bool:
    return (a.pid == b.pid and a.started_at == b.started_at
            and hmac.compare_digest(a.control_key.encode(), b.control_key.encode()))


def remove_state(owner: ServerState) -> bool:
    """Owner side (the server, which holds the instance lock): remove the state file if
    it is still the one `owner` wrote (same pid, start time and control key)."""
    current = read_state()
    if current is None or not _same_owner(current, owner):
        return False
    with contextlib.suppress(FileNotFoundError, OSError):
        state_path().unlink()
        return True
    return False


def remove_stale_state(stale: ServerState | None = None) -> bool:
    """Any other command: remove a stale state file, but only while the instance lock
    is free (nobody live owns it) and, with `stale`, only if the file still holds that
    state (without it: only if it is unreadable). The lock is held while checking and
    deleting, so a server starting meanwhile can't have its fresh file removed."""
    lock = InstanceLock()
    try:
        if not lock.acquire():
            return False
    except OSError:  # can't even open the lock file: leave everything alone
        return False
    try:
        if not state_path().exists():
            return False
        current = read_state()
        if stale is None:
            if current is not None:
                return False
        elif current is None or not _same_owner(current, stale):
            return False
        with contextlib.suppress(FileNotFoundError, OSError):
            state_path().unlink()
            return True
        return False
    finally:
        lock.release()


# --------------------------------------------------------------------------- instance lock
def lock_path() -> Path:
    return paths.app_support_dir() / LOCK_FILE_NAME


class InstanceLock:
    """The exclusive, non-blocking instance lock (`server.lock`, 0600). A running server
    holds it for its lifetime; the OS drops it when the process dies, however it dies.
    Conflicts are per open file, so a second `acquire` from another `InstanceLock` in
    the same process fails too."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or lock_path()
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """True if taken (or already held); False if another open file holds it.
        OSError if the lock file can't be created or opened."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        fd = os.open(self.path, flags, 0o600)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        try:
            _lock_fd(fd)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        with contextlib.suppress(OSError):
            _unlock_fd(fd)
        with contextlib.suppress(OSError):
            os.close(fd)

    def __enter__(self) -> InstanceLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _lock_fd(fd: int) -> None:
    """Take an exclusive lock on `fd` without blocking; OSError if someone holds it."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def lock_is_free() -> bool:
    """True if no live process holds the instance lock (checked by taking it briefly)."""
    lock = InstanceLock()
    try:
        if not lock.acquire():
            return False
    except OSError:
        return False
    lock.release()
    return True


# --------------------------------------------------------------------------- processes
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_pid_alive(pid)
    # Reap it if it's our own (exited) child, so a zombie doesn't count as alive.
    with contextlib.suppress(ChildProcessError, OSError):
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    status = Path(f"/proc/{pid}/status")
    with contextlib.suppress(OSError):
        for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("State:"):
                return "Z" not in line.split(":", 1)[1].split()[0]
    return True


def _win_pid_alive(pid: int) -> bool:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: it exists
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def terminate(pid: int, timeout: float = 5.0) -> bool:
    """Terminate `pid` (SIGTERM, then SIGKILL after `timeout`). True if it's gone."""
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    if wait_for_exit(pid, timeout):
        return True
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    return wait_for_exit(pid, 2.0)


def wait_for_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while pid_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


# --------------------------------------------------------------------------- control client
class ControlError(Exception):
    """A control request failed (no answer, or a non-2xx status)."""


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def health_proof(control_key: str, nonce: str, pid: int, port: int) -> str:
    """The server's answer to a health nonce: proof that it holds `control_key`, bound
    to its pid and port."""
    message = f"floe-health-v1|{nonce}|{int(pid)}|{int(port)}".encode()
    return hmac.new(control_key.encode(), message, hashlib.sha256).hexdigest()


def _get_json(request: urllib.request.Request, route: str, port: int,
              timeout: float) -> dict[str, Any]:
    try:
        with _opener.open(request, timeout=timeout) as response:
            data = json.loads(response.read(64 * 1024) or b"{}")
    except urllib.error.HTTPError as exc:
        raise ControlError(f"HTTP {exc.code} from /_control/{route}") from None
    except (OSError, ValueError) as exc:
        raise ControlError(f"no answer from Floe on port {port} ({type(exc).__name__})") \
            from None
    if not isinstance(data, dict):
        raise ControlError(f"unexpected answer from /_control/{route}")
    return data


def verify(state: ServerState, timeout: float = HEALTH_TIMEOUT) -> dict[str, Any] | None:
    """Mutual-proof health check. Sends a fresh nonce (never the control key) and
    returns the server's answer only if it carries `HMAC(control_key, nonce|pid|port)`
    for the state file's pid and port, i.e. the server on that port is the one that
    wrote the state file; else None."""
    nonce = secrets.token_urlsafe(32)
    request = urllib.request.Request(
        f"{state.base_url}/_control/health?nonce={nonce}", method="GET",
    )
    try:
        info = _get_json(request, "health", state.port, timeout)
    except ControlError:
        return None
    proof = info.get("proof")
    if not isinstance(proof, str) or not info.get("ok"):
        return None
    expected = health_proof(state.control_key, nonce, state.pid, state.port)
    if not hmac.compare_digest(proof.encode("ascii", "replace"), expected.encode()):
        return None
    try:
        if int(info.get("pid", -1)) != state.pid or int(info.get("port", -1)) != state.port:
            return None
    except (TypeError, ValueError):
        return None
    return info


def health(state: ServerState, timeout: float = HEALTH_TIMEOUT) -> dict[str, Any] | None:
    """Alias of `verify`: a health answer counts only if the server proved itself."""
    return verify(state, timeout)


def control_request(
    state: ServerState, method: str, route: str, body: dict[str, Any] | None = None,
    timeout: float = HEALTH_TIMEOUT,
) -> dict[str, Any]:
    """An authenticated control request. The server is verified first (`verify`); the
    control key is sent only if it proved it already knows it."""
    if verify(state, timeout) is None:
        raise ControlError(f"Floe on port {state.port} didn't prove it is the recorded "
                           "instance")
    data = json.dumps(body or {}).encode() if method == "POST" else None
    request = urllib.request.Request(
        f"{state.base_url}/_control/{route}", data=data, method=method,
        headers={CONTROL_HEADER: state.control_key, "Content-Type": "application/json"},
    )
    return _get_json(request, route, state.port, timeout)


def live_instance(clean_stale: bool = True) -> ServerState | None:
    """The running instance: state file + live pid + a verified health check. A stale
    state file is removed when `clean_stale` (only while the instance lock is free)."""
    state = read_state()
    if state is None:
        if clean_stale and state_path().exists():
            remove_stale_state()  # unreadable / malformed
        return None
    if pid_alive(state.pid) and verify(state) is not None:
        return state
    if clean_stale:
        remove_stale_state(state)
    return None


def login_link(state: ServerState, launch_dir: str | None = None) -> str:
    """Ask the verified server for a fresh single-use launch link and return it, only
    if it is exactly `http://127.0.0.1:<port>/?token=<urlsafe>` (else ControlError).
    With `launch_dir` (a `LaunchFile` directory the caller will write the page into),
    the server deletes that directory once the token is used or expires."""
    body: dict[str, Any] = {}
    if launch_dir is not None:
        body["launch_dir"] = launch_dir
    answer = control_request(state, "POST", "login-link", body)
    url = valid_login_url(answer.get("url"), state.port)
    if url is None:
        raise ControlError("the running Floe returned an unexpected link; refusing it")
    return url


def request_shutdown(state: ServerState) -> bool:
    try:
        control_request(state, "POST", "shutdown")
    except ControlError:
        return False
    return True
