"""Profiles API (SPEC §4, §8.2, §8.3, §15.2).

Secret values never leave the server: responses carry only `{"saved": bool}` per secret
field. In a save / test body, a secret that is omitted (or null) is left unchanged, ""
clears it, and any other string sets it.

`.env` import: `POST /api/profiles/import-env` takes the file *contents* (JSON
`{"text": ...}` or a `text/plain` body — the browser reads the chosen file with
FileReader) and returns the non-secret form values, notes, the names of the secret
fields that were present, and an `import_id`. The parsed secret values stay in server
memory under that id (10 minutes); send `"import_id"` with the following save or test
and every secret field the body doesn't set explicitly is taken from the import.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import logging
import re
import urllib.parse
from typing import Any

from fastapi import APIRouter, Request, Response

from floe.core import diagnostics
from floe.core.profiles import (
    SECRET_FIELDS,
    Profile,
    ProfileImportError,
    export_profile,
    keyring_usable,
    parse_env_text,
    parse_profile_export,
    required_secrets,
    secret_env_var_name,
)
from floe.web.state import ApiError, JsonBody, OptionalJson, State, WebState

log = logging.getLogger("floe.web.profiles")

router = APIRouter(prefix="/api/profiles")

MAX_NAME = 100
MAX_TEXT = 4096
MAX_ENV_BYTES = 256 * 1024

# Validation kind per Profile field (kept in sync with the dataclass by a test).
FIELD_KINDS: dict[str, Any] = {
    "name": "name",
    "mode": ("remote", "local"),
    "local_fixture_dir": "opt_str",
    "adls_account": "str",
    "adls_auth": ("account_key", "service_principal"),
    "adls_tenant_id": "str",
    "adls_client_id": "str",
    "adls_ca_cert_file": "opt_str",
    "nessie_uri": "str",
    "nessie_auth": ("oauth2", "none"),
    "nessie_token_endpoint": "str",
    "nessie_client_id": "str",
    "nessie_scope": "str",
    "nessie_main_ref": "str",
    "nessie_head_ttl_seconds": "int",
    "shared_containers": "str_list",
    "shared_namespaces": "str_list",
    "tenant_registry_table": "opt_str",
    "tenant_container_map": "str_map",
    "tenant_data_source_map": "str_map",
    "duckdb_memory_limit": "opt_str",
    "duckdb_threads": "opt_int",
    "conn_cache_max": "pos_int",
    "allow_export": "bool",
    "preview_row_limit": "pos_int",
    "restrict_file_access": "bool",
}


# Upper bounds for some integer fields (a preview holds its rows in server memory).
INT_MAXIMA = {"preview_row_limit": 1_000_000}

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback_url(url: str) -> bool:
    """True when `url`'s host is this machine (localhost, 127.0.0.0/8 or ::1)."""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_https_unless_loopback(field: str, url: str) -> None:
    """Secrets are sent to these URLs: plain http only to this machine."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme == "https":
        return
    if scheme == "http" and is_loopback_url(url):
        return
    raise ApiError(
        400,
        "ValidationError",
        f"{field} must start with https:// (plain http:// is allowed only for "
        "localhost / 127.0.0.1 / ::1).",
        field=field,
    )


def _bad(message: str) -> ApiError:
    return ApiError(400, "ValidationError", message)


def check_name(value: object, what: str = "Profile name") -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{what} is required.")
    value = value.strip()
    if len(value) > MAX_NAME:
        raise _bad(f"{what} is too long (max {MAX_NAME} characters).")
    if any(ord(c) < 32 or c in "\x7f:" for c in value):
        raise _bad(f"{what} may not contain control characters or ':'.")
    return value


def _check_str(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise _bad(f"{name} must be a string (max {MAX_TEXT} characters).")
    return value.strip()


def _check_int(name: str, value: object, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _bad(f"{name} must be an integer ≥ {minimum}.")
    return value


def validate_profile(data: object) -> Profile:
    """A `Profile` from untrusted JSON: known fields only, each type-checked."""
    if not isinstance(data, dict):
        raise _bad("profile must be an object.")
    secret_keys = set(data) & set(SECRET_FIELDS)
    if secret_keys:
        raise _bad("Secrets go in the separate 'secrets' object, not in the profile.")
    unknown = set(data) - set(FIELD_KINDS)
    if unknown:
        raise _bad(f"Unknown profile field(s): {', '.join(sorted(map(str, unknown)))}.")
    clean: dict[str, Any] = {}
    for key, value in data.items():
        kind = FIELD_KINDS[key]
        if kind == "name":
            clean[key] = check_name(value)
        elif isinstance(kind, tuple):
            if value not in kind:
                raise _bad(f"{key} must be one of {', '.join(kind)}.")
            clean[key] = value
        elif kind == "str":
            clean[key] = _check_str(key, value)
        elif kind == "opt_str":
            clean[key] = None if value is None else (_check_str(key, value) or None)
        elif kind == "int":
            clean[key] = _check_int(key, value, 0)
        elif kind == "pos_int":
            clean[key] = _check_int(key, value, 1)
            if key in INT_MAXIMA and clean[key] > INT_MAXIMA[key]:
                raise _bad(f"{key} must be at most {INT_MAXIMA[key]:,}.")
        elif kind == "opt_int":
            clean[key] = None if value is None else _check_int(key, value, 1)
        elif kind == "bool":
            if not isinstance(value, bool):
                raise _bad(f"{key} must be true or false.")
            clean[key] = value
        elif kind == "str_list":
            if not isinstance(value, list):
                raise _bad(f"{key} must be a list of strings.")
            clean[key] = [s for s in (_check_str(key, v) for v in value) if s]
        elif kind == "str_map":
            if not isinstance(value, dict):
                raise _bad(f"{key} must be an object of strings.")
            clean[key] = {
                _check_str(key, k): _check_str(key, v) for k, v in value.items() if k.strip()
            }
    if "name" not in clean:
        raise _bad("Profile name is required.")
    profile = Profile.from_dict(clean)
    if profile.mode == "local" and not profile.local_fixture_dir:
        raise _bad("Local mode needs a local fixture directory.")
    remote_oauth = profile.mode != "local" and profile.nessie_auth == "oauth2"
    if remote_oauth and profile.nessie_token_endpoint:
        require_https_unless_loopback("nessie_token_endpoint", profile.nessie_token_endpoint)
    return profile


def _secrets_arg(data: object) -> dict[str, str | None]:
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _bad("secrets must be an object.")
    out: dict[str, str | None] = {}
    for key, value in data.items():
        if key not in SECRET_FIELDS:
            raise _bad(f"Unknown secret field: {key}.")
        if value is not None and (not isinstance(value, str) or len(value) > MAX_TEXT):
            raise _bad(f"{key} must be a string or null.")
        diagnostics.register_secret(value)
        out[key] = value
    return out


def _with_import(
    state: WebState, secrets: dict[str, str | None], import_id: object
) -> dict[str, str | None]:
    if import_id is None:
        return secrets
    if not isinstance(import_id, str):
        raise _bad("import_id must be a string.")
    imported = state.imports.get(import_id)
    if imported is None:
        raise ApiError(
            410, "ImportExpired", "The .env import expired; import the file again."
        )
    merged = dict(secrets)
    for key, value in imported.items():
        if merged.get(key) is None:
            merged[key] = value
    return merged


def profile_payload(state: WebState, profile: Profile) -> dict[str, Any]:
    """Non-secret fields plus `{"saved": bool}` per secret — never a secret value."""
    return {
        "profile": profile.to_dict(),
        "secrets": {
            f: {"saved": state.store.has_secret(profile.name, f)} for f in SECRET_FIELDS
        },
        "keyring_available": keyring_usable(),
        "secret_env_vars": {f: secret_env_var_name(profile.name, f) for f in SECRET_FIELDS},
    }


def _get_profile(state: WebState, name: str) -> Profile:
    profile = state.store.get(name)
    if profile is None:
        raise ApiError(404, "ProfileNotFound", f"No such profile: {name}")
    return profile


def _forget(state: WebState, name: str) -> None:
    """Drop the session and in-memory jobs/results of a changed profile."""
    state.sessions.drop(name)
    state.jobs.forget_profile(name)


def _save(state: WebState, profile: Profile, payload: dict[str, Any]) -> None:
    secrets = _with_import(state, _secrets_arg(payload.get("secrets")), payload.get("import_id"))
    _forget(state, profile.name)
    state.store.save(profile, secrets=secrets)
    if isinstance(payload.get("import_id"), str):
        state.imports.discard(payload["import_id"])
    log.info("Profile saved (mode=%s)", profile.mode)


@router.get("")
def list_profiles(state: State) -> dict[str, Any]:
    profiles = sorted(state.store.list(), key=lambda p: p.name.lower())
    return {
        "profiles": [p.to_dict() for p in profiles],
        "keyring_available": keyring_usable(),
    }


@router.post("", status_code=201)
def create_profile(
    payload: JsonBody, state: State
) -> dict[str, Any]:
    profile = validate_profile(payload.get("profile"))
    if state.store.get(profile.name) is not None:
        raise ApiError(409, "ProfileExists", f"Profile already exists: {profile.name}")
    _save(state, profile, payload)
    return profile_payload(state, profile)


@router.post("/import-env")
async def import_env(request: Request, state: State) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_ENV_BYTES:
        raise _bad("The .env file is too large.")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _bad("Invalid JSON body.") from None
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str):
            raise _bad("Send the .env contents as {\"text\": ...}.")
    elif content_type in ("text/plain", "application/octet-stream", ""):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise _bad("The .env file must be UTF-8 text.") from None
    else:
        raise ApiError(415, "UnsupportedMediaType", "Send JSON {text} or a text/plain body.")
    try:
        result = parse_env_text(text)
    except ValueError:
        raise _bad("The .env file has an invalid number in it.") from None
    import_id = state.imports.put(result.secrets) if result.secrets else None
    return {
        "fields": result.fields,
        "notes": result.notes,
        "secrets_present": sorted(result.secrets),
        "import_id": import_id,
    }


@router.post("/import-profile")
def import_profile(payload: JsonBody) -> dict[str, Any]:
    """Parse a profile file's contents (`{"text": ...}`). Saves nothing."""
    text = payload.get("text")
    if not isinstance(text, str):
        raise _bad('Send the profile file contents as {"text": ...}.')
    try:
        result = parse_profile_export(text)
    except ProfileImportError as exc:
        raise _bad("; ".join(exc.errors)) from None
    return {
        "fields": result.fields,
        "notes": result.notes,
        "missing_secrets": result.missing_secrets,
    }


