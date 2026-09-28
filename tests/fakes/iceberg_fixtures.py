"""Small synthetic Iceberg tables (pyiceberg) and local-mode parquet files for tests.

Everything here is synthetic. Layout (mirrors the lake, SPEC §13.1 / PLAN "Paths"):

    <root>/iceberg/<container>/<key path>/{metadata,data}/...   (pyiceberg, SqlCatalog/sqlite)
    <root>/local/<container>/<key path>/part-0.parquet          (local mode)

Nessie refs → containers:

    eg-test1 → eg-test1, eg-test2 → eg-test2   (container = branch name rule)
    eg-test3 → eg-c3                            (only reachable via the tenant registry)
    main     → ref-a (gold_ref.codes), ref-b (registry.tenants)

`build_fixtures(base_dir)` writes everything and returns an `IcebergFixtures` holding the
`(ref, dotted key) -> metadata_location` mapping; `seed_fake_nessie(fake, fixtures)` points a
`FakeNessie` at them.
"""

from __future__ import annotations

import glob
import site
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

MAIN_REF = "main"
SHARED_CONTAINERS = ["ref-a", "ref-b"]
SHARED_NAMESPACES = ["gold_ref", "registry"]
REGISTRY_KEY = "registry.tenants"

# ref -> container for tenant branches
TENANT_CONTAINERS = {"eg-test1": "eg-test1", "eg-test2": "eg-test2", "eg-test3": "eg-c3"}

# Rows per tenant table: 3 rows of the tenant's own data_source + 2 rows of another tenant.
OWN_ROWS = 3
OTHER_ROWS = 2


def _tenant_rows(branch: str, other: str, n_own: int = OWN_ROWS) -> pa.Table:
    ids = list(range(1, n_own + OTHER_ROWS + 1))
    sources = [branch] * n_own + [other] * OTHER_ROWS
    return pa.table(
        {
            "id": pa.array(ids, pa.int64()),
            "amount": pa.array([float(i) * 10.5 for i in ids], pa.float64()),
            "code": pa.array([f"C{i:03d}" for i in ids], pa.string()),
            "data_source": pa.array(sources, pa.string()),
        }
    )


def _no_ds_rows() -> pa.Table:
    return pa.table(
        {
            "id": pa.array([1, 2], pa.int64()),
            "note": pa.array(["synthetic-a", "synthetic-b"], pa.string()),
        }
    )


def _codes_rows() -> pa.Table:
    return pa.table(
        {
            "code": pa.array(["C001", "C002", "C003", "C004"], pa.string()),
            "label": pa.array(["alpha", "beta", "gamma", "o'delta"], pa.string()),
        }
    )


def _registry_rows() -> pa.Table:
    return pa.table(
        {
            "eg_nid": pa.array([101, 102, 103, 104], pa.int64()),
            "container": pa.array(["eg-test1", "eg-test2", "eg-c3", "eg-orphan"], pa.string()),
            "branch": pa.array(["eg-test1", "eg-test2", "eg-test3", None], pa.string()),
            "catalog": pa.array(["nessie"] * 4, pa.string()),
            "warehouse": pa.array(["wh-synthetic"] * 4, pa.string()),
            "active": pa.array([True, False, True, True], pa.bool_()),
        }
    )


def fixture_tables() -> dict[tuple[str, str], tuple[str, pa.Table]]:
    """(ref, dotted key) -> (container, arrow table) for every fixture table."""
    tables: dict[tuple[str, str], tuple[str, pa.Table]] = {}
    for branch, other in (("eg-test1", "eg-test2"), ("eg-test2", "eg-test1")):
        container = TENANT_CONTAINERS[branch]
        tables[(branch, "bronze.medical")] = (container, _tenant_rows(branch, other))
        tables[(branch, "silver.input_layer.medical_claim")] = (
            container,
            _tenant_rows(branch, other, n_own=4),
        )
        tables[(branch, "gold.summary")] = (container, _tenant_rows(branch, other, n_own=2))
    # A tenant table without data_source (MissingDataSourceColumn).
    tables[("eg-test1", "silver.input_layer.no_ds")] = ("eg-test1", _no_ds_rows())
    # A stale copy of shared content on a tenant branch: must be hidden there.
    tables[("eg-test1", "gold_ref.codes")] = ("eg-test1", _codes_rows().slice(0, 1))
    # eg-test3 lives in container eg-c3 (registry mapping only).
    tables[("eg-test3", "gold.summary")] = ("eg-c3", _tenant_rows("eg-test3", "eg-test1"))
    # Shared content on main.
    tables[(MAIN_REF, "gold_ref.codes")] = ("ref-a", _codes_rows())
    tables[(MAIN_REF, REGISTRY_KEY)] = ("ref-b", _registry_rows())
    return tables


