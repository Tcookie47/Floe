"""Instance management (SPEC §15.1, §15.3): the state file, the `/_control/...`
endpoints, extra single-use login links, `floe show` / `floe stop`, single-instance
detection, and a real `floe serve --background` in a subprocess."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import httpx
import pytest
from fastapi.testclient import TestClient

from floe import __version__, cli, instance
from floe.core import diagnostics, paths
from floe.web.app import create_app
from floe.web.security import MAX_OUTSTANDING_TOKENS
from tests.web.conftest import BASE, PORT, TOKEN, signed_in_key

CONTROL_KEY = "control-key-" + "c" * 32
LINK_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/\?token=([A-Za-z0-9_-]+)")


def _state(**overrides) -> instance.ServerState:
    values = dict(pid=os.getpid(), port=PORT, started_at=time.time(), version=__version__,
                  control_key=CONTROL_KEY)
    values.update(overrides)
    return instance.ServerState(**values)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.fixture
def control_app(store):
    """`(app, shutdowns)`: an app with the control routes; `shutdowns` counts calls."""
    shutdowns: list[int] = []
    app = create_app(token=TOKEN, port=PORT, store=store, control_key=CONTROL_KEY,
                     on_shutdown=lambda: shutdowns.append(1))
    return app, shutdowns


@pytest.fixture
def ctl(control_app):
    """A non-browser client (no Origin / Sec-Fetch-*) sending the control key."""
    app, _ = control_app
    with TestClient(app, base_url=BASE, headers={"X-Floe-Control": CONTROL_KEY}) as client:
        yield client


# --------------------------------------------------------------------------- state file


def test_state_file_is_private_atomic_and_has_no_tokens():
    path = instance.write_state(_state())
    assert path == paths.app_support_dir() / "server.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"pid", "port", "started_at", "version", "control_key"}
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["server.json"]  # no temp left over
    own = _state(started_at=data["started_at"])
    assert instance.read_state() == own
    # Only the owner (same pid, start time and control key) removes it.
    assert not instance.remove_state(_state(pid=os.getpid() + 1, started_at=own.started_at))
    assert not instance.remove_state(_state(started_at=own.started_at, control_key="k" * 43))
    assert not instance.remove_state(_state(started_at=own.started_at + 1))
    assert path.exists()
    assert instance.remove_state(own)
    assert not path.exists() and instance.read_state() is None


def test_malformed_state_is_ignored_and_cleaned():
    path = paths.app_support_dir() / "server.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert instance.read_state() is None
    assert instance.live_instance() is None
    assert not path.exists()


def test_foreground_serve_writes_state_without_secrets_and_removes_it(
    capsys, monkeypatch
):
    import uvicorn

    seen: list[dict] = []

    def run(self, sockets=None):
        path = instance.state_path()
        seen.append({"text": path.read_text(encoding="utf-8"),
                     "mode": stat.S_IMODE(path.stat().st_mode),
                     "api_key": self.config.app.state.api_key})

    monkeypatch.setattr(uvicorn.Server, "run", run)
    assert cli.main(["serve", "--no-browser"]) == 0
    token = LINK_RE.search(capsys.readouterr().out).group(2)
    (at_run,) = seen
    data = json.loads(at_run["text"])
    assert data["pid"] == os.getpid() and data["version"] == __version__
    assert len(data["control_key"]) >= 40
    assert token not in at_run["text"] and at_run["api_key"] not in at_run["text"]
    assert data["control_key"] not in (token, at_run["api_key"])
    if sys.platform != "win32":
        assert at_run["mode"] == 0o600
    assert not instance.state_path().exists()  # removed at shutdown
    assert diagnostics.redact(data["control_key"]) == "***"


def test_stale_state_with_dead_pid_is_cleaned_by_show_and_stop(capsys):
    instance.write_state(_state(pid=_dead_pid()))
    assert cli.main(["show"]) == 1
    assert "Floe isn't running. Start it with `floe serve --background`." in (
        capsys.readouterr().out
    )
    assert not instance.state_path().exists()
    instance.write_state(_state(pid=_dead_pid()))
    assert cli.main(["stop"]) == 0
    assert "isn't running" in capsys.readouterr().out
    assert not instance.state_path().exists()


def test_stale_state_whose_health_check_fails_is_cleaned():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    instance.write_state(_state(port=free))  # our own (live) pid, but nothing listening
    assert instance.live_instance() is None
    assert not instance.state_path().exists()


def test_show_when_nothing_ever_ran(capsys):
    assert cli.main(["show", "--open"]) == 1
    assert "Floe isn't running" in capsys.readouterr().out


# --------------------------------------------------------------------------- control auth


def test_control_routes_need_the_key(ctl):
    for key in (None, "wrong", CONTROL_KEY[:-1], CONTROL_KEY + "x"):
        headers = {"X-Floe-Control": key} if key else {}
        if key is None:
            ctl.headers.pop("X-Floe-Control")
        for method, route in (("GET", "health"), ("POST", "login-link"), ("POST", "shutdown")):
            r = ctl.request(method, f"/_control/{route}", headers=headers)
            assert r.status_code == 401, (key, route)
            assert r.json()["error"]["type"] == "Unauthorized"
        ctl.headers["X-Floe-Control"] = CONTROL_KEY


@pytest.mark.parametrize("header", [
    ("Origin", BASE), ("Origin", "http://evil.example"), ("Origin", "null"),
    ("Sec-Fetch-Site", "same-origin"), ("Sec-Fetch-Mode", "cors"),
    ("Sec-Fetch-Dest", "empty"),
])
def test_browser_requests_to_control_routes_are_refused(ctl, control_app, header):
    _, shutdowns = control_app
    for method, route in (("GET", "health"), ("POST", "login-link"), ("POST", "shutdown"),
                          ("OPTIONS", "shutdown")):
        r = ctl.request(method, f"/_control/{route}", headers=dict([header]))
        assert r.status_code == 403, (header, route)
    assert shutdowns == []


def test_control_routes_check_host(ctl):
    r = ctl.get("/_control/health", headers={"Host": "evil.example"})
    assert r.status_code == 400
    assert ctl.get("/_control/health", headers={"Host": f"localhost:{PORT}"}).status_code == 200


def test_control_health_and_shutdown(ctl, control_app):
    _, shutdowns = control_app
    r = ctl.get("/_control/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "version": __version__, "pid": os.getpid()}
    assert ctl.post("/_control/shutdown").json() == {"ok": True}
    assert shutdowns == [1]


def test_control_routes_absent_without_a_key(make_client):
    client = make_client()  # signed in: cookie + API key
    assert client.get("/_control/health", headers={"X-Floe-Control": "x" * 40}).status_code \
        == 404
    assert client.post("/_control/shutdown").status_code == 404


def test_signed_in_browser_session_cannot_use_control_routes(control_app):
    app, shutdowns = control_app
    with TestClient(app, base_url=BASE) as client:
        key = signed_in_key(client.get(f"/?token={TOKEN}", follow_redirects=False))
        headers = {"X-Floe-Auth": key, "Origin": BASE}
        assert client.post("/_control/shutdown", headers=headers).status_code == 403
        assert client.post("/_control/login-link", headers={"X-Floe-Auth": key}).status_code \
            == 401
    assert shutdowns == []


def test_control_logs_route_and_outcome_only(ctl):
    diagnostics.setup_logging()
    url = ctl.post("/_control/login-link").json()["url"]
    ctl.get("/_control/health", headers={"X-Floe-Control": "wrong-control-key"})
    for handler in diagnostics.logging.getLogger("floe").handlers:
        handler.flush()
    text = (paths.logs_dir() / "app.log").read_text(encoding="utf-8")
    assert "login-link" in text and "Rejected a control request" in text
    token = LINK_RE.search(url).group(2)
    for secret in (token, CONTROL_KEY, "wrong-control-key"):
        assert secret not in text


# --------------------------------------------------------------------------- login links


def _exchange(app, url: str):
    client = TestClient(app, base_url=BASE)
    parts = urlsplit(url)
    return client, client.get(f"/?{parts.query}", follow_redirects=False)


def test_login_links_are_single_use_and_keep_the_same_api_key(ctl, control_app):
    app, _ = control_app
    first = ctl.post("/_control/login-link").json()
    second = ctl.post("/_control/login-link").json()
    assert first["expires_in"] == 120
    assert first["url"] != second["url"]
    assert first["url"].startswith(f"http://127.0.0.1:{PORT}/?token=")
    c1, r1 = _exchange(app, first["url"])
    c2, r2 = _exchange(app, second["url"])
    key1, key2 = signed_in_key(r1), signed_in_key(r2)
    # Same per-process API key (and cookie), so tabs opened earlier keep working.
    assert key1 == key2 == app.state.api_key
    assert r1.headers["set-cookie"] == r2.headers["set-cookie"]
    assert c1.get("/api/about", headers={"X-Floe-Auth": key1}).status_code == 200
    # Each link works once.
    _, again = _exchange(app, first["url"])
    assert again.status_code == 401 and "set-cookie" not in again.headers
    # The startup token still works (independently of the minted ones).
    _, startup = _exchange(app, f"{BASE}/?token={TOKEN}")
    assert startup.status_code == 200
    for c in (c1, c2):
        c.close()


def test_login_links_expire(store):
    app = create_app(token=TOKEN, port=PORT, store=store, control_key=CONTROL_KEY,
                     token_ttl=0.05)
    with TestClient(app, base_url=BASE, headers={"X-Floe-Control": CONTROL_KEY}) as ctl:
        url = ctl.post("/_control/login-link").json()["url"]
        time.sleep(0.1)
        _, r = _exchange(app, url)
        assert r.status_code == 401
        assert app.state.launch_tokens.outstanding() == 0


def test_outstanding_login_links_are_capped(ctl, control_app):
    app, _ = control_app
    urls = [ctl.post("/_control/login-link").json()["url"]
            for _ in range(MAX_OUTSTANDING_TOKENS + 1)]
    assert app.state.launch_tokens.outstanding() == MAX_OUTSTANDING_TOKENS
    # The oldest outstanding tokens (the startup token, then the first minted) are gone.
    assert _exchange(app, f"{BASE}/?token={TOKEN}")[1].status_code == 401
    assert _exchange(app, urls[0])[1].status_code == 401
    assert _exchange(app, urls[-1])[1].status_code == 200


def test_login_link_with_launch_dir_removed_by_the_server(ctl, control_app):
    app, _ = control_app
    launch = instance.LaunchFile()  # the CLI makes the dir and writes the page itself
    body = ctl.post("/_control/login-link", json={"launch_dir": str(launch.dir)}).json()
    assert set(body) == {"url", "expires_in"}  # no launch_uri from the server any more
    launch.write(body["url"])
    if sys.platform != "win32":
        assert stat.S_IMODE(launch.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(launch.path.parent.stat().st_mode) == 0o700
    assert _exchange(app, body["url"])[1].status_code == 200
    assert not launch.path.exists() and not launch.path.parent.exists()  # removed once used
    launch = instance.LaunchFile()
    ctl.post("/_control/login-link", json={"launch_dir": str(launch.dir)})
    app.state.launch_tokens.clear()  # as at shutdown
    assert not launch.path.parent.exists()


def test_server_only_removes_floe_launch_dirs(tmp_path):
    victim = tmp_path / "floe-launch-victim"
    victim.mkdir()
    (victim / "open-floe.html").write_text("x", encoding="utf-8")
    other = Path(instance.tempfile.mkdtemp(prefix="not-floe-"))
    (other / "open-floe.html").write_text("x", encoding="utf-8")
    full = instance.LaunchFile("http://127.0.0.1:9/?token=" + "t" * 40)
    (full.dir / "precious.txt").write_text("keep", encoding="utf-8")
    try:
        # Not directly in the temp dir / wrong prefix / relative / non-empty: left alone.
        assert not instance.remove_launch_dir(str(victim))
        assert not instance.remove_launch_dir(str(other))
        assert not instance.remove_launch_dir("floe-launch-x")
        assert not instance.remove_launch_dir(str(full.dir))
        assert victim.exists() and (other / "open-floe.html").exists()
        assert (full.dir / "precious.txt").exists()
    finally:
        instance.shutil.rmtree(other, ignore_errors=True)
        full.remove()


# --------------------------------------------------------------------------- in-process server


@pytest.fixture
def served(monkeypatch, capsys):
    """A real foreground `floe serve --no-browser` in a thread (FLOE_HOME is temp)."""
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(cli.main(["serve", "--no-browser"])), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 15
    state = None
    while state is None and time.monotonic() < deadline:
        state = instance.live_instance(clean_stale=False)
        time.sleep(0.05)
    assert state is not None, capsys.readouterr()
    capsys.readouterr()
    yield state
    if thread.is_alive():
        instance.request_shutdown(state)
        thread.join(15)
    assert not thread.is_alive()


def test_show_and_single_instance_with_a_foreground_server(served, capsys, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda uri: opened.append(uri) or True)
    assert cli.main(["show"]) == 0
    out = capsys.readouterr().out
    assert re.search(
        rf"Floe {re.escape(__version__)} is running \(PID {os.getpid()}, since "
        rf"\d{{4}}-\d\d-\d\d \d\d:\d\d, port {served.port}\)", out), out
    assert "works once, within two minutes" in out
    link = LINK_RE.search(out)
    assert int(link.group(1)) == served.port and opened == []
    r = httpx.get(link.group(0), follow_redirects=False, trust_env=False)
    assert r.status_code == 200 and "floe_session=" in r.headers["set-cookie"]
    assert httpx.get(link.group(0), follow_redirects=False, trust_env=False).status_code == 401

    # --open: the browser gets a private file:// launch page, not the token URL.
    assert cli.main(["show", "--open"]) == 0
    capsys.readouterr()
    (uri,) = opened
    assert uri.startswith("file:") and "token" not in uri

    # A second `floe serve` (foreground or background) doesn't start another server.
    assert cli.main(["serve", "--no-browser"]) == 0
    out = capsys.readouterr().out
    assert "already running" in out and LINK_RE.search(out)
    assert cli.main(["serve", "--background", "--no-browser"]) == 0
    assert "already running" in capsys.readouterr().out
    assert instance.read_state() == served


def test_shutdown_stops_the_server_and_removes_the_state_file(served):
    assert instance.request_shutdown(served)
    deadline = time.monotonic() + 10
    while instance.state_path().exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not instance.state_path().exists()
    assert instance.health(served, timeout=1) is None


# --------------------------------------------------------------------------- real background


def _floe(home: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ, FLOE_HOME=str(home),
               PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
    env.pop("QT_QPA_PLATFORM", None)
    return subprocess.run([sys.executable, "-m", "floe.cli", *args], env=env,
                          capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture
def background_home(tmp_path):
    """A temp FLOE_HOME; any server still recorded there is killed at teardown."""
    yield tmp_path
    state_file = tmp_path / "support" / "server.json"
    if state_file.exists():
        pid = json.loads(state_file.read_text(encoding="utf-8"))["pid"]
        if instance.pid_alive(pid):
            instance.terminate(pid)


def test_real_background_server_show_exchange_and_stop(background_home):
    home = background_home
    started = _floe(home, "serve", "--background", "--no-browser", "--port", "0")
    assert started.returncode == 0, started.stdout + started.stderr
    assert "Floe is running in the background (PID" in started.stdout
    assert "Use `floe show` to get a new link, `floe stop` to stop it." in started.stdout
    first = LINK_RE.search(started.stdout)
    assert first, started.stdout
    data = json.loads((home / "support" / "server.json").read_text(encoding="utf-8"))
    pid, port = data["pid"], data["port"]
    assert int(first.group(1)) == port and instance.pid_alive(pid)
    if hasattr(os, "getsid"):
        assert os.getsid(pid) != os.getsid(0)  # detached: its own session

    shown = _floe(home, "show")
    assert shown.returncode == 0, shown.stderr
    assert f"is running (PID {pid}" in shown.stdout
    link = LINK_RE.search(shown.stdout).group(0)
    with httpx.Client(trust_env=False) as client:
        r = client.get(link, follow_redirects=False)
        assert r.status_code == 200 and "floe_session" in client.cookies
        key = signed_in_key(r)
        assert client.get(f"http://127.0.0.1:{port}/api/about",
                          headers={"X-Floe-Auth": key}).status_code == 200
    assert httpx.get(link, follow_redirects=False, trust_env=False).status_code == 401

    stopped = _floe(home, "stop")
    assert stopped.returncode == 0, stopped.stderr
    assert f"Floe (PID {pid}) stopped." in stopped.stdout
    assert instance.wait_for_exit(pid, 10)
    assert not (home / "support" / "server.json").exists()
    assert _floe(home, "show").returncode == 1

    # Nothing sensitive reached the logs.
    logs = "".join(p.read_text(encoding="utf-8") for p in (home / "logs").iterdir())
    for secret in (first.group(2), LINK_RE.search(shown.stdout).group(2), key,
                   data["control_key"]):
        assert secret not in logs
    if sys.platform != "win32":
        for p in (home / "logs").iterdir():
            assert stat.S_IMODE(p.stat().st_mode) == 0o600, p


def test_background_start_failure_reports_the_error(background_home):
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        started = _floe(background_home, "serve", "--background", "--no-browser",
                        "--port", str(port))
    assert started.returncode != 0
    assert "didn't start in the background" in started.stderr
    assert "can't listen" in started.stderr
    assert not (background_home / "support" / "server.json").exists()


# --------------------------------------------------------------------------- security review

import hashlib  # noqa: E402
import hmac  # noqa: E402
import socket  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def dummy_floe():
    """A live, unrelated process whose command line mentions "floe"."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", "floe", "serve"]
    )
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(10)


