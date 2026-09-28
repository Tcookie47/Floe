"""M1 "done when" script: list tables on a branch and query one against the fake Nessie.

Everything is synthetic: it starts the in-process fake Nessie, writes small pyiceberg
fixture tables into a temp dir, then uses `FloeSession` exactly as the UI will.

    .venv/bin/python examples/list_and_query.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))  # for tests.fakes
sys.path.insert(0, str(REPO / "src"))

from floe.core.context import FloeSession  # noqa: E402
from floe.core.nessie import NessieClient  # noqa: E402
from tests.fakes.fake_nessie import DEFAULT_CLIENT_SECRET, FakeNessie  # noqa: E402
from tests.fakes.iceberg_fixtures import (  # noqa: E402
    REGISTRY_KEY,
    SHARED_CONTAINERS,
    SHARED_NAMESPACES,
    build_fixtures,
    extension_unavailable_reason,
    seed_fake_nessie,
)


def main() -> int:
    reason = extension_unavailable_reason("iceberg")
    if reason:
        print(f"Cannot run: {reason}")
        return 1
    storage_ok = extension_unavailable_reason("azure", "httpfs") is None

    with tempfile.TemporaryDirectory(prefix="floe-example-") as tmp, FakeNessie() as fake:
        fixtures = build_fixtures(tmp)
        seed_fake_nessie(fake, fixtures)
        profile = fake.profile(
            name="example",
            shared_containers=SHARED_CONTAINERS,
            shared_namespaces=SHARED_NAMESPACES,
            tenant_registry_table=REGISTRY_KEY,
        )
        secrets = {"nessie_client_secret": DEFAULT_CLIENT_SECRET}
        session = FloeSession(
            profile,
            secrets,
            NessieClient(profile, DEFAULT_CLIENT_SECRET),
            local_storage_root=fixtures.iceberg_root,
            skip_storage_setup=not storage_ok,
        )

        print("Branches:")
        for b in session.branches():
            state = {True: "active", False: "inactive", None: "-"}[b.active]
            print(f"  {b.name:10} {state}")

        branch = "eg-test1"
        print(f"\nTables on {branch}:")
        for t in session.list_tables(branch):
            origin = f"shared, from {t.source_ref}" if t.shared else branch
            print(f"  {t.dotted:36} view={t.view_name:34} ({origin})")

        print("\nPreview of silver_input_layer_medical_claim (tenant filter on):")
        preview = session.preview(branch, "silver_input_layer_medical_claim")
        print(preview.df.to_string(index=False))

        sql = (
            "SELECT m.code, c.label, m.amount FROM gold_summary m "
            "JOIN gold_ref_codes c USING (code) ORDER BY m.code"
        )
        print(f"\nQuery: {sql}")
        result = session.query(branch, sql, limit=100)
        print(result.df.to_string(index=False))
        print(
            f"\n{result.row_count} rows in {result.elapsed * 1000:.0f} ms "
            f"(truncated={result.truncated}, reloaded={result.reloaded})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