@dataclass
class IcebergFixtures:
    root: Path
    iceberg_root: Path
    local_root: Path
    locations: dict[tuple[str, str], str] = field(default_factory=dict)
    snapshot_ids: dict[tuple[str, str], int] = field(default_factory=dict)

    def location(self, ref: str, key: str) -> str:
        return self.locations[(ref, key)]


def fixture_location(path: Path) -> str:
    """Location string for `path` as handed to pyiceberg (and so to Nessie / DuckDB).

    POSIX: a `file://` URI. Windows: a forward-slash drive path (`C:/...`), because
    pyiceberg's PyArrowFileIO turns `file:///C:/x` into the invalid path `/C:/x`, while it
    treats drive-letter paths as local files; DuckDB accepts `C:/...` paths too.
    """
    path = Path(path).absolute()
    return path.as_posix() if sys.platform == "win32" else path.as_uri()


def build_fixtures(base_dir: str | Path) -> IcebergFixtures:
    """Write all fixture tables under `base_dir` and return their metadata locations."""
    from pyiceberg.catalog.sql import SqlCatalog

    base = Path(base_dir)
    iceberg_root = base / "iceberg"
    local_root = base / "local"
    iceberg_root.mkdir(parents=True, exist_ok=True)
    local_root.mkdir(parents=True, exist_ok=True)
    fx = IcebergFixtures(root=base, iceberg_root=iceberg_root, local_root=local_root)

    # One sqlite catalog per ref so the same key can exist on several refs.
    catalogs: dict[str, SqlCatalog] = {}
    for (ref, key), (container, data) in fixture_tables().items():
        cat = catalogs.get(ref)
        if cat is None:
            cat = SqlCatalog(
                f"fx_{ref.replace('-', '_')}",
                uri=f"sqlite:///{base / f'catalog-{ref}.db'}",
                warehouse=fixture_location(iceberg_root / "_warehouse"),
            )
            catalogs[ref] = cat
        elements = key.split(".")
        namespace = tuple(elements[:-1])
        cat.create_namespace_if_not_exists(namespace)
        table_dir = iceberg_root / container / Path(*elements)
        table = cat.create_table(key, schema=data.schema, location=fixture_location(table_dir))
        table.append(data)
        table = cat.load_table(key)
        fx.locations[(ref, key)] = table.metadata_location
        fx.snapshot_ids[(ref, key)] = table.current_snapshot().snapshot_id

        # Local-mode mirror: <local_root>/<container>/<key path>/part-0.parquet
        local_dir = local_root / container / Path(*elements)
        local_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(data, local_dir / "part-0.parquet")
    return fx


def seed_fake_nessie(fake, fixtures: IcebergFixtures, main_ref: str = MAIN_REF) -> None:
    """Create branches main / eg-test1 / eg-test2 / eg-test3 and add every fixture table."""
    fake.set_branch(main_ref)
    for branch in TENANT_CONTAINERS:
        fake.set_branch(branch)
    for (ref, key), location in fixtures.locations.items():
        target = main_ref if ref == MAIN_REF else ref
        fake.add_table(
            target,
            key,
            metadata_location=location,
            snapshot_id=fixtures.snapshot_ids[(ref, key)],
        )


def _install_from_wheels(con) -> None:
    """Install extensions shipped as PyPI wheels (duckdb-extension-*), if present.

    Only a test convenience for sandboxes that cannot reach extensions.duckdb.org.
    """
    for sp in site.getsitepackages():
        for path in glob.glob(f"{sp}/duckdb_extension_*/extensions/v*/*.duckdb_extension"):
            try:
                con.execute(f"INSTALL '{path}'")
            except Exception:  # pragma: no cover - best effort
                pass


def extension_unavailable_reason(*names: str) -> str | None:
    """Return None if all DuckDB extensions `names` can be INSTALLed/LOADed, else a reason."""
    import duckdb

    con = duckdb.connect()
    try:
        for attempt in range(2):
            try:
                for name in names:
                    con.execute(f"INSTALL {name}")
                    con.execute(f"LOAD {name}")
                return None
            except Exception as exc:
                if attempt == 0:
                    _install_from_wheels(con)
                    continue
                return f"DuckDB extension(s) {', '.join(names)} unavailable: {exc}"
    finally:
        con.close()
    return None  # pragma: no cover