class _FakeServer:
    """A non-Floe HTTP server on 127.0.0.1 recording every request's headers.
    `respond(handler, path) -> dict` builds the JSON answer."""

    def __init__(self, respond) -> None:
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                seen.append((self.command, self.path, dict(self.headers)))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                body = json.dumps(respond(self, self.path)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

            def log_message(self, *_args) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def control_keys_seen(self) -> list[str]:
        return [h[k] for _, _, h in self.seen for k in h if k.lower() == "x-floe-control"]


@pytest.fixture
def fake_server():
    servers: list[_FakeServer] = []

    def make(respond) -> _FakeServer:
        servers.append(_FakeServer(respond))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def _nonce(path: str) -> str:
    return dict(p.split("=", 1) for p in urlsplit(path).query.split("&") if "=" in p)["nonce"]


def test_stop_never_kills_when_health_fails(dummy_floe, capsys):
    instance.write_state(_state(pid=dummy_floe.pid, port=_free_port()))  # nothing listening
    assert cli.main(["stop"]) == 1
    out = capsys.readouterr().out
    assert "Floe isn't responding; state file removed" in out
    assert f"process {dummy_floe.pid} may still be running" in out
    time.sleep(0.3)
    assert dummy_floe.poll() is None  # never signalled
    assert not instance.state_path().exists()


def test_stop_never_kills_or_sends_the_key_to_an_impostor(dummy_floe, fake_server, capsys):
    # Something else answers on the recorded port, echoing the recorded pid but it
    # can't produce the HMAC proof (it doesn't know the control key).
    fake = fake_server(lambda h, path: {"ok": True, "pid": dummy_floe.pid, "version": "x",
                                         "proof": "0" * 64})
    instance.write_state(_state(pid=dummy_floe.pid, port=fake.port))
    assert cli.main(["stop"]) == 1
    assert "isn't responding" in capsys.readouterr().out
    assert dummy_floe.poll() is None
    assert fake.seen and fake.control_keys_seen() == []
    assert all(method == "GET" and "/_control/health?nonce=" in path
               for method, path, _ in fake.seen)


def test_fake_server_without_the_key_never_gets_it(dummy_floe, fake_server, capsys,
                                                     monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda uri: opened.append(uri) or True)

    def respond(_h, path):
        if path.startswith("/_control/health"):
            return {"ok": True, "pid": dummy_floe.pid, "port": fake.port, "proof": "ab" * 32}
        return {"url": f"http://127.0.0.1:{fake.port}/?token=" + "p" * 43}

    fake = fake_server(respond)
    for args in (["show", "--open"], ["show"], ["serve", "--background", "--no-browser"]):
        instance.write_state(_state(pid=dummy_floe.pid, port=fake.port))
        if args[0] == "serve":
            # Don't start a real server here: just check the fake isn't trusted.
            monkeypatch.setattr(cli, "_serve_background", lambda *a: 0)
        code = cli.main(args)
        out = capsys.readouterr()
        assert "token=" not in out.out + out.err
        if args[0] == "show":
            assert code == 1 and "Floe isn't running" in out.out
    assert fake.control_keys_seen() == [] and opened == []
    assert dummy_floe.poll() is None


def _proving_server(fake_server, pid: int, key: str, url_for, version="1.0"):
    """A server that *does* know the control key (so it passes `verify`) but returns
    whatever `url_for(port)` gives as the login link."""
    holder: dict = {}

    def respond(_h, path):
        port = holder["fake"].port
        if path.startswith("/_control/health"):
            nonce = _nonce(path)
            proof = instance.health_proof(key, nonce, pid, port)
            return {"ok": True, "pid": pid, "port": port, "version": version, "proof": proof}
        return {"url": url_for(port), "launch_uri": "file:///etc/passwd"}

    holder["fake"] = fake_server(respond)
    return holder["fake"]


@pytest.mark.parametrize("url_for", [
    lambda port: f"http://127.0.0.1:{port}/?token=abc\x1b]0;pwned\x07" + "a" * 30,
    lambda port: f"http://127.0.0.1:{port}/?token=" + "a" * 40 + "\n\x1b[2Jfake prompt",
    lambda port: f"http://127.0.0.1:{port}/?token=" + "a" * 40 + "&next=http://evil",
    lambda port: "http://evil.example/?token=" + "a" * 40,
    lambda port: f"http://127.0.0.1:{port + 1}/?token=" + "a" * 40,
    lambda port: f"javascript:alert(1)//127.0.0.1:{port}/?token=" + "a" * 40,
    lambda port: f"http://127.0.0.1:{port}@evil.example/?token=" + "a" * 40,
    lambda port: ["not", "a", "string"],
])
def test_malicious_login_urls_are_refused(url_for, dummy_floe, fake_server, capsys,
                                           monkeypatch, tmp_path):
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(instance.tempfile, "tempdir", str(temp))
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda uri: opened.append(uri) or True)
    fake = _proving_server(fake_server, dummy_floe.pid, CONTROL_KEY, url_for)
    instance.write_state(_state(pid=dummy_floe.pid, port=fake.port))
    assert cli.main(["show", "--open"]) == 1
    out = capsys.readouterr()
    assert "unexpected link" in out.err
    assert "token=" not in out.out + out.err and "\x1b" not in out.out + out.err
    assert opened == []  # neither the server's launch_uri nor anything else
    assert list(temp.iterdir()) == []  # no launch directory left behind


