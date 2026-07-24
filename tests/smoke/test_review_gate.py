"""
tests/smoke/test_review_gate.py — pytest unit test for the
DBOS.recv()/DBOSClient.send() review gate, isolated from the real
5-agent pipeline.

Uses a dummy workflow (not architecture_review_workflow), so this runs in
well under a second against a real Postgres test database — no LLM
calls, no thermal guard, no llama-server dependency. Belongs in
tests/smoke/ alongside your other per-piece validation tests, not
tests/integration/ (see test_pipeline_approval.py for that).

Uses the reset_dbos fixture verbatim from the DBOS testing guide. NOTE:
that guide's example fixture sets `"database_url"` in DBOSConfig, but the
DBOSConfig field list elsewhere in the same doc only documents
`system_database_url` / `application_database_url` — no `database_url`
key. Kept as-written below since it's the guide's literal required
pattern, but if this fixture errors on an unexpected key, swap to
`system_database_url` first.

Per pytest.ini, anything touching a live DBOS/Postgres context is an
`integration` test gated behind RUN_DBOS_TESTS=1 — applied per-test below
(not module-wide) since one test here (validation of send_approval.py's
own argument parsing) needs neither DBOS nor Postgres and should stay
fast and always-on. Run the DBOS-backed ones with:

    RUN_DBOS_TESTS=1 pytest -m integration tests/smoke/test_review_gate.py
"""

from __future__ import annotations

import os

import pytest
from dbos import DBOS, DBOSConfig

from pipeline.send_approval import REVIEW_TOPIC, send_decision

skip_without_live_dbos = pytest.mark.skipif(
    os.environ.get("RUN_DBOS_TESTS") != "1",
    reason="requires live DBOS/Postgres context; set RUN_DBOS_TESTS=1",
)


@pytest.fixture()
def reset_dbos():
    DBOS.destroy()
    config: DBOSConfig = {
        "name": "edge-agent-swarm-test",
        "system_database_url": os.environ.get("TESTING_DATABASE_URL"),
    }
    DBOS(config=config)
    DBOS.reset_system_database()
    DBOS.launch()
    yield
    DBOS.destroy()


@DBOS.workflow()
async def _dummy_review_workflow() -> dict | None:
    return await DBOS.recv_async(topic=REVIEW_TOPIC, timeout_seconds=30)


# asyncio_mode = auto is already configured project-wide, so no
# @pytest.mark.asyncio decorator needed on these — consistent with
# the rest of the suite.

@pytest.mark.integration
@skip_without_live_dbos
async def test_approval_unblocks_workflow(reset_dbos, monkeypatch):
    monkeypatch.setenv(
        "DBOS_SYSTEM_DATABASE_URL", os.environ["TESTING_DATABASE_URL"]
    )
    handle = await DBOS.start_workflow_async(_dummy_review_workflow)
    send_decision(handle.workflow_id, approved=True, notes=None)

    result = await handle.get_result()

    assert result == {"approved": True, "notes": None}


@pytest.mark.integration
@skip_without_live_dbos
async def test_rejection_carries_notes(reset_dbos, monkeypatch):
    monkeypatch.setenv(
        "DBOS_SYSTEM_DATABASE_URL", os.environ["TESTING_DATABASE_URL"]
    )
    handle = await DBOS.start_workflow_async(_dummy_review_workflow)
    send_decision(handle.workflow_id, approved=False, notes="needs another SPOF pass")

    result = await handle.get_result()

    assert result == {"approved": False, "notes": "needs another SPOF pass"}


async def test_reject_requires_nonempty_notes():
    # Doesn't need reset_dbos/a real workflow at all — this tests
    # send_approval.py's own validation, which happens before any DBOS
    # call. Mirrors argparse's parser.error() -> SystemExit behavior.
    import sys

    from pipeline.send_approval import main

    old_argv = sys.argv
    sys.argv = ["send_approval.py", "some-workflow-id", "--reject", "   "]
    try:
        with pytest.raises(SystemExit):
            main()
    finally:
        sys.argv = old_argv


@pytest.mark.integration
@skip_without_live_dbos
async def test_timeout_returns_none_when_nobody_sends(reset_dbos):
    # No send_decision() call — recv() should time out and return None
    # per its documented behavior, not hang the test suite. This test
    # takes ~30s (the dummy workflow's timeout_seconds) by design.
    handle = await DBOS.start_workflow_async(_dummy_review_workflow)
    result = await handle.get_result()

    assert result is None