"""Tests for floe.core.nessie against the in-process fake Nessie (SPEC §13.2)."""

from __future__ import annotations

import logging
import socket
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from floe.core.errors import CorruptPointer, NessieAuthError, NessieUnreachable, TableNotFound
from floe.core.nessie import (
    NessieClient,
    NessieHTTPError,
    TableKey,
    container_of,
    first_component_under,
    local_path_of,
)
from tests.fakes.fake_nessie import DEFAULT_CLIENT_SECRET, TOKEN_PATH


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def fake(fake_nessie):
    fake_nessie.set_branch("main", "a" * 32)
    fake_nessie.set_branch("eg-test1", "b" * 32)
    fake_nessie.set_branch("eg-test2", "c" * 32)
    return fake_nessie


def make_client(fake, clock, **overrides):
    return NessieClient(fake.profile(**overrides), DEFAULT_CLIENT_SECRET, timeout=2, clock=clock)


def token_requests(fake):
    return fake.requests(TOKEN_PATH, method="POST")


# ----- token ---------------------------------------------------------------


def test_token_cached_across_calls(fake, clock):
    client = make_client(fake, clock)
    for _ in range(5):
        client.list_refs()
        client.head("main")
        client.clear_heads()
    assert len(token_requests(fake)) == 1
    assert all(r.has_auth for r in fake.api_requests())


def test_token_refreshed_when_less_than_60s_left(fake, clock):
    fake.state.token_expires_in = 300
    client = make_client(fake, clock)
    client.list_refs()
    clock.advance(200)  # 100 s left: still fine
    client.list_refs()
    assert len(token_requests(fake)) == 1
    clock.advance(45)  # 55 s left: refresh
    client.list_refs()
    assert len(token_requests(fake)) == 2


def test_token_request_form_fields(fake, clock):
    fake.state.scope = "api://fake/some scope/.default"
    client = make_client(fake, clock, nessie_scope="api://fake/some scope/.default")
    client.list_refs()  # would fail with 400 if scope not sent verbatim
    assert len(token_requests(fake)) == 1


def test_401_refreshes_once_then_succeeds(fake, clock):
    client = make_client(fake, clock)
    client.list_refs()
    fake.revoke_tokens()
    refs = client.list_refs()
    assert {r.name for r in refs} == {"main", "eg-test1", "eg-test2"}
    assert len(token_requests(fake)) == 2
    statuses = [r.status for r in fake.api_requests()]
    assert statuses == [200, 401, 200]


def test_persistent_401_raises_auth_error(fake, clock):
    client = make_client(fake, clock)
    client.list_refs()
    # Tokens are issued but immediately expire server-side -> every call 401s.
    fake.state.token_expires_in = 0
    fake.revoke_tokens()
    with pytest.raises(NessieAuthError) as info:
        client.list_refs()
    assert info.value.status == 401
    assert len(fake.requests("/api/v2/trees")) == 3  # initial ok + 2 failed attempts


def test_token_endpoint_failure_raises_auth_error(fake, clock):
    fake.state.token_failure_status = 400
    client = make_client(fake, clock)
    with pytest.raises(NessieAuthError) as info:
        client.list_refs()
    assert info.value.status == 400
    assert info.value.endpoint == fake.token_endpoint
    assert DEFAULT_CLIENT_SECRET not in info.value.user_message()


def test_bad_client_secret_raises_auth_error(fake, clock):
    client = NessieClient(fake.profile(), "wrong-secret-value", clock=clock)
    with pytest.raises(NessieAuthError) as info:
        client.list_refs()
    assert info.value.status == 401


def test_auth_none_sends_no_header(fake, clock):
    fake.state.auth_required = False
    client = NessieClient(fake.profile(nessie_auth="none"), None, clock=clock)
    client.list_refs()
    client.head("main")
    assert token_requests(fake) == []
    assert fake.api_requests()
    assert not any(r.has_auth for r in fake.api_requests())


# ----- heads ---------------------------------------------------------------


def test_head_ttl_honored(fake, clock):
    client = make_client(fake, clock, nessie_head_ttl_seconds=45)
    assert client.head("eg-test1") == "b" * 32
    fake.move_head("eg-test1", "d" * 32)
    clock.advance(30)
    assert client.head("eg-test1") == "b" * 32
    assert len(fake.requests("/api/v2/trees/eg-test1")) == 1
    clock.advance(20)
    assert client.head("eg-test1") == "d" * 32
    assert len(fake.requests("/api/v2/trees/eg-test1")) == 2
    assert client.catalog_status == "ok"


