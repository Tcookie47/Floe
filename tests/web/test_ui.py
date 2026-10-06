"""The browser UI's server side (SPEC §15.1, §15.3): the page renders CSP-compatible
markup, every referenced static file (and every ES module it imports) is served with the
right content type, the vendored CodeMirror files and licences are present, and a wheel
contains the templates and static files."""

from __future__ import annotations

import html.parser
import importlib.util
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from floe.core.profiles import SECRET_FIELDS, Profile
from floe.web import api_profiles
from floe.web.app import STATIC_DIR, TEMPLATES_DIR
from floe.web.security import CSP

ROOT = Path(__file__).resolve().parents[2]
VENDOR = STATIC_DIR / "vendor" / "codemirror"
OWN_JS = sorted(p for p in STATIC_DIR.rglob("*.js") if "vendor" not in p.parts)
JS_TYPES = ("text/javascript", "application/javascript")
IMPORT_RE = re.compile(
    r"""(?:^|;|\n)\s*(?:import|export)\b[^'";]*?\bfrom\s*['"]([^'"]+)['"]"""
    r"""|\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.M,
)


class _Page(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.script_bodies: list[str] = []
        self.style_tags = 0
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._in_script = True
            self.script_bodies.append("")
        if tag == "style":
            self.style_tags += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.script_bodies[-1] += data


def _page(client) -> _Page:
    r = client.get("/")
    assert r.status_code == 200, r.text
    assert r.headers["content-security-policy"] == CSP
    parser = _Page()
    parser.feed(r.text)
    return parser


def test_index_markup_is_csp_compatible(client):
    page = _page(client)
    assert page.style_tags == 0, "no <style> elements (CSP has no 'unsafe-inline')"
    assert all(body.strip() == "" for body in page.script_bodies), "no inline script bodies"
    scripts = [a for t, a in page.tags if t == "script"]
    assert scripts and all(a.get("src", "").startswith("/static/") for a in scripts)
    for tag, attrs in page.tags:
        assert "style" not in attrs, f"style= attribute on <{tag}>"
        handlers = [k for k in attrs if k.lower().startswith("on")]
        assert not handlers, f"inline handler {handlers} on <{tag}>"
        for key in ("href", "src", "action"):
            value = attrs.get(key)
            if value:
                assert not value.lower().startswith(("http:", "https:", "//", "javascript:"))


def test_index_template_has_no_inline_code_on_disk():
    text = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\sstyle\s*=", text)
    assert not re.search(r"\son[a-z]+\s*=", text, re.I)
    assert "<style" not in text


def _served(client, url: str) -> str:
    r = client.get(url)
    assert r.status_code == 200, url
    assert r.headers["content-security-policy"] == CSP, url
    return r.headers["content-type"]


def test_all_referenced_static_files_are_served(client):
    page = _page(client)
    refs = [
        a.get("href") or a.get("src")
        for t, a in page.tags
        if t in ("link", "script") and (a.get("href") or a.get("src"))
    ]
    assert "/static/floe.css" in refs and "/static/app.js" in refs
    seen: set[str] = set()
    todo = [ref for ref in refs if ref.startswith("/static/")]
    while todo:
        url = todo.pop()
        if url in seen:
            continue
        seen.add(url)
        path = STATIC_DIR / url.removeprefix("/static/")
        assert path.is_file(), f"{url} is referenced but missing"
        ctype = _served(client, url)
        if url.endswith(".js"):
            assert ctype.split(";")[0] in JS_TYPES, (url, ctype)
            source = path.read_text(encoding="utf-8")
            for m in IMPORT_RE.finditer(source):
                spec = m.group(1) or m.group(2)
                assert spec.startswith("."), f"{url}: bare import {spec!r} can't load"
                base = url.rsplit("/", 1)[0]
                resolved = (Path(base) / spec).as_posix()
                parts: list[str] = []
                for part in resolved.split("/"):
                    if part == "..":
                        parts.pop()
                    elif part and part != ".":
                        parts.append(part)
                todo.append("/" + "/".join(parts))
        elif url.endswith(".css"):
            assert ctype.startswith("text/css"), (url, ctype)
        elif url.endswith(".svg"):
            assert ctype.startswith("image/svg+xml"), (url, ctype)
    # The editor and every vendored CodeMirror module are reachable from app.js.
    assert "/static/js/editor.js" in seen
    assert {f"/static/vendor/codemirror/{p.name}" for p in VENDOR.glob("*.js")} <= seen


def test_static_needs_the_session_cookie(make_client):
    anonymous = make_client(logged_in=False)
    assert anonymous.get("/static/app.js").status_code == 401


def test_vendored_codemirror_with_licences():
    readme = (STATIC_DIR / "vendor" / "README.md").read_text(encoding="utf-8")
    script = ROOT / "scripts" / "vendor_codemirror.py"
    spec = importlib.util.spec_from_file_location("vendor_cm", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(module.PACKAGES) >= 10
    for npm_name, (version, _entry, short) in module.PACKAGES.items():
        js = VENDOR / f"{short}.js"
        licence = VENDOR / f"LICENSE-{short}"
        assert js.is_file() and js.stat().st_size > 500, js
        assert licence.is_file() and "MIT" in licence.read_text(encoding="utf-8").upper()
        assert js.read_text(encoding="utf-8").startswith(f"// {npm_name}@{version} ")
        assert f"`{npm_name}` | {version}" in readme, npm_name


def test_own_js_avoids_csp_and_injection_pitfalls():
    assert OWN_JS
    for path in OWN_JS:
        text = path.read_text(encoding="utf-8")
        assert "innerHTML" not in text and "outerHTML" not in text, path
        assert "insertAdjacentHTML" not in text and "document.write" not in text, path
        assert not re.search(r"setAttribute\(\s*['\"]style", text), path
        assert "eval(" not in text and "new Function" not in text, path
        # No third-party requests from the page (SPEC §15.3).
        assert not re.search(r"""(fetch|import)\(\s*['"`](https?:)?//""", text), path


def test_profile_form_fields_match_the_server():
    """profiles.js lists every Profile field (and the secrets) exactly once."""
    source = (STATIC_DIR / "js" / "profiles.js").read_text(encoding="utf-8")
    block = source.split("export const FIELDS = [", 1)[1].split("\n];", 1)[0]
    names = re.findall(r'\{\s*name:\s*"(\w+)"', block)
    assert len(names) == len(set(names))
    assert set(names) == set(api_profiles.FIELD_KINDS) | set(SECRET_FIELDS)


def test_profile_defaults_are_rendered_without_secrets(client):
    page = _page(client)
    body = next(a for t, a in page.tags if t == "body")
    defaults = json.loads(body["data-profile-defaults"])
    assert defaults == Profile(name="").to_dict()
    assert not set(defaults) & set(SECRET_FIELDS)


def test_wheel_contains_templates_and_static(tmp_path):
    pytest.importorskip("hatchling")
    out = tmp_path / "dist"
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps",
            "--no-build-isolation", "-q", "-w", str(out),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    (wheel,) = out.glob("floe-*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    expected = {
        "floe/web/templates/index.html",
        "floe/web/static/app.js",
        "floe/web/static/floe.css",
        "floe/web/static/vendor/README.md",
        *(f"floe/web/static/{p.relative_to(STATIC_DIR).as_posix()}" for p in OWN_JS),
        *(f"floe/web/static/vendor/codemirror/{p.name}" for p in VENDOR.iterdir()),
    }
    assert expected <= names, sorted(expected - names)


def test_save_as_image_is_client_side_only(client):
    """The SQL results bar gets a "Save as image…" button (built in grid.js), the Canvas
    module is served under the unchanged CSP, and nothing in it talks to the server."""
    assert (STATIC_DIR / "js" / "snapshot.js").is_file()
    assert _served(client, "/static/js/snapshot.js").split(";")[0] in JS_TYPES
    snap = (STATIC_DIR / "js" / "snapshot.js").read_text(encoding="utf-8")
    assert "fetch(" not in snap and "XMLHttpRequest" not in snap and "console." not in snap
    assert "html2canvas" not in snap
    assert "showSaveFilePicker" in snap and "revokeObjectURL" in snap
    grid = (STATIC_DIR / "js" / "grid.js").read_text(encoding="utf-8")
    assert "Save as image…" in grid
    app = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "onImage: saveSqlImage" in app and "Safety → Allow export" in app
    assert "'unsafe-inline'" not in CSP and "blob:" not in CSP and "data:" not in CSP
    index = client.get("/").text
    assert "Shift</kbd>+<kbd>S" in index  # listed in the shortcuts help
