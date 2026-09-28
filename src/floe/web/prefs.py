"""Small JSON UI-preferences file (SPEC §15.2): text only, never results.

Keys (all optional):

- `last_profile`: str | null
- `last_branch`: {profile name: branch name}
- `last_tab`: one of `TABS`
- `editor_text`: {profile name: SQL editor text}

`update()` merges: top-level scalars are replaced; for the two per-profile maps each
given entry is set, and an entry whose value is null is removed. Anything else (unknown
keys, wrong types, oversize text) raises `PrefsError` and nothing is written.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from floe.core import paths

TABS = ("preview", "schema", "sql", "timeline")
MAX_NAME = 200
MAX_EDITOR_TEXT = 200_000  # characters per profile
MAX_FILE_BYTES = 2_000_000
MAP_KEYS = ("last_branch", "editor_text")
KEYS = ("last_profile", "last_tab", *MAP_KEYS)


class PrefsError(ValueError):
    pass


def _check_name(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_NAME:
        raise PrefsError(f"{what} must be a non-empty string of at most {MAX_NAME} characters")
    return value


def _clean(data: Any) -> dict[str, Any]:
    """Drop anything invalid from data read from disk (never raises)."""
    out: dict[str, Any] = {}
    if not isinstance(data, dict):
        return out
    last = data.get("last_profile")
    out["last_profile"] = last if isinstance(last, str) and 0 < len(last) <= MAX_NAME else None
    out["last_tab"] = data.get("last_tab") if data.get("last_tab") in TABS else None
    for key in MAP_KEYS:
        value = data.get(key)
        out[key] = (
            {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(value, dict)
            else {}
        )
    return out


class PrefsStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else paths.prefs_path()
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return _clean(data)

    def update(self, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise PrefsError("prefs must be a JSON object")
        unknown = set(patch) - set(KEYS)
        if unknown:
            raise PrefsError(f"unknown prefs key(s): {', '.join(sorted(map(str, unknown)))}")
        with self._lock:
            current = self.load()
            if "last_profile" in patch:
                value = patch["last_profile"]
                current["last_profile"] = (
                    None if value is None else _check_name(value, "last_profile")
                )
            if "last_tab" in patch:
                value = patch["last_tab"]
                if value is not None and value not in TABS:
                    raise PrefsError(f"last_tab must be one of {', '.join(TABS)}")
                current["last_tab"] = value
            for key in MAP_KEYS:
                if key not in patch:
                    continue
                entries = patch[key]
                if not isinstance(entries, dict):
                    raise PrefsError(f"{key} must be an object of profile name → text")
                merged = dict(current[key])
                for name, value in entries.items():
                    _check_name(name, f"{key} profile name")
                    if value is None:
                        merged.pop(name, None)
                        continue
                    if not isinstance(value, str):
                        raise PrefsError(f"{key} values must be strings")
                    limit = MAX_EDITOR_TEXT if key == "editor_text" else MAX_NAME
                    if len(value) > limit:
                        raise PrefsError(f"{key} value too long (max {limit} characters)")
                    merged[name] = value
                current[key] = merged
            text = json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False)
            if len(text.encode("utf-8")) > MAX_FILE_BYTES:
                raise PrefsError("prefs too large")
            self._write(text)
            return current

    def forget_profile(self, name: str, new_name: str | None = None) -> None:
        """Remove (or move, on rename) a profile's entries."""
        with self._lock:
            current = self.load()
            changed = False
            for key in MAP_KEYS:
                if name in current[key]:
                    value = current[key].pop(name)
                    if new_name is not None:
                        current[key][new_name] = value
                    changed = True
            if current.get("last_profile") == name:
                current["last_profile"] = new_name
                changed = True
            if changed:
                self._write(json.dumps(current, indent=2, sort_keys=True, ensure_ascii=False))

    def _write(self, text: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".prefs-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