def test_valid_link_from_a_proven_server_opens_our_own_launch_file(
    dummy_floe, fake_server, capsys, monkeypatch
):
    opened: list[str] = []

    def fake_open(uri):
        opened.append((uri, Path(url2pathname(urlsplit(uri).path)).read_text("utf-8")))
        return True

    monkeypatch.setattr(cli.webbrowser, "open", fake_open)
    good = "http://127.0.0.1:{}/?token=" + "Ab-_9" * 9
    fake = _proving_server(fake_server, dummy_floe.pid, CONTROL_KEY, good.format,
                           version="9.9\x1b[31m\r\nINJECTED")
    instance.write_state(_state(pid=dummy_floe.pid, port=fake.port,
                                version="1.0\x1b[2J\nFAKE"))
    assert cli.main(["show", "--open"]) == 0
    out = capsys.readouterr().out
    assert good.format(fake.port) in out and "\x1b" not in out
    assert "Floe 1.0[2JFAKE is running" in out  # control characters stripped
    ((uri, text),) = opened
    assert uri.startswith("file:") and "passwd" not in uri  # server's launch_uri ignored
    assert good.format(fake.port) in text
    # The key was sent only after the server proved it knows it.
    first_keyed = next(i for i, (_, _, h) in enumerate(fake.seen)
                       if "X-Floe-Control" in h or "x-floe-control" in h)
    assert fake.seen[first_keyed - 1][1].startswith("/_control/health?nonce=")
    instance.shutil.rmtree(Path(url2pathname(urlsplit(uri).path)).parent, ignore_errors=True)


