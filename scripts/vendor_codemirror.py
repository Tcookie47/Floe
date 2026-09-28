#!/usr/bin/env python3
"""Vendor CodeMirror 6 (pinned) into `src/floe/web/static/vendor/codemirror/`.

Usage: vendor_codemirror.py

Downloads each package's npm tarball, copies its ES-module entry file to
`<short name>.js` and its LICENSE to `LICENSE-<short name>`, and rewrites bare import
specifiers (`from '@codemirror/state'`) to relative ones (`from './state.js'`). No
bundler and no import map: an import map would be an inline `<script>`, which the web
app's CSP forbids. The files are otherwise unmodified. Re-run after bumping a version
in PACKAGES and update `static/vendor/README.md`.
"""

from __future__ import annotations

import io
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

# npm name → (version, entry file inside the tarball, vendored short name)
PACKAGES: dict[str, tuple[str, str, str]] = {
    "@codemirror/state": ("6.7.6", "dist/index.js", "state"),
    "@codemirror/view": ("6.43.13", "dist/index.js", "view"),
    "@codemirror/language": ("6.12.4", "dist/index.js", "language"),
    "@codemirror/commands": ("6.11.1", "dist/index.js", "commands"),
    "@codemirror/autocomplete": ("6.20.3", "dist/index.js", "autocomplete"),
    "@codemirror/search": ("6.7.2", "dist/index.js", "search"),
    "@codemirror/lang-sql": ("6.10.0", "dist/index.js", "lang-sql"),
    "@lezer/common": ("1.5.3", "dist/index.js", "lezer-common"),
    "@lezer/highlight": ("1.2.5", "dist/index.js", "lezer-highlight"),
    "@lezer/lr": ("1.4.10", "dist/index.js", "lezer-lr"),
    "style-mod": ("4.1.4", "src/style-mod.js", "style-mod"),
    "w3c-keyname": ("2.2.8", "index.js", "w3c-keyname"),
    "crelt": ("1.0.7", "index.js", "crelt"),
    "@marijn/find-cluster-break": ("1.0.4", "src/index.js", "find-cluster-break"),
}

OUT = Path(__file__).resolve().parent.parent / "src/floe/web/static/vendor/codemirror"
_IMPORT_RE = re.compile(
    r"""^((?:import|export)\b[^'";]*?\bfrom\s*|import\s*)(['"])([^'"./][^'"]*)\2""", re.M
)


def _tarball_url(name: str, version: str) -> str:
    return f"https://registry.npmjs.org/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"


def _rewrite(source: str, name: str) -> str:
    def repl(m: re.Match[str]) -> str:
        spec = m.group(3)
        if spec not in PACKAGES:
            raise SystemExit(f"{name}: unexpected import {spec!r}")
        return f"{m.group(1)}{m.group(2)}./{PACKAGES[spec][2]}.js{m.group(2)}"

    return _IMPORT_RE.sub(repl, source)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (version, entry, short) in PACKAGES.items():
        with urllib.request.urlopen(_tarball_url(name, version), timeout=60) as resp:
            data = resp.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            source = tar.extractfile(f"package/{entry}").read().decode("utf-8")
            license_text = tar.extractfile("package/LICENSE").read().decode("utf-8")
        header = f"// {name}@{version} ({entry}) — vendored by scripts/vendor_codemirror.py\n"
        (OUT / f"{short}.js").write_text(header + _rewrite(source, name), encoding="utf-8")
        (OUT / f"LICENSE-{short}").write_text(license_text, encoding="utf-8")
        print(f"{name}@{version} -> {short}.js")
    return 0


if __name__ == "__main__":
    sys.exit(main())