def export_filename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")[:80] or "profile"
    return f"{safe}.floe-profile.json"


@router.get("/{name}/export")
def export_profile_file(name: str, state: State) -> Response:
    profile = _get_profile(state, name)
    body = json.dumps(export_profile(profile), indent=2, sort_keys=False) + "\n"
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{export_filename(name)}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/{name}")
def get_profile(name: str, state: State) -> dict[str, Any]:
    return profile_payload(state, _get_profile(state, name))


@router.put("/{name}")
def update_profile(
    name: str, payload: JsonBody, state: State
) -> dict[str, Any]:
    _get_profile(state, name)
    data = payload.get("profile")
    if isinstance(data, dict) and "name" not in data:
        data = {**data, "name": name}
    profile = validate_profile(data)
    if profile.name != name:
        raise _bad("To rename a profile use POST /api/profiles/{name}/rename.")
    _save(state, profile, payload)
    return profile_payload(state, profile)


@router.delete("/{name}")
def delete_profile(name: str, state: State) -> dict[str, Any]:
    _get_profile(state, name)
    _forget(state, name)
    state.store.delete(name)
    state.history.clear(name)
    state.prefs.forget_profile(name)
    return {"deleted": name}


@router.post("/{name}/rename")
def rename_profile(
    name: str, payload: JsonBody, state: State
) -> dict[str, Any]:
    _get_profile(state, name)
    new = check_name(payload.get("new_name"), "New name")
    if new == name:
        return profile_payload(state, _get_profile(state, name))
    if state.store.get(new) is not None:
        raise ApiError(409, "ProfileExists", f"Profile already exists: {new}")
    _forget(state, name)
    state.store.rename(name, new)
    state.prefs.forget_profile(name, new_name=new)
    return profile_payload(state, _get_profile(state, new))