def test_head_keeps_last_known_on_5xx(fake, clock, caplog):
    client = make_client(fake, clock)
    assert client.catalog_status == "unknown"
    assert client.head("main") == "a" * 32
    assert client.catalog_status == "ok"

    fake.move_head("main", "e" * 32)
    fake.fail_next(1, 503, path_prefix="/api/v2/trees/main")
    clock.advance(100)
    with caplog.at_level(logging.WARNING, logger="floe.nessie"):
        assert client.head("main") == "a" * 32
    assert client.catalog_status == "stale"
    assert any(r.levelno == logging.WARNING for r in caplog.records)

    assert client.head("main") == "e" * 32
    assert client.catalog_status == "ok"


def test_catalog_status_is_per_ref_not_overwritten_by_other_refs(fake, clock):
    """A failed refresh of one ref must not be masked by a later, unrelated ref's success."""
    client = make_client(fake, clock)
    assert client.head("main") == "a" * 32
    assert client.head("eg-test1") == "b" * 32
    assert client.catalog_status == "ok"

    # eg-test1's refresh fails (5xx) and falls back to its last known head.
    fake.move_head("eg-test1", "d" * 32)
    fake.fail_next(1, 503, path_prefix="/api/v2/trees/eg-test1")
    clock.advance(100)
    assert client.head("eg-test1") == "b" * 32
    assert client.catalog_status == "stale"

    # main refreshes successfully afterwards; it must not clear eg-test1's staleness.
    fake.move_head("main", "e" * 32)
    clock.advance(100)
    assert client.head("main") == "e" * 32
    assert client.catalog_status == "stale"

    # Once eg-test1 itself refreshes successfully, the catalog is ok again.
    assert client.head("eg-test1") == "d" * 32
    assert client.catalog_status == "ok"


def test_head_5xx_without_last_known_raises(fake, clock):
    client = make_client(fake, clock)
    fake.fail_next(1, 500, path_prefix="/api/v2/trees/main")
    with pytest.raises(NessieHTTPError):
        client.head("main")


def test_head_keeps_last_known_when_unreachable(fake, clock):
    client = make_client(fake, clock)
    assert client.head("main") == "a" * 32
    client.list_refs()  # ensure token is cached before shutting down
    fake.stop()
    clock.advance(100)
    assert client.head("main") == "a" * 32
    assert client.catalog_status == "stale"
    with pytest.raises(NessieUnreachable):
        client.head("eg-test1")


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_unreachable_closed_port(fake, clock):
    port = _closed_port()
    profile = fake.profile(
        nessie_uri=f"http://127.0.0.1:{port}/api/v2", nessie_auth="none"
    )
    client = NessieClient(profile, None, timeout=2, clock=clock)
    with pytest.raises(NessieUnreachable) as info:
        client.list_refs()
    assert info.value.uri == profile.nessie_uri


def test_token_endpoint_unreachable(fake, clock):
    port = _closed_port()
    client = make_client(fake, clock, nessie_token_endpoint=f"http://127.0.0.1:{port}/oauth2/token")
    with pytest.raises(NessieUnreachable):
        client.list_refs()


# ----- tables --------------------------------------------------------------


def test_list_tables_paging_and_namespace_filter(fake, clock):
    fake.state.page_size = 3
    fake.add_namespace("eg-test1", ("silver",))
    fake.add_namespace("eg-test1", ("silver", "ns1"))
    expected = set()
    for i in range(7):
        key = ("silver", "ns1", f"t{i}")
        fake.add_table("eg-test1", key)
        expected.add(key)
    fake.add_table("eg-test1", ("gold", "g1"))
    expected.add(("gold", "g1"))

    client = make_client(fake, clock)
    tables = client.list_tables("eg-test1")
    assert {t.elements for t in tables} == expected
    assert len(tables) == len(expected)
    # 2 namespaces + 8 tables = 10 entries at 3/page -> 4 pages.
    entry_reqs = fake.requests("/api/v2/trees/eg-test1/entries")
    assert len(entry_reqs) == 4
    assert "page-token" not in entry_reqs[0].query
    assert all("page-token" in r.query for r in entry_reqs[1:])
    assert all("max-records" in r.query for r in entry_reqs)


def test_table_key_properties():
    key = TableKey(("silver", "input_ns", "tbl"))
    assert key.dotted == "silver.input_ns.tbl"
    assert key.view_name == "silver_input_ns_tbl"
    assert key.layer == "silver"
    assert TableKey.parse("gold.g1") == TableKey(("gold", "g1"))
    assert TableKey(("gold", "a.b")).path_segment() == "gold.a%1Db"


# ----- pointers ------------------------------------------------------------


