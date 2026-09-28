"""Per-profile SQL query history (SPEC §9, §14 M6).

Stores SQL text only — never query results — as JSON under
`paths.app_support_dir()/history.json`, one list of entries per profile name, most
recent first, capped at `MAX_ENTRIES` distinct statements. No Qt here.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from floe.core import paths

MAX_ENTRIES = 200


@dataclass(frozen=True)
class HistoryEntry:
    """One recorded query. `sql` is the exact text that was run; nothing else."""

    sql: str
    branch: str | None
    timestamp: float  # unix epoch seconds


class HistoryStore:
    """JSON-backed store of each profile's last `MAX_ENTRIES` distinct queries."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else paths.app_support_dir() / "history.json"

    @property
    def path(self) -> Path:
        return self._path

    def _read_all(self) -> dict[str, list[dict[str, Any]]]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _write_all(self, data: dict[str, list[dict[str, Any]]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".history-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._path)
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        os.chmod(self._path, 0o600)

    def list(self, profile_name: str) -> list[HistoryEntry]:
        """This profile's history, most recent first."""
        raw = self._read_all().get(profile_name, [])
        return [
            HistoryEntry(sql=e["sql"], branch=e.get("branch"), timestamp=e["timestamp"])
            for e in raw
        ]

    def record(
        self,
        profile_name: str,
        sql: str,
        branch: str | None,
        timestamp: float | None = None,
    ) -> None:
        """Record a successfully run query.

        Re-running an identical statement moves it to the front instead of duplicating
        it; the list is capped at `MAX_ENTRIES` per profile.
        """
        sql = sql.strip()
        if not sql:
            return
        ts = time.time() if timestamp is None else timestamp
        data = self._read_all()
        entries = [e for e in data.get(profile_name, []) if e.get("sql") != sql]
        entries.insert(0, {"sql": sql, "branch": branch, "timestamp": ts})
        data[profile_name] = entries[:MAX_ENTRIES]
        self._write_all(data)

    def clear(self, profile_name: str) -> None:
        data = self._read_all()
        if profile_name in data:
            del data[profile_name]
            self._write_all(data)