@router.post("/{name}/duplicate", status_code=201)
def duplicate_profile(
    name: str, payload: JsonBody, state: State
) -> dict[str, Any]:
    _get_profile(state, name)
    new = check_name(payload.get("new_name"), "New name")
    if state.store.get(new) is not None:
        raise ApiError(409, "ProfileExists", f"Profile already exists: {new}")
    return profile_payload(state, state.store.duplicate(name, new))


# --------------------------------------------------------------------------- test connection

# A saved secret is only ever sent where the saved profile sends it: if the form points
# one of these at a different address, the secrets that go there must be typed again.
_SECRET_DESTINATIONS = {
    "nessie_client_secret": ("nessie_uri", "nessie_token_endpoint"),
    "adls_account_key": ("adls_account",),
    "adls_client_secret": ("adls_account",),
}


def _needed_secrets(profile: Profile) -> set[str]:
    return set(required_secrets(profile))


def _reusable_saved_secrets(
    saved: Profile | None, form: Profile, given: dict[str, str | None]
) -> set[str]:
    """The secret fields whose saved (keyring) values may be used to test `form`.
    Raises a validation error naming the fields when a needed secret would otherwise be
    sent to a changed address."""
    if saved is None:
        return set()  # nothing saved under this name: never reuse stray keyring entries
    reusable: set[str] = set()
    changed: set[str] = set()
    missing: list[str] = []
    needed = _needed_secrets(form)
    for secret, destinations in _SECRET_DESTINATIONS.items():
        moved = [
            f for f in destinations
            if str(getattr(saved, f) or "").strip() != str(getattr(form, f) or "").strip()
        ]
        if not moved:
            reusable.add(secret)
            continue
        changed.update(moved)
        if secret in needed and not given.get(secret):
            missing.append(secret)
    if missing:
        raise ApiError(
            400,
            "ValidationError",
            f"{', '.join(sorted(changed))} changed from the saved profile, so the saved "
            f"secret(s) won't be sent there. Enter {', '.join(missing)} to test.",
            fields=missing,
        )
    return reusable