def test_url_validation_and_safe_text():
    port = 4321
    ok = f"http://127.0.0.1:{port}/?token=" + "a" * 43
    assert instance.valid_login_url(ok, port) == ok
    for bad in (ok + "\n", ok + " ", ok.replace("127.0.0.1", "localhost"), ok[:-40],
                ok.replace("a" * 43, "a" * 42 + "é"), ok + "#x", None, 5):
        assert instance.valid_login_url(bad, port) is None, bad
    assert instance.valid_login_url(ok, port + 1) is None
    assert instance.safe_text("v1\x1b[2J\r\n\x07‮ok\x9b") == "v1[2Jok"


def test_keyless_health_answers_only_with_a_proof(ctl, control_app):
    app, _ = control_app
    with TestClient(app, base_url=BASE) as bare:  # no control key
        assert bare.get("/_control/health").status_code == 401
        assert bare.get("/_control/health?nonce=short").status_code == 401
        assert bare.get("/_control/health?nonce=" + "n" * 20 + "!").status_code == 401
        assert bare.post("/_control/shutdown?nonce=" + "n" * 20).status_code == 401
        assert bare.post("/_control/login-link?nonce=" + "n" * 20).status_code == 401
        nonce = "n" * 32
        assert bare.get(f"/_control/health?nonce={nonce}",
                        headers={"Sec-Fetch-Mode": "cors"}).status_code == 403
        body = bare.get(f"/_control/health?nonce={nonce}").json()
        assert body["proof"] == instance.health_proof(CONTROL_KEY, nonce, os.getpid(), PORT)
        assert body["proof"] != instance.health_proof(CONTROL_KEY, nonce, os.getpid(), PORT + 1)
        assert CONTROL_KEY not in json.dumps(body)
        expected = hmac.new(CONTROL_KEY.encode(),
                            f"floe-health-v1|{nonce}|{os.getpid()}|{PORT}".encode(),
                            hashlib.sha256).hexdigest()
        assert body["proof"] == expected
    # A wrong key is still a 401, even with a nonce.
    r = ctl.get(f"/_control/health?nonce={nonce}", headers={"X-Floe-Control": "wrong"})
    assert r.status_code == 401


