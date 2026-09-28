#!/usr/bin/env python3
"""Write `src/floe/_build_info.py` from a release tag and commit SHA.

Usage: write_build_info.py <version> <commit>

`<version>` is normally the tag ref name (e.g. `v0.1.0`, or `desktop-v0.1.0` for the
desktop release); the `desktop-` prefix and a leading `v` are stripped, and the rest must
be a valid PEP 440 version (hatchling reads it as the wheel/sdist version). It is written
in normalised form (e.g. `1.2.0-rc1` -> `1.2.0rc1`); anything else fails with exit code 2.
`<commit>` is normally the short GITHUB_SHA. Used by the release workflows before
building. `_build_info.py` is gitignored: it only exists in a release build, and
`floe.__init__` falls back to `"0.0.0.dev0"` / a live `git` lookup when it is absent
(local/dev checkouts, `pip install git+…`).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TEMPLATE = '''"""Generated at release time by scripts/write_build_info.py. Do not edit by hand."""

__version__ = {version!r}
__commit__ = {commit!r}
'''


class BuildInfoError(ValueError):
    """The tag/version or commit can't be used for a release build."""


def normalize_version(version: str) -> str:
    """`v1.2.3` / `desktop-v1.2.3` -> `1.2.3`; raises `BuildInfoError` if not PEP 440."""
    from packaging.version import InvalidVersion, Version

    raw = version.strip()
    clean = raw.removeprefix("desktop-")
    clean = clean[1:] if clean[:1] in ("v", "V") else clean
    try:
        return str(Version(clean))
    except InvalidVersion:
        raise BuildInfoError(
            f"{raw!r} is not a valid PEP 440 version (after stripping a leading 'v'). "
            "Use a tag like v1.2.3, v1.2.3rc1 or v1.2.3.dev0; for CI builds use e.g. 0.0.0.dev0."
        ) from None


def write_build_info(version: str, commit: str, out_path: Path) -> str:
    """Write the file; returns the normalised version written."""
    clean_version = normalize_version(version)
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,64}", commit):
        raise BuildInfoError(f"{commit!r} is not a valid commit id.")
    out_path.write_text(TEMPLATE.format(version=clean_version, commit=commit))
    return clean_version


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <version> <commit>", file=sys.stderr)
        return 2
    _, version, commit = argv
    out_path = Path(__file__).resolve().parent.parent / "src" / "floe" / "_build_info.py"
    try:
        clean_version = write_build_info(version, commit, out_path)
    except BuildInfoError as exc:
        print(f"write_build_info: error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {out_path} (version={clean_version}, commit={commit})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
