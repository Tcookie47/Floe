"""Profiles API: CRUD, secrets never in responses, .env import, Test connection."""

from __future__ import annotations

import dataclasses

import keyring

from floe.core.connection_test import StepResult
from floe.core.profiles import SECRET_FIELDS, SERVICE_NAME, Profile
from floe.web.api_profiles import FIELD_KINDS
from tests.web.conftest import local_profile, wait_job

KEY_SECRET = "synthetic-account-key-AAAA-0123456789=="
CLIENT_SECRET = "synthetic-nessie-secret-BBBB-987654"
SP_SECRET = "synthetic-sp-secret-CCCC-55555"
ALL_SECRETS = (KEY_SECRET, CLIENT_SECRET, SP_SECRET)

REMOTE = {
    "name": "p1",
    "mode": "remote",
    "adls_account": "acctsynthetic",
    "nessie_uri": "http://nessie.invalid:19120/api",
    "nessie_token_endpoint": "https://nessie.invalid:19120/oauth2/token",
    "nessie_client_id": "client-synthetic",
    "shared_containers": ["ref-a"],
    "tenant_container_map": {"eg-test1": "eg-test1"},
}


def stored(name: str, field: str) -> str | None:
    return keyring.get_password(SERVICE_NAME, f"{name}:{field}")


def assert_no_secrets(client) -> None:
    for body in client.bodies:
        for secret in ALL_SECRETS:
            assert secret not in body


def test_field_kinds_cover_profile_fields():
    assert set(FIELD_KINDS) == {f.name for f in dataclasses.fields(Profile)}