def test_pointer_cached_and_cleared(fake, clock):
    loc = "abfss://eg-test1@acct.dfs.core.windows.net/silver/ns1/t1/metadata/00003-x.metadata.json"
    fake.add_table("eg-test1", ("silver", "ns1", "t1"), metadata_location=loc, snapshot_id=42)
    client = make_client(fake, clock)
    key = TableKey(("silver", "ns1", "t1"))

    ptr = client.pointer("eg-test1", key)
    assert ptr.metadata_location == loc
    assert ptr.snapshot_id == 42
    assert ptr.content_id

    new_loc = loc.replace("00003-x", "00004-y")
    fake.add_table("eg-test1", ("silver", "ns1", "t1"), metadata_location=new_loc, snapshot_id=43)
    assert client.pointer("eg-test1", key) == ptr
    assert len(fake.requests("/api/v2/trees/eg-test1/contents")) == 1

    client.clear_pointers("eg-test2")  # other ref: no effect
    assert client.pointer("eg-test1", key) == ptr
    client.clear_pointers("eg-test1")
    assert client.pointer("eg-test1", key).metadata_location == new_loc
    client.clear_pointers()
    assert client.pointer("eg-test1", key).snapshot_id == 43
    assert len(fake.requests("/api/v2/trees/eg-test1/contents")) == 3


def test_pointer_key_with_dot_in_element(fake, clock):
    fake.add_table("main", ("gold", "odd.name"), snapshot_id=7)
    client = make_client(fake, clock)
    assert client.pointer("main", TableKey(("gold", "odd.name"))).snapshot_id == 7


def test_pointer_out_of_container_is_returned_verbatim(fake, clock):
    loc = "abfss://ref-b@acct.dfs.core.windows.net/silver/ns1/t1/metadata/1.metadata.json"
    fake.add_table("eg-test1", ("silver", "ns1", "t1"), metadata_location=loc)
    client = make_client(fake, clock)
    ptr = client.pointer("eg-test1", TableKey(("silver", "ns1", "t1")))
    assert container_of(ptr.metadata_location) == "ref-b"


def test_table_not_found_not_cached(fake, clock):
    client = make_client(fake, clock)
    key = TableKey(("silver", "ns1", "missing"))
    with pytest.raises(TableNotFound) as info:
        client.pointer("eg-test1", key)
    assert info.value.key == "silver.ns1.missing"
    assert info.value.ref == "eg-test1"
    fake.add_table("eg-test1", key.elements)
    assert client.pointer("eg-test1", key).snapshot_id == 1


def test_corrupt_pointer_not_cached(fake, clock):
    key = TableKey(("gold", "g1"))
    fake.add_table("eg-test2", key.elements, snapshot_id=-1)
    client = make_client(fake, clock)
    with pytest.raises(CorruptPointer) as info:
        client.pointer("eg-test2", key)
    assert info.value.key == "gold.g1"
    with pytest.raises(CorruptPointer):
        client.pointer("eg-test2", key)
    fake.add_table("eg-test2", key.elements, snapshot_id=5)
    assert client.pointer("eg-test2", key).snapshot_id == 5


# ----- container_of --------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("abfss://ref-a@acct.dfs.core.windows.net/silver/t/metadata/1.metadata.json", "ref-a"),
        ("abfs://ref-b@acct.dfs.core.windows.net/x", "ref-b"),
        ("az://ref-a@acct.blob.core.windows.net/x", "ref-a"),
        ("az://ref-b/x/y", "ref-b"),
        ("/tmp/lake/ref-a/silver/t/metadata/1.metadata.json", None),
        ("file:///tmp/lake/ref-a/x", None),
        ("s3://bucket/x", None),
    ],
)
def test_container_of(location, expected):
    assert container_of(location) == expected


def test_container_of_local_root(tmp_path):
    root = tmp_path / "lake"
    (root / "ref-a" / "silver").mkdir(parents=True)
    inside = root / "ref-a" / "silver" / "1.metadata.json"
    assert container_of(str(inside), local_root=root) == "ref-a"
    assert container_of(inside.as_uri(), local_root=root) == "ref-a"
    assert container_of(str(tmp_path / "elsewhere" / "x"), local_root=root) is None
    assert container_of(str(root), local_root=root) is None
    assert container_of(str(inside), local_root=Path(str(root) + "/")) == "ref-a"


@pytest.mark.parametrize(
    ("location", "windows", "expected"),
    [
        # Windows forms
        ("file:///C:/lake/ref-a/x.json", True, "C:/lake/ref-a/x.json"),
        ("file:///c:/lake/a%20b/x.json", True, "c:/lake/a b/x.json"),
        ("file:/C:/lake/x", True, "C:/lake/x"),
        ("file://localhost/C:/lake/x", True, "C:/lake/x"),
        ("file://C:/lake/x", True, "C:/lake/x"),
        ("file:///C:", True, "C:/"),
        ("file://server/share/lake/x", True, "//server/share/lake/x"),
        ("C:\\lake\\ref-a\\x.json", True, "C:\\lake\\ref-a\\x.json"),
        ("C:/lake/ref-a/x.json", True, "C:/lake/ref-a/x.json"),
        # POSIX forms
        ("file:///tmp/lake/ref-a/x", False, "/tmp/lake/ref-a/x"),
        ("file://localhost/tmp/x", False, "/tmp/x"),
        ("file://otherhost/tmp/x", False, None),
        ("/tmp/lake/x", False, "/tmp/lake/x"),
        ("file:///tmp/C:/x", False, "/tmp/C:/x"),
        # Remote schemes are never local
        ("abfss://ref-a@acct.dfs.core.windows.net/x", True, None),
        ("abfss://ref-a@acct.dfs.core.windows.net/x", False, None),
        ("s3://bucket/x", False, None),
    ],
)
def test_local_path_of(location, windows, expected):
    assert local_path_of(location, windows=windows) == expected