def start_test_connection(
    state: WebState, name: str, payload: dict[str, Any], form_key: str = "profile"
):
    """Submit a Test-connection job. With a form object in `payload[form_key]`, the
    (possibly unsaved) form values are tested; otherwise the saved profile `name`. Secrets: explicit
    values from `secrets`, then the `import_id` values, then the saved keyring values —
    the latter only while the form still sends them to the saved profile's addresses
    (`_SECRET_DESTINATIONS`)."""
    form = payload.get(form_key)
    given = _with_import(state, _secrets_arg(payload.get("secrets")), payload.get("import_id"))
    if form is not None:
        if isinstance(form, dict) and "name" not in form:
            form = {**form, "name": name}
        profile = validate_profile(form)
        reusable = _reusable_saved_secrets(state.store.get(name), profile, given)
    else:
        profile = _get_profile(state, name)
        reusable = set(SECRET_FIELDS)
    tester = state.connection_tester
    store = state.store

    def run(progress, is_cancelled) -> dict[str, Any]:
        secrets: dict[str, str | None] = {}
        if profile.mode != "local":
            for field_name in SECRET_FIELDS:
                value = given.get(field_name)
                if value is None and field_name in reusable:
                    value = store.get_secret(name, field_name)
                secrets[field_name] = value or None
        steps: list[dict[str, Any]] = []
        for step in tester(profile, secrets):
            item = dataclasses.asdict(step)
            item["message"] = diagnostics.redact(str(item["message"]))
            steps.append(item)
            progress(item)
            if is_cancelled():
                break
        return {"ok": bool(steps) and all(s["ok"] for s in steps), "steps": steps}

    return state.jobs.submit(
        "test_connection",
        run,
        profile=name,
        channel=str(payload.get("channel") or "test_connection"),
        meta={},
    )


@router.post("/{name}/test", status_code=202)
def test_connection(
    state: State,
    name: str,
    payload: OptionalJson = None,
) -> dict[str, Any]:
    job = start_test_connection(state, check_name(name), payload or {})
    return {"id": job.id, "status": job.status, "kind": job.kind}