def test_profile_crud_and_secrets_never_returned(client):
    r = client.post(
        "/api/profiles",
        json={
            "profile": REMOTE,
            "secrets": {"adls_account_key": KEY_SECRET, "nessie_client_secret": CLIENT_SECRET},
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["profile"]["adls_account"] == "acctsynthetic"
    assert body["secrets"] == {
        "adls_account_key": {"saved": True},
        "adls_client_secret": {"saved": False},
        "nessie_client_secret": {"saved": True},
    }
    assert stored("p1", "adls_account_key") == KEY_SECRET
    assert stored("p1", "nessie_client_secret") == CLIENT_SECRET

    assert client.post("/api/profiles", json={"profile": REMOTE}).status_code == 409
    assert [p["name"] for p in client.get("/api/profiles").json()["profiles"]] == ["p1"]
    got = client.get("/api/profiles/p1").json()
    assert got["secrets"]["adls_account_key"] == {"saved": True}
    assert set(got["secret_env_vars"]) == set(SECRET_FIELDS)

    # Update: omitted secret unchanged, "" clears, a value sets.
    r = client.put(
        "/api/profiles/p1",
        json={
            "profile": {**REMOTE, "allow_export": True},
            "secrets": {"nessie_client_secret": "", "adls_client_secret": SP_SECRET},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["profile"]["allow_export"] is True
    assert stored("p1", "adls_account_key") == KEY_SECRET
    assert stored("p1", "nessie_client_secret") is None
    assert stored("p1", "adls_client_secret") == SP_SECRET
    # Renaming through PUT is refused.
    renamed = {"profile": {**REMOTE, "name": "x"}}
    assert client.put("/api/profiles/p1", json=renamed).status_code == 400

    r = client.post("/api/profiles/p1/duplicate", json={"new_name": "p2"})
    assert r.status_code == 201
    assert stored("p2", "adls_account_key") == KEY_SECRET
    r = client.post("/api/profiles/p2/rename", json={"new_name": "p3"})
    assert r.status_code == 200 and r.json()["profile"]["name"] == "p3"
    assert stored("p2", "adls_account_key") is None
    assert stored("p3", "adls_account_key") == KEY_SECRET
    assert client.post("/api/profiles/p3/rename", json={"new_name": "p1"}).status_code == 409

    assert client.delete("/api/profiles/p3").status_code == 200
    assert stored("p3", "adls_account_key") is None
    assert client.get("/api/profiles/p3").status_code == 404
    assert [p["name"] for p in client.get("/api/profiles").json()["profiles"]] == ["p1"]
    assert_no_secrets(client)


def test_profile_validation(client):
    def post(profile, **extra):
        return client.post("/api/profiles", json={"profile": profile, **extra})

    assert post({**REMOTE, "bogus": 1}).status_code == 400
    assert post({**REMOTE, "adls_account_key": KEY_SECRET}).status_code == 400
    assert post({**REMOTE, "mode": "cloud"}).status_code == 400
    assert post({**REMOTE, "preview_row_limit": "10"}).status_code == 400
    assert post({**REMOTE, "preview_row_limit": 0}).status_code == 400
    assert post({**REMOTE, "allow_export": "yes"}).status_code == 400
    assert post({**REMOTE, "shared_containers": "ref-a"}).status_code == 400
    assert post({**REMOTE, "name": ""}).status_code == 400
    assert post({**REMOTE, "name": "a:b"}).status_code == 400
    assert post({"name": "l", "mode": "local"}).status_code == 400  # needs a fixture dir
    assert post(REMOTE, secrets={"bogus": "x"}).status_code == 400
    # Validation errors never echo the submitted values.
    r = client.post("/api/profiles", json=[KEY_SECRET])
    assert r.status_code == 422
    r = client.post("/api/profiles", json={"profile": REMOTE, "secrets": {"adls_account_key": 5}})
    assert r.status_code == 400
    assert client.get("/api/profiles").json()["profiles"] == []
    assert_no_secrets(client)


ENV_TEXT = f"""
# synthetic
ADLS_ACCOUNT=acctsynthetic
ADLS_ACCOUNT_KEY="{KEY_SECRET}"
NESSIE_URI=http://nessie.invalid:19120/api
NESSIE_CLIENT_ID=client-synthetic
NESSIE_CLIENT_SECRET={CLIENT_SECRET}
NESSIE_KEY_VAULT=kv-synthetic
"""


def test_import_env_keeps_secrets_server_side(client):
    r = client.post("/api/profiles/import-env", json={"text": ENV_TEXT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fields"]["adls_account"] == "acctsynthetic"
    assert body["fields"]["nessie_uri"] == "http://nessie.invalid:19120/api"
    assert body["secrets_present"] == ["adls_account_key", "nessie_client_secret"]
    assert body["notes"] and "Key Vault" in body["notes"][0]
    import_id = body["import_id"]
    assert import_id

    profile = {**body["fields"], "name": "from-env"}
    r = client.post(
        "/api/profiles",
        json={"profile": profile, "import_id": import_id, "secrets": {"adls_account_key": None}},
    )
    assert r.status_code == 201, r.text
    assert stored("from-env", "adls_account_key") == KEY_SECRET
    assert stored("from-env", "nessie_client_secret") == CLIENT_SECRET
    # The import is consumed by the save.
    r = client.put("/api/profiles/from-env", json={"profile": profile, "import_id": import_id})
    assert r.status_code == 410

    # text/plain upload works too; an explicit secret beats the imported one.
    r = client.post(
        "/api/profiles/import-env",
        content=ENV_TEXT.encode(),
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code == 200
    r = client.post(
        "/api/profiles",
        json={
            "profile": {**profile, "name": "env2"},
            "import_id": r.json()["import_id"],
            "secrets": {"nessie_client_secret": SP_SECRET},
        },
    )
    assert r.status_code == 201
    assert stored("env2", "nessie_client_secret") == SP_SECRET
    assert stored("env2", "adls_account_key") == KEY_SECRET
    assert client.post(
        "/api/profiles/import-env", content=b"x", headers={"Content-Type": "image/png"}
    ).status_code == 415
    assert_no_secrets(client)


def test_test_connection_local(local_client, fx):
    r = local_client.post("/api/profiles/local-p/test")
    assert r.status_code == 202
    job = wait_job(local_client, r.json()["id"])
    assert job["status"] == "done"
    assert job["result"]["ok"] is True
    assert job["steps"] and job["steps"][0]["ok"] is True
    # Also through /api/jobs, and for unsaved form values ("profile" is the name there).
    r = local_client.post("/api/jobs", json={"kind": "test_connection", "profile": "unsaved"})
    assert r.status_code == 404  # no saved profile and no form
    form = local_profile(fx, name="unsaved", local_fixture_dir=str(fx.root / "missing"))
    r = local_client.post(
        "/api/jobs",
        json={"kind": "test_connection", "profile": "unsaved", "profile_form": form.to_dict()},
    )
    job = wait_job(local_client, r.json()["id"])
    assert job["status"] == "done" and job["result"]["ok"] is False


def test_test_connection_uses_form_import_and_saved_secrets(make_client, store):
    seen: list[dict] = []

    def tester(profile, secrets):
        seen.append({"profile": profile, "secrets": dict(secrets)})
        yield StepResult("Nessie host reachable", True, "ok", 1.0)
        yield StepResult("Token", False, f"failed with {secrets.get('nessie_client_secret')}", 2.0)

    client = make_client(connection_tester=tester)
    client.post(
        "/api/profiles", json={"profile": REMOTE, "secrets": {"adls_account_key": KEY_SECRET}}
    )
    import_id = client.post("/api/profiles/import-env", json={"text": ENV_TEXT}).json()["import_id"]
    r = client.post(
        "/api/profiles/p1/test",
        json={
            "profile": {**REMOTE, "adls_account": "changed"},
            "import_id": import_id,
            "secrets": {"adls_client_secret": SP_SECRET},
        },
    )
    job = wait_job(client, r.json()["id"])
    assert job["status"] == "done"
    assert job["result"]["ok"] is False
    assert [s["name"] for s in job["steps"]] == ["Nessie host reachable", "Token"]
    assert "***" in job["steps"][1]["message"]  # step text is redacted
    call = seen[-1]
    assert call["profile"].adls_account == "changed"
    assert call["secrets"] == {
        "adls_account_key": KEY_SECRET,  # imported (overrides nothing explicit)
        "adls_client_secret": SP_SECRET,  # explicit
        "nessie_client_secret": CLIENT_SECRET,  # imported
    }
    # Saved profile only: keyring values.
    job = wait_job(client, client.post("/api/profiles/p1/test").json()["id"])
    assert seen[-1]["secrets"]["adls_account_key"] == KEY_SECRET
    assert seen[-1]["secrets"]["nessie_client_secret"] is None
    assert_no_secrets(client)


def _recording_tester(seen):
    def tester(profile, secrets):
        seen.append(dict(secrets))
        yield StepResult("Nessie host reachable", True, "ok", 1.0)

    return tester


def test_saved_secrets_never_go_to_a_changed_address(make_client):
    seen: list[dict] = []
    client = make_client(connection_tester=_recording_tester(seen))
    r = client.post(
        "/api/profiles",
        json={"profile": REMOTE,
              "secrets": {"adls_account_key": KEY_SECRET, "nessie_client_secret": CLIENT_SECRET}},
    )
    assert r.status_code == 201, r.text

    def test(form, secrets=None):
        body = {"profile": form}
        if secrets is not None:
            body["secrets"] = secrets
        return client.post("/api/profiles/p1/test", json=body)

    # Unchanged addresses: the saved secrets are used.
    wait_job(client, test(dict(REMOTE)).json()["id"])
    assert seen[-1] == {"adls_account_key": KEY_SECRET, "adls_client_secret": None,
                        "nessie_client_secret": CLIENT_SECRET}
    # A changed Nessie URI / token endpoint / ADLS account without the secret → 400
    # naming the field(s); the tester never runs.
    count = len(seen)
    for changed, secret in (
        ({"nessie_uri": "https://attacker.invalid/api"}, "nessie_client_secret"),
        ({"nessie_token_endpoint": "https://attacker.invalid/token"}, "nessie_client_secret"),
        ({"adls_account": "attackeracct"}, "adls_account_key"),
    ):
        r = test({**REMOTE, **changed})
        assert r.status_code == 400, changed
        error = r.json()["error"]
        assert error["type"] == "ValidationError"
        assert secret in error["message"] and next(iter(changed)) in error["message"]
        assert error["fields"] == [secret]
    assert len(seen) == count
    # Supplying the secret for the changed address works (and only it is replaced).
    r = test({**REMOTE, "nessie_uri": "https://other.invalid/api"},
             {"nessie_client_secret": "synthetic-typed-secret-777"})
    wait_job(client, r.json()["id"])
    assert seen[-1] == {"adls_account_key": KEY_SECRET, "adls_client_secret": None,
                        "nessie_client_secret": "synthetic-typed-secret-777"}
    # Changed Nessie URI with auth "none": allowed, but the saved client secret stays home.
    r = test({**REMOTE, "nessie_uri": "https://other.invalid/api", "nessie_auth": "none"})
    wait_job(client, r.json()["id"])
    assert seen[-1]["nessie_client_secret"] is None
    assert seen[-1]["adls_account_key"] == KEY_SECRET
    # An unsaved name never picks up stray keyring entries.
    keyring.set_password(SERVICE_NAME, "ghost:adls_account_key", "synthetic-stray-key-999")
    r = client.post("/api/jobs", json={"kind": "test_connection", "profile": "ghost",
                                       "profile_form": {**REMOTE, "name": "ghost"}})
    wait_job(client, r.json()["id"])
    assert seen[-1]["adls_account_key"] is None
    assert_no_secrets(client)


def test_token_endpoint_needs_https_unless_loopback(client):
    for url in ("http://nessie.invalid/token", "ftp://nessie.invalid/token",
                "http://localhost.nessie.invalid/token"):
        r = client.post("/api/profiles", json={"profile": {**REMOTE, "nessie_token_endpoint": url}})
        assert r.status_code == 400, url
        assert "nessie_token_endpoint" in r.json()["error"]["message"]
    for i, url in enumerate(("http://127.0.0.1:9/token", "http://localhost:9/token",
                             "http://[::1]:9/token", "https://nessie.invalid/token")):
        profile = {**REMOTE, "name": f"ok{i}", "nessie_token_endpoint": url}
        assert client.post("/api/profiles", json={"profile": profile}).status_code == 201, url
    # Not checked when OAuth isn't used; the Nessie URI itself may be http.
    profile = {**REMOTE, "name": "noauth", "nessie_auth": "none",
               "nessie_token_endpoint": "http://nessie.invalid/token"}
    assert client.post("/api/profiles", json={"profile": profile}).status_code == 201


def test_saving_a_profile_drops_its_session(local_client, fx):
    state = local_client.app.state.floe
    assert local_client.get("/api/profiles/local-p/branches").status_code == 200
    assert state.sessions.peek("local-p") is not None
    profile = local_profile(fx).to_dict()
    r = local_client.put("/api/profiles/local-p", json={"profile": profile})
    assert r.status_code == 200
    assert state.sessions.peek("local-p") is None


def _form(**overrides):
    """A full payload shaped like the browser form sends: every field, hidden ones too."""
    return {**Profile(name="").to_dict(), **overrides}


def test_form_shaped_profiles_save_without_spurious_errors(client, tmp_path):
    # Local mode: hidden remote fields (incl. a stale http token endpoint) are ignored.
    local = _form(name="eg-local", mode="local", local_fixture_dir=str(tmp_path),
                  nessie_token_endpoint="http://nessie.invalid/token")
    r = client.post("/api/profiles", json={"profile": local, "secrets": {}})
    assert r.status_code == 201, r.text
    # Remote, Nessie auth none, empty token endpoint.
    none = _form(name="eg-none", adls_account="acctsynthetic",
                 nessie_uri="http://nessie.invalid:19120/api", nessie_auth="none")
    r = client.post("/api/profiles", json={"profile": none, "secrets": {}})
    assert r.status_code == 201, r.text
    # Remote OAuth with an https token endpoint + secrets.
    oauth = _form(name="eg-oauth", adls_account="acctsynthetic",
                  nessie_uri="https://nessie.invalid/api",
                  nessie_token_endpoint="https://nessie.invalid/oauth2/token",
                  nessie_client_id="client-synthetic")
    r = client.post("/api/profiles", json={
        "profile": oauth,
        "secrets": {"adls_account_key": KEY_SECRET, "nessie_client_secret": CLIENT_SECRET},
    })
    assert r.status_code == 201, r.text
    assert r.json()["secrets"]["nessie_client_secret"] == {"saved": True}
    assert client.put("/api/profiles/eg-oauth", json={"profile": oauth}).status_code == 200
    # Remote OAuth with a plain-http, non-loopback token endpoint → a clear field error.
    bad = {**oauth, "name": "eg-bad", "nessie_token_endpoint": "http://nessie.invalid/token"}
    r = client.post("/api/profiles", json={"profile": bad})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["type"] == "ValidationError"
    assert err.get("field") == "nessie_token_endpoint"
    assert "https://" in err["message"]
    assert_no_secrets(client)


# --------------------------------------------------------------------------- export / import


def _save_remote(client):
    r = client.post(
        "/api/profiles",
        json={
            "profile": {**REMOTE, "name": "we\"ird; name"},
            "secrets": {"adls_account_key": KEY_SECRET, "nessie_client_secret": CLIENT_SECRET},
        },
    )
    assert r.status_code == 201, r.text


def test_export_profile_headers_and_no_secrets(client):
    _save_remote(client)
    r = client.get("/api/profiles/we%22ird%3B%20name/export")
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    disposition = r.headers["content-disposition"]
    assert disposition == 'attachment; filename="we_ird_name.floe-profile.json"'
    body = r.json()
    assert body["format"] == "floe-profile" and body["profile"]["adls_account"] == "acctsynthetic"
    for secret in ALL_SECRETS:
        assert secret not in r.text
    for name in SECRET_FIELDS:
        assert name not in r.text
    assert client.get("/api/profiles/nope/export").status_code == 404


def test_export_and_import_need_the_api_key(client):
    _save_remote(client)
    client.headers.pop("X-Floe-Auth")
    assert client.get("/api/profiles/we%22ird%3B%20name/export").status_code == 401
    assert client.post("/api/profiles/import-profile", json={"text": "{}"}).status_code == 401


def test_import_profile_does_not_persist(client, store):
    _save_remote(client)
    text = client.get("/api/profiles/we%22ird%3B%20name/export").text
    before = [p.name for p in store.list()]
    r = client.post("/api/profiles/import-profile", json={"text": text})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fields"]["name"] == "we\"ird; name"
    assert body["missing_secrets"] == ["adls_account_key", "nessie_client_secret"]
    assert [p.name for p in store.list()] == before
    assert client.get("/api/profiles").json()["profiles"] == [
        p.to_dict() for p in store.list()
    ]


def test_import_profile_rejects_bad_files(client):
    r = client.post("/api/profiles/import-profile", json={"text": "not json"})
    assert r.status_code == 400 and r.json()["error"]["type"] == "ValidationError"
    assert client.post("/api/profiles/import-profile", json={}).status_code == 400