def test_control_rejection_logs_are_not_injectable(ctl):
    diagnostics.setup_logging()
    evil = "/_control/x%0A2026-01-01 00:00:00,000 INFO floe FORGED%0D%1B[2J"
    ctl.get(evil, headers={"Sec-Fetch-Site": "none"})
    ctl.get(evil, headers={"X-Floe-Control": "wrong"})
    ctl.get("/_control/health%0AFORGED", headers={"X-Floe-Control": "wrong"})
    for handler in diagnostics.logging.getLogger("floe").handlers:
        handler.flush()
    text = (paths.logs_dir() / "app.log").read_text(encoding="utf-8")
    assert "FORGED" not in text and "\x1b" not in text
    assert "Rejected a browser request to control route (other)" in text
    assert "Rejected a control request to (other) with a missing or wrong key" in text


# --------------------------------------------------------------------------- instance lock


def test_instance_lock_is_exclusive_private_and_released():
    first = instance.InstanceLock()
    assert first.acquire() and first.held
    if sys.platform != "win32":
        assert stat.S_IMODE(instance.lock_path().stat().st_mode) == 0o600
    second = instance.InstanceLock()
    assert not second.acquire() and not instance.lock_is_free()
    first.release()
    assert instance.lock_is_free()
    with second:
        assert second.acquire()
        assert not instance.lock_is_free()
    assert instance.lock_is_free() and instance.lock_path().exists()


