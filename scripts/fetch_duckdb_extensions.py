#!/usr/bin/env python3
"""Pre-download DuckDB extensions into a directory for bundling into the .app.

Usage: fetch_duckdb_extensions.py <out_dir>

Sets `extension_directory` to `<out_dir>` and installs the extensions Floe needs at
runtime (SPEC §6.2, §11.3): `azure`, `iceberg`, `httpfs`. Run this on the same OS/arch
as the release build (the macOS runner) before PyInstaller, so the .app can bundle
them and set `extension_directory` itself instead of downloading them on first use.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

EXTENSIONS = ("httpfs", "azure", "iceberg")
# Extensions that iceberg may pull in transitively on some DuckDB builds; best-effort.
OPTIONAL_EXTENSIONS = ("avro",)


def fetch(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(":memory:")
    try:
        conn.execute(f"SET extension_directory = '{out_dir}'")
        for ext in EXTENSIONS:
            print(f"Installing DuckDB extension: {ext}")
            conn.execute(f"INSTALL {ext}")
            conn.execute(f"LOAD {ext}")
        for ext in OPTIONAL_EXTENSIONS:
            try:
                print(f"Installing optional DuckDB extension: {ext}")
                conn.execute(f"INSTALL {ext}")
                conn.execute(f"LOAD {ext}")
            except duckdb.Error as exc:
                print(f"Skipping optional extension {ext}: {exc}", file=sys.stderr)
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <out_dir>", file=sys.stderr)
        return 2
    out_dir = Path(argv[1]).resolve()
    fetch(out_dir)
    print(f"DuckDB extensions written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
