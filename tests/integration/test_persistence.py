"""
tests/integration/test_persistence.py — integration test for
pipeline/persistence.py (§7).

Verifies correct interaction across three real components: Postgres
(DBOS_SYSTEM_DATABASE_URL), the filesystem (artifacts/adr/v<n>/adr_*.md
and, as of 28 Aug 2026, artifacts/architecture/v<n>/adr_*.md), and
agents/architect.py's ADR parser. Not a smoke test — asserts field-level
correctness, round-trips through a second module's real parsing logic,
and checks idempotency under simulated DBOS step retry. See
tests/smoke/test_persistence.py for pure liveness checks.

Requires: DBOS_SYSTEM_DATABASE_URL set (.env), migrations applied
(001_app_tables.sql, 002_add_adr_id.sql — confirmed applied 24 Jul).

Uses a distinct workflow_id per run (uuid) so re-running doesn't collide
with prior test rows or real pipeline rows in the same DB. Cleans up its
own Postgres rows; ADR files live under pytest's tmp_path, never the real
artifacts/ tree.

Run:
    pytest tests/integration/test_persistence.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import uuid

import psycopg
import pytest

from pipeline.persistence import (
    ensure_spec_version_row,
    ensure_pipeline_run_row,
    update_pipeline_run_status,
    persist_adr,
    persist_diagram,
    insert_artifact_row,
    insert_revision_cycle_row,
    _db_url,
)
from schemas.adr import ADROutput
from schemas.architect import ArchitectOutput, DiagramProvenance
from schemas.judge import JudgeOutput, MetricScore

# Deliberately far outside real spec_version range, so this test's DB rows
# and artifacts/v<n>/ folder are obviously test data.
TEST_SPEC_VERSION = 999_001


@pytest.fixture
def workflow_id() -> str:
    return f"integration-test-{uuid.uuid4()}"


@pytest.fixture
def artifacts_root(tmp_path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture(autouse=True)
def cleanup_db_rows(workflow_id):
    yield
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM artifacts WHERE workflow_id = %s", (workflow_id,))
            cur.execute("DELETE FROM revision_cycles WHERE workflow_id = %s", (workflow_id,))
            cur.execute("DELETE FROM pipeline_runs WHERE workflow_id = %s", (workflow_id,))
        conn.commit()


def _stub_adr_output() -> ADROutput:
    return ADROutput(
        context="Integration test context.",
        decision="Integration test decision.",
        consequences="Integration test consequences.",
        diff_summary="Integration test diff.",
        diff_hunk_count=1,
        affected_diagrams=["context"],
    )


def _stub_architect_output() -> ArchitectOutput:
    return ArchitectOutput(
        context_diagram="C4Context\n    title Integration Test\n",
        diagram_source=DiagramProvenance(
            model="integration-test-model",
            generated_at=datetime.now(timezone.utc),
            spec_version=TEST_SPEC_VERSION,
            informed_by_adrs=[],
        ),
        docs="Integration test docs.",
        components=[
            {
                "id": "system_under_test",
                "name": "Integration Test System",
                "type": "internal_system",
                "description": "Stub component for persistence.py integration test.",
                "technology": None,
                "redundant": False,
            }
        ],
    )

def _stub_judge_output() -> JudgeOutput:
    def _score(value: float, target: float, flag_threshold: float, direction: str) -> MetricScore:
        return MetricScore(
            value=value,
            target=target,
            flag_threshold=flag_threshold,
            direction=direction,
            flagged=False,
            flag_reason=None,
        )

    return JudgeOutput(
        scores={
            "spof_count": _score(0.0, target=0.0, flag_threshold=1.0, direction="lower_is_better"),
            "redundancy_ratio": _score(1.0, target=1.0, flag_threshold=0.5, direction="higher_is_better"),
            "cost_per_component": _score(0.0, target=0.0, flag_threshold=100.0, direction="lower_is_better"),
            "integration_coverage": _score(1.0, target=1.0, flag_threshold=0.5, direction="higher_is_better"),
            "adrs_per_diff": _score(1.0, target=1.0, flag_threshold=1.0, direction="higher_is_better"),
        },
        cost_estimate=0.0,
        recommendation="approve",
        flagged_for_review=[],
    )


# ---------------------------------------------------------------------------
# Approval path
# ---------------------------------------------------------------------------

def test_approval_path_end_to_end(workflow_id, artifacts_root):
    spec_version_id = ensure_spec_version_row(
        TEST_SPEC_VERSION, {"spec_version": TEST_SPEC_VERSION, "note": "integration test"}
    )
    assert isinstance(spec_version_id, int)

    ensure_pipeline_run_row(workflow_id, spec_version_id)

    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM pipeline_runs WHERE workflow_id = %s", (workflow_id,))
            row = cur.fetchone()
    assert row is not None, "pipeline_runs row was not created"
    assert row[0] == "pending"

    adr_record = persist_adr(
        _stub_adr_output(),
        spec_version=TEST_SPEC_VERSION,
        supersedes=None,
        artifacts_root=artifacts_root,
    )
    assert adr_record.adr_id == "adr_0001"
    written_path = artifacts_root / "adr" / f"v{TEST_SPEC_VERSION}" / f"{adr_record.adr_id}.md"
    assert written_path.exists()

    # Round-trip through the REAL agents/architect.py parser — the
    # highest-value assertion in this file. A frontmatter mismatch here
    # doesn't raise on the read side in production; it silently drops the
    # ADR from PRIOR_DECISIONS (see serialize_adr_markdown()'s docstring).
    from agents.architect import _parse_adr_markdown
    reparsed = _parse_adr_markdown(written_path)
    assert reparsed.adr_id == adr_record.adr_id
    assert reparsed.context == adr_record.context
    assert reparsed.decision == adr_record.decision
    assert reparsed.consequences == adr_record.consequences

    # Diagram file — pairs with the ADR file above by identical filename
    # under a different top-level folder (artifacts/architecture/v<n>/
    # vs artifacts/adr/v<n>/). Added 28 Aug 2026 alongside the adr/ split.
    architect_output = _stub_architect_output()
    diagram_path = persist_diagram(
        adr_record.adr_id,
        TEST_SPEC_VERSION,
        architect_output.context_diagram,
        artifacts_root=artifacts_root,
    )
    expected_diagram_path = (
        artifacts_root / "architecture" / f"v{TEST_SPEC_VERSION}" / f"{adr_record.adr_id}.md"
    )
    assert diagram_path == expected_diagram_path
    assert diagram_path.exists()
    diagram_text = diagram_path.read_text()
    assert "```mermaid" in diagram_text
    assert architect_output.context_diagram.strip() in diagram_text

    # Exercises the ArchitectOutput.diagram_source.model_dump_json()
    # assumption directly, with a real DiagramProvenance instance.
    artifact_id = insert_artifact_row(
        workflow_id,
        architect_output,
        adr_record,
        _stub_judge_output(),
    )
    assert isinstance(artifact_id, int)

    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT adr_id, adr_context, adr_decision, adr_consequences, judge_scores "
                "FROM artifacts WHERE id = %s",
                (artifact_id,),
            )
            db_row = cur.fetchone()
    assert db_row is not None
    assert db_row[0] == adr_record.adr_id
    assert db_row[1] == adr_record.context
    assert db_row[2] == adr_record.decision
    assert db_row[3] == adr_record.consequences
    assert db_row[4] == _stub_judge_output().model_dump(mode="json")["scores"]

    update_pipeline_run_status(workflow_id, "approved")
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM pipeline_runs WHERE workflow_id = %s", (workflow_id,))
            row = cur.fetchone()
    assert row[0] == "approved"


# ---------------------------------------------------------------------------
# Rejection path
# ---------------------------------------------------------------------------

def test_rejection_path(workflow_id):
    spec_version_id = ensure_spec_version_row(
        TEST_SPEC_VERSION, {"spec_version": TEST_SPEC_VERSION, "note": "integration test"}
    )
    ensure_pipeline_run_row(workflow_id, spec_version_id)

    revision_id = insert_revision_cycle_row(workflow_id, "Integration test rejection notes.")
    assert isinstance(revision_id, int)

    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT revision_notes FROM revision_cycles WHERE id = %s", (revision_id,))
            row = cur.fetchone()
    assert row is not None
    assert row[0] == "Integration test rejection notes."

    update_pipeline_run_status(workflow_id, "rejected")
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM pipeline_runs WHERE workflow_id = %s", (workflow_id,))
            row = cur.fetchone()
    assert row[0] == "rejected"


# ---------------------------------------------------------------------------
# Idempotency — DBOS steps can be retried on workflow recovery; this
# checks ensure_pipeline_run_row's ON CONFLICT DO NOTHING actually holds.
# ---------------------------------------------------------------------------

def test_ensure_pipeline_run_row_is_idempotent(workflow_id):
    spec_version_id = ensure_spec_version_row(
        TEST_SPEC_VERSION, {"spec_version": TEST_SPEC_VERSION, "note": "integration test"}
    )
    ensure_pipeline_run_row(workflow_id, spec_version_id)
    ensure_pipeline_run_row(workflow_id, spec_version_id)  # simulated retry

    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pipeline_runs WHERE workflow_id = %s", (workflow_id,))
            count = cur.fetchone()[0]
    assert count == 1, "ON CONFLICT DO NOTHING should prevent a duplicate row"