"""`floe` / `floe serve` CLI (SPEC §15.1, §15.3). uvicorn is never actually started."""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import pytest
import uvicorn

from floe import __version__, cli
from floe.core import diagnostics, paths


@pytest.fixture
def fake_server(monkeypatch):
    runs: list[dict] = []

    def run(self, sockets=None):
        runs.append({"config": self.config, "ports": [s.getsockname()[1] for s in sockets]})

    monkeypatch.setattr(uvicorn.Server, "run", run)
    return runs


@pytest.fixture
def opened(monkeypatch):
    """Records each URL handed to the browser, plus (for a file:// launch page) its
    content and permissions at the time it was opened."""

    class Opened(list):
        files: list[dict]

    urls = Opened()
    urls.files = []

    def fake_open(url):
        urls.append(url)
        if url.startswith("file:"):
            path = Path(url2pathname(urlsplit(url).path))
            urls.files.append({
                "path": path,
                "text": path.read_text(encoding="utf-8"),
                "mode": stat.S_IMODE(path.stat().st_mode),
                "dir_mode": stat.S_IMODE(path.parent.stat().st_mode),
            })
        return True

    monkeypatch.setattr(cli.webbrowser, "open", fake_open)
    return urls


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.1", "::", "example.invalid"])
def test_refuses_non_local_host(host, capsys, fake_server, opened):
    assert cli.main(["serve", "--host", host, "--no-browser"]) == 2
    assert "refusing" in capsys.readouterr().err
    assert fake_server == [] and opened == []


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_serve_prints_url_and_never_logs_token(capsys, fake_server, opened):
    assert cli.main(["serve", "--no-browser"]) == 0
    out = capsys.readouterr().out
    m = re.search(r"http://127\.0\.0\.1:(\d+)/\?token=([A-Za-z0-9_-]+)", out)
    assert m, out
    port, token = int(m.group(1)), m.group(2)
    assert len(token) >= 40 and port > 0
    (run,) = fake_server
    assert run["ports"] == [port]
    config = run["config"]
    assert config.access_log is False and config.log_level == "warning"
    assert opened == []
    for handler in diagnostics.logging.getLogger("floe").handlers:
        handler.flush()
    log_text = (paths.logs_dir() / "app.log").read_text(encoding="utf-8")
    assert f"serving on 127.0.0.1:{port}" in log_text
    assert token not in log_text
    assert diagnostics.redact(f"x {token} y") == "x *** y"


def test_no_args_means_serve_and_opens_browser_via_private_file(
    capsys, fake_server, opened
):
    assert cli.main([]) == 0
    assert len(fake_server) == 1
    out = capsys.readouterr().out
    m = re.search(r"http://127\.0\.0\.1:\d+/\?token=[A-Za-z0-9_-]+", out)
    assert m, out
    (opened_url,) = opened
    # The token is never on the browser's command line: only a local file's path is.
    assert opened_url.startswith("file:") and "token" not in opened_url
    (launch,) = opened.files
    assert m.group(0) in launch["text"] and 'http-equiv="refresh"' in launch["text"]
    if sys.platform != "win32":
        assert launch["mode"] == 0o600 and launch["dir_mode"] == 0o700
    # Deleted at shutdown.
    assert not launch["path"].exists() and not launch["path"].parent.exists()


def test_launch_file_removed_when_token_is_used(tmp_path):
    from fastapi.testclient import TestClient

    from floe.web.app import create_app

    launch = cli.LaunchFile("http://127.0.0.1:9/?token=synthetic-token-" + "t" * 30)
    assert launch.path.exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(launch.path).st_mode) == 0o600
    app = create_app(token="tok-" + "x" * 40, port=9, on_token_used=launch.remove)
    with TestClient(app, base_url="http://127.0.0.1:9") as client:
        r = client.get("/?token=tok-" + "x" * 40, follow_redirects=False)
        assert r.status_code == 200 and "set-cookie" in r.headers
    assert not launch.path.exists()
    launch.remove()  # idempotent


def test_explicit_port_and_localhost(capsys, fake_server, opened):
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert cli.main(["serve", "--port", str(free), "--host", "localhost", "--no-browser"]) == 0
    assert f"http://127.0.0.1:{free}/?token=" in capsys.readouterr().out
