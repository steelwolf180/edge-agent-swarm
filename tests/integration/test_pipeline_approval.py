"""
tests/integration/test_pipeline_approval.py — full pipeline run through
architecture_review_workflow (checklist §8 End-to-End Run), asserting
the review gate at the end unblocks correctly on both approve and
reject.

SLOW + REAL HARDWARE: exercises all 5 agents, real llama-server calls,
both model swaps, and the thermal guard between every step. Expect
~4 minutes per test based on your 22 July full-run measurement.

Per pytest.ini, `integration` tests require a live DBOS/Postgres context
and are gated behind RUN_DBOS_TESTS=1 — the skipif below enforces that
the same way the rest of the suite does. Run with:

    RUN_DBOS_TESTS=1 pytest -m integration tests/integration/

Prereqs (same as `python pipeline/run.py --spec ...`):
  - llama-server up in router mode, both models loadable
  - mermaid.ink, Infracost, Phoenix containers up
  - Postgres reachable at DBOS_SYSTEM_DATABASE_URL
  - fixtures/minimal_spec.json — NOT included here. I don't have
    schemas/spec.py, so I can't fabricate a valid ArchitectureSpec dict
    with confidence (field names, required-vs-optional, nesting under
    functional_requirements.integration_points as referenced in
    run.py's _summarize_for_critic). Build this from a spec you've
    already run successfully through pipeline/run.py, plus the
    top-level "spec_version" key run.py requires separately.

THERMAL / RAM CAUTION: this is a real full pipeline run, same class of
workload as the 22 July hard-power-off incident logged in checklist §8.
Close non-essential foreground apps (memory: RAM/environment discipline
lesson) before running this, same as before a live demo.

APPROVAL TIMING: send_decision() fires immediately after the workflow
starts, not after a sleep. DBOS persists send() to Postgres regardless
of whether the destination workflow has reached its recv() call yet, so
the message sits queued until Judge finishes and recv_async() runs —
no need to guess or poll for the right moment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import psycopg

import pytest
from dbos import DBOS, DBOSConfig

from pipeline.run import architecture_review_workflow, __require_env
from pipeline.send_approval import send_decision

SPEC_FIXTURE = Path(__file__).parent / "fixtures" / "minimal_spec.json"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_DBOS_TESTS") != "1",
        reason="requires live DBOS/Postgres context; set RUN_DBOS_TESTS=1",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


@pytest.fixture(scope="module")
def dbos_launched():
    config: DBOSConfig = {
        "name": "edge-agent-swarm",
        "system_database_url": _require_env("DBOS_SYSTEM_DATABASE_URL"),
        # run.py's own instance uses 3010; use a different port here so
        # this test can run without run.py's process also being up.
        "admin_port": 3011,
    }
    DBOS(config=config)
    DBOS.launch()
    yield
    DBOS.destroy()


async def test_full_pipeline_approve_flow(dbos_launched):
    spec = json.loads(SPEC_FIXTURE.read_text())

    handle = await DBOS.start_workflow_async(
        architecture_review_workflow, spec, None, 1
    )
    send_decision(handle.workflow_id, approved=True, notes=None)

    result = await handle.get_result()

    assert result["review"] == {"approved": True, "notes": None}
    assert "researcher" in result and "architect" in result
    assert "scores" in result["judge"]


async def test_full_pipeline_reject_flow(dbos_launched):
    spec = json.loads(SPEC_FIXTURE.read_text())

    handle = await DBOS.start_workflow_async(
        architecture_review_workflow, spec, None, 1
    )
    send_decision(handle.workflow_id, approved=False, notes="needs another SPOF pass")

    result = await handle.get_result()

    assert result["review"] == {"approved": False, "notes": "needs another SPOF pass"}

    # persist_rejection_step (run.py) writes revision_cycles on rejection
    # as of this session's §7 persistence wiring — confirm the row landed.
    with psycopg.connect(os.environ["DBOS_SYSTEM_DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT revision_notes FROM revision_cycles WHERE workflow_id = %s",
                (handle.workflow_id,),
            )
            row = cur.fetchone()
    assert row is not None
    assert row[0] == "needs another SPOF pass"