def test_first_component_under_windows_paths():
    root = PureWindowsPath("C:\\Users\\Runner\\lake")
    W = PureWindowsPath
    assert first_component_under(W("C:/Users/Runner/lake/ref-a/s/1.json"), root) == "ref-a"
    assert first_component_under(W("c:\\users\\RUNNER\\Lake\\ref-a\\x"), root) == "ref-a"
    path = local_path_of("file:///C:/Users/Runner/lake/eg-test1/m.json", windows=True)
    assert first_component_under(W(path), root) == "eg-test1"
    assert first_component_under(W("D:/Users/Runner/lake/ref-a/x"), root) is None
    assert first_component_under(W("C:/Users/Runner/lakeside/ref-a/x"), root) is None
    assert first_component_under(root, root) is None
    # POSIX stays case-sensitive.
    proot = PurePosixPath("/tmp/lake")
    assert first_component_under(PurePosixPath("/tmp/lake/ref-a/x"), proot) == "ref-a"
    assert first_component_under(PurePosixPath("/tmp/LAKE/ref-a/x"), proot) is None


# ----- logging -------------------------------------------------------------


def test_logs_never_contain_token_or_secret(fake, clock, caplog):
    fake.add_table("eg-test1", ("silver", "ns1", "t1"),
                   metadata_location="abfss://eg-test1@acct.dfs.core.windows.net/"
                   "silver/ns1/t1/metadata/00007-z.metadata.json")
    client = make_client(fake, clock)
    with caplog.at_level(logging.DEBUG):
        client.list_refs()
        client.head("eg-test1")
        client.list_tables("eg-test1")
        client.pointer("eg-test1", TableKey(("silver", "ns1", "t1")))
        fake.revoke_tokens()
        client.list_refs()

    issued = [r for r in fake.requests(TOKEN_PATH)]
    assert len(issued) == 2
    text = caplog.text + "\n".join(str(r.args) for r in caplog.records)
    assert "fake-tok-" not in text
    assert DEFAULT_CLIENT_SECRET not in text
    assert "Bearer" not in text
    assert "00007-z.metadata.json" in text
    assert "abfss://" not in text  # filename only
    assert "GET /api/v2/trees -> 200" in text


def test_default_ssl_context_falls_back_when_default_store_is_empty(monkeypatch):
    """A frozen app's OpenSSL may point at a CA path that doesn't exist on the Mac."""
    import ssl

    import certifi

    from floe.core import nessie

    monkeypatch.setattr(nessie, "_ssl_context", None)
    monkeypatch.setattr(
        nessie.ssl, "create_default_context", lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    )
    monkeypatch.setattr(nessie, "SYSTEM_CA_BUNDLE", certifi.where())
    ctx = nessie.default_ssl_context()
    assert ctx.cert_store_stats()["x509_ca"] > 0
    assert nessie.default_ssl_context() is ctx
    monkeypatch.setattr(nessie, "_ssl_context", None)


def test_default_ssl_context_keeps_the_windows_store(monkeypatch):
    """win32: the default context (Windows store) is used as is when it has roots."""
    import ssl

    import certifi

    from floe.core import nessie

    base = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    base.load_verify_locations(cafile=certifi.where())  # stands in for the Windows store
    loaded: list[object] = []
    monkeypatch.setattr(nessie, "_ssl_context", None)
    monkeypatch.setattr(nessie.sys, "platform", "win32")
    monkeypatch.setattr(nessie.ssl, "create_default_context", lambda: base)
    monkeypatch.setattr(base, "load_verify_locations", lambda **kw: loaded.append(kw))
    assert nessie.default_ssl_context() is base
    assert loaded == []  # nothing added or replaced

    # Empty Windows store: certifi is added, never the POSIX system bundle path.
    empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    monkeypatch.setattr(nessie, "_ssl_context", None)
    monkeypatch.setattr(nessie.ssl, "create_default_context", lambda: empty)
    monkeypatch.setattr(nessie.os.path, "exists", lambda p: True)
    ctx = nessie.default_ssl_context()
    assert ctx is empty and ctx.cert_store_stats()["x509_ca"] > 0
    monkeypatch.setattr(nessie, "_ssl_context", None)
