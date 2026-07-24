"""
tests/smoke/test_persistence.py — smoke test for pipeline/persistence.py.

Liveness only: can we reach DBOS_SYSTEM_DATABASE_URL, and can we write to
disk under a temp artifacts/ root. No correctness assertions on data
round-tripping — that's tests/integration/test_persistence.py. Mirrors
test_llama.sh's shape: fast, shallow, "does it turn on."

Run:
    pytest tests/smoke/test_persistence.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from pipeline.persistence import _db_url, persist_adr
from schemas.adr import ADROutput

SMOKE_SPEC_VERSION = 999_002


def test_can_connect_to_system_database():
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)


def test_can_query_app_tables_exist():
    """Confirms 001_app_tables.sql + 002_add_adr_id.sql were actually
    applied — a missing migration should fail loud here, not three steps
    deep into a real pipeline run."""
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            for table in ("spec_versions", "pipeline_runs", "artifacts", "revision_cycles"):
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)",
                    (table,),
                )
                assert cur.fetchone()[0], f"table '{table}' does not exist — run pipeline/run_migration.py"

            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'artifacts' AND column_name = 'adr_id')"
            )
            assert cur.fetchone()[0], "artifacts.adr_id missing — run 002_add_adr_id.sql"


def test_can_write_adr_file(tmp_path):
    """Filesystem liveness only — not checking round-trip correctness
    against agents/architect.py's parser (that's the integration test)."""
    artifacts_root = tmp_path / "artifacts"
    adr_output = ADROutput(
        context="Smoke test context.",
        decision="Smoke test decision.",
        consequences="Smoke test consequences.",
        diff_summary="Smoke test diff.",
        diff_hunk_count=1,
        affected_diagrams=["context"],
    )
    record = persist_adr(
        adr_output,
        spec_version=SMOKE_SPEC_VERSION,
        supersedes=None,
        artifacts_root=artifacts_root,
    )
    written_path = artifacts_root / f"v{SMOKE_SPEC_VERSION}" / f"{record.adr_id}.md"
    assert written_path.exists()
    assert written_path.stat().st_size > 0