def test_instance_lock_windows_branch(monkeypatch):
    """The msvcrt branch, exercised on any OS with a fake msvcrt."""
    import types

    held: set[tuple[int, int]] = set()
    calls: list[tuple[str, int]] = []

    def locking(fd, mode, nbytes):
        assert nbytes == 1 and os.lseek(fd, 0, os.SEEK_CUR) == 0
        st = os.fstat(fd)
        ident = (st.st_dev, st.st_ino)
        calls.append(("lock" if mode == fake.LK_NBLCK else "unlock", fd))
        if mode == fake.LK_NBLCK:
            if ident in held:
                raise PermissionError(13, "locked")
            held.add(ident)
        else:
            held.discard(ident)

    fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(instance.sys, "platform", "win32")
    a, b = instance.InstanceLock(), instance.InstanceLock()
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()
    assert [c[0] for c in calls] == ["lock", "lock", "unlock", "lock", "unlock"]


def test_stale_cleanup_never_deletes_a_live_owners_state(capsys):
    owner = instance.InstanceLock()
    assert owner.acquire()  # a live server (here: this test) holds the lock
    try:
        instance.write_state(_state(pid=_dead_pid()))
        assert instance.live_instance() is None
        assert cli.main(["show"]) == 1
        assert cli.main(["stop"]) == 0
        capsys.readouterr()
        assert instance.state_path().exists()  # kept: the lock owner may still be using it
        stale = instance.read_state()
        # A file replaced meanwhile by a new owner is not removed as "stale".
        assert not instance.remove_stale_state(stale)
    finally:
        owner.release()
    newer = _state(pid=_dead_pid(), started_at=time.time() + 5)
    instance.write_state(newer)
    assert not instance.remove_stale_state(stale)  # the file no longer holds `stale`
    assert instance.state_path().exists()
    assert instance.remove_stale_state(newer)
    assert not instance.state_path().exists()


