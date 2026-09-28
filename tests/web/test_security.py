"""Local server protection (SPEC §15.3): token → cookie, Host, Origin, headers, logs."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from floe.core import diagnostics, paths
from floe.web.app import create_app
from floe.web.security import CSP, MAX_BODY_BYTES
from tests.web.conftest import BASE, PORT, TOKEN, signed_in_key


def test_no_cookie_is_401(make_client):
    client = make_client(logged_in=False)
    r = client.get("/api/about")
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "Unauthorized"
    page = client.get("/")
    assert page.status_code == 401
    assert "link printed in the terminal" in page.text
    assert client.get("/static/floe.css").status_code == 401


def test_wrong_token_is_401_and_sets_no_cookie(make_client):
    client = make_client(logged_in=False)
    r = client.get("/?token=not-the-token", follow_redirects=False)
    assert r.status_code == 401
    assert "set-cookie" not in r.headers
    assert client.get("/api/about").status_code == 401


def test_token_sets_cookie_and_refreshes_same_origin(make_client):
    client = make_client(logged_in=False)
    r = client.get(f"/?token={TOKEN}", follow_redirects=False)
    # A 200 page (not a 303) whose same-origin meta refresh carries the Strict cookie
    # even when the launch chain started from the cross-site file:// launch page.
    assert "location" not in r.headers
    key = signed_in_key(r)
    assert "<script" not in r.text and TOKEN not in r.text
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["content-security-policy"] == CSP
    assert len(key) >= 40 and key != TOKEN
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert TOKEN not in cookie and key not in cookie  # the cookie is a separate random value
    assert client.get("/api/about", headers={"X-Floe-Auth": key}).status_code == 200
    home = client.get("/")
    assert home.status_code == 200 and "Floe" in home.text
    assert client.get("/static/floe.css").status_code == 200


def test_bad_host_is_400(client):
    for host in ("evil.example", f"evil.example:{PORT}", "127.0.0.1:1", "0.0.0.0:8765"):
        r = client.get("/api/about", headers={"Host": host})
        assert r.status_code == 400, host
    assert client.get("/api/about", headers={"Host": f"localhost:{PORT}"}).status_code == 200


def test_state_changing_requests_need_matching_origin(client):
    del client.headers["Origin"]
    assert client.put("/api/prefs", json={}).status_code == 403
    for origin in ("http://evil.example", "null", f"http://127.0.0.1:{PORT + 1}",
                   f"https://127.0.0.1:{PORT}"):
        r = client.put("/api/prefs", json={}, headers={"Origin": origin})
        assert r.status_code == 403, origin
    assert client.put("/api/prefs", json={}, headers={"Origin": BASE}).status_code == 200
    ok = client.put(
        "/api/prefs", json={}, headers={"Origin": f"http://localhost:{PORT}"}
    )
    assert ok.status_code == 200
    # Referer fallback when no Origin is sent.
    assert client.put("/api/prefs", json={}, headers={"Referer": f"{BASE}/x"}).status_code == 200
    bad = client.put("/api/prefs", json={}, headers={"Referer": "http://evil.example/x"})
    assert bad.status_code == 403
    # GET needs no Origin.
    assert client.get("/api/prefs").status_code == 200


def test_security_headers(make_client):
    client = make_client(logged_in=False)
    denied = client.get("/api/about")
    client = make_client()
    for r in (denied, client.get("/api/about"), client.get("/")):
        assert r.headers["content-security-policy"] == CSP
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"
    assert "script-src" not in CSP and "unsafe-inline" not in CSP


def test_token_never_logged(make_client):
    diagnostics.setup_logging()
    client = make_client(logged_in=False)
    client.get("/?token=wrong-token-value", follow_redirects=False)
    client.get(f"/?token={TOKEN}", follow_redirects=False)
    client.get("/api/about", headers={"Host": "evil.example"})
    client.put("/api/prefs", json={}, headers={"Origin": "http://evil.example"})
    for handler in diagnostics.logging.getLogger("floe").handlers:
        handler.flush()
    text = (paths.logs_dir() / "app.log").read_text(encoding="utf-8")
    assert "Rejected" in text  # the rejections were logged...
    assert TOKEN not in text and "wrong-token-value" not in text  # ...without tokens


def test_launch_token_is_single_use(make_client):
    client = make_client()  # used the token once
    again = client.get(f"/?token={TOKEN}", follow_redirects=False)
    assert again.status_code == 401
    assert "set-cookie" not in again.headers
    assert "link printed in the terminal" in again.text
    # e.g. someone replaying it from `ps` / shell history, with a fresh cookie jar:
    other = TestClient(client.app, base_url=BASE)
    assert other.get(f"/?token={TOKEN}", follow_redirects=False).status_code == 401
    # The first session keeps working for the server's lifetime.
    assert client.get("/api/about").status_code == 200


def test_launch_token_expires(store):
    app = create_app(token=TOKEN, port=PORT, store=store, token_ttl=0.05)
    with TestClient(app, base_url=BASE) as client:
        time.sleep(0.1)
        r = client.get(f"/?token={TOKEN}", follow_redirects=False)
        assert r.status_code == 401 and "set-cookie" not in r.headers


def test_head_does_not_consume_the_token(make_client):
    client = make_client(logged_in=False)
    assert client.head(f"/?token={TOKEN}").status_code == 401
    assert client.get(f"/?token={TOKEN}", follow_redirects=False).status_code == 200


def test_api_needs_the_key_header_as_well_as_the_cookie(client):
    key = client.headers.pop("X-Floe-Auth")
    # Cookie alone (what another 127.0.0.1 port would receive) is not enough.
    r = client.get("/api/about")
    assert r.status_code == 401 and r.json()["error"]["type"] == "Unauthorized"
    assert client.put("/api/prefs", json={}).status_code == 401
    for wrong in ("wrong", key[:-1], key + "x", key.upper() if key.upper() != key else "Z"):
        assert client.get("/api/about", headers={"X-Floe-Auth": wrong}).status_code == 401
    assert client.get("/api/about", headers={"X-Floe-Auth": key}).status_code == 200
    # HTML and static files stay cookie-only.
    assert client.get("/").status_code == 200
    assert client.get("/static/floe.css").status_code == 200


def test_key_without_cookie_is_401(client):
    key = client.headers["X-Floe-Auth"]
    client.cookies.clear()
    assert client.get("/api/about", headers={"X-Floe-Auth": key}).status_code == 401


def test_api_key_is_redacted(client):
    key = client.headers["X-Floe-Auth"]
    assert diagnostics.redact(f"a {key} b") == "a *** b"


def test_oversized_body_is_413(client):
    big = b'{"x": "' + b"a" * (MAX_BODY_BYTES + 10) + b'"}'
    r = client.put("/api/prefs", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert r.json()["error"]["type"] == "RequestTooLarge"


def test_oversized_chunked_body_is_413(client):
    def chunks():
        for _ in range(MAX_BODY_BYTES // 65536 + 2):
            yield b"a" * 65536

    r = client.put("/api/prefs", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    # A small chunked body still works.
    ok = client.put(
        "/api/prefs", content=iter([b"{", b"}"]), headers={"Content-Type": "application/json"}
    )
    assert ok.status_code == 200, ok.text


@pytest.mark.parametrize("size", [0, 1000])
def test_small_bodies_pass(client, size):
    body = b'{"last_tab": "' + b"s" * size + b'"}'
    r = client.put("/api/prefs", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code in (200, 400, 422), r.text