def test_foreground_serve_holds_the_lock_while_running(monkeypatch, capsys):
    import uvicorn

    seen: list[bool] = []
    monkeypatch.setattr(uvicorn.Server, "run",
                        lambda self, sockets=None: seen.append(instance.lock_is_free()))
    assert cli.main(["serve", "--no-browser"]) == 0
    assert seen == [False]
    assert instance.lock_is_free()


def test_serve_defers_to_the_lock_owner_even_if_it_does_not_answer(served, monkeypatch,
                                                                   capsys):
    monkeypatch.setattr(cli, "BACKGROUND_READY_TIMEOUT", 0.5)
    real_verify = instance.verify
    monkeypatch.setattr(instance, "verify", lambda *a, **k: None)
    assert cli.main(["serve", "--no-browser"]) == cli.ALREADY_RUNNING
    assert "already running" in capsys.readouterr().err
    assert cli.main(["serve", "--_child", "--no-browser"]) == cli.ALREADY_RUNNING
    monkeypatch.setattr(instance, "verify", real_verify)
    assert instance.read_state() == served  # the live server's state file was kept
    assert instance.verify(served) is not None


def test_lock_released_on_shutdown(served):
    assert not instance.lock_is_free()
    assert instance.request_shutdown(served)
    deadline = time.monotonic() + 10
    while not instance.lock_is_free() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert instance.lock_is_free()
    assert not instance.state_path().exists()


def test_concurrent_background_starts_run_exactly_one_server(background_home):
    env = dict(os.environ, FLOE_HOME=str(background_home),
               PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring")
    env.pop("QT_QPA_PLATFORM", None)
    cmd = [sys.executable, "-m", "floe.cli", "serve", "--background", "--no-browser"]
    procs = [subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True) for _ in range(2)]
    outs = [p.communicate(timeout=90) for p in procs]
    try:
        assert [p.returncode for p in procs] == [0, 0], outs
        texts = [o + e for o, e in outs]
        started = [t for t in texts if "started on port" in t]
        existing = [t for t in texts if "already running" in t]
        assert len(started) == 1 and len(existing) == 1, texts
        pid = json.loads((background_home / "support" / "server.json")
                         .read_text(encoding="utf-8"))["pid"]
        assert f"(PID {pid})" in started[0] and f"PID {pid}," in existing[0]
        for text in texts:
            assert LINK_RE.search(text)
    finally:
        stopped = _floe(background_home, "stop")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    assert instance.wait_for_exit(pid, 10)
