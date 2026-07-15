"""
tests/smoke/test_critic.py

Pytest-discoverable smoke test for agents/critic.py, per KICKOFF_CHECKLIST.md §6:
    - CriticOutput validates (gaps, spofs, missing_integrations)
    - Gap list non-empty against a deliberately weak test spec

Not a unit test with mocks -- this makes a real call against the running
llama-server (LFM), same spirit as tests/smoke/test_llama.sh but at the
agent level instead of the raw model level.

Requires the llama-server router running with LFM loaded:
    conda activate swarm
    ./scripts/start_llama_router.sh
    ./tests/smoke/test_llama.sh lfm   # confirm LFM alive first

Run with:
    pytest -v -s tests/smoke/test_critic.py

(-s so the printed gaps/spofs/missing_integrations are visible -- pytest
captures stdout by default, and reading what Critic actually said is more
useful here than a bare pass/fail.)

The fixture is deliberately weak: one component (Postgres, no redundancy),
while the blackboard context name-drops three other services (Infracost,
mermaid.ink, Phoenix) that never made it into ArchitectOutput.components.
A healthy Critic should flag at least: Postgres as a SPOF, and the missing
integrations. If gaps comes back empty against this input, that's a signal
to revisit the system prompt, not a pass.
"""
import os
from datetime import datetime, timezone

import pytest

from agents.critic import run_critic
from schemas.adr import ADROutput
from schemas.architect import ArchitectOutput, Component, DiagramProvenance

from dotenv import load_dotenv
load_dotenv()

GEMMA_MODEL_NAME = os.environ.get("GEMMA_MODEL_NAME")  # must match the model name in models.ini

WEAK_ARCHITECT_OUTPUT = ArchitectOutput(
    context_diagram=(
        "C4Context\n"
        '    title Agent Swarm at the Edge - System Context (L1)\n'
        '    Person(engineer, "Solo Engineer", "Submits specs, reviews output")\n'
        '    System(pipeline, "Agent Swarm Pipeline", "5-agent architecture review")\n'
        '    System_Ext(postgres, "PostgreSQL", "Workflow state, blackboard, artifact store")\n'
        '    Rel(engineer, pipeline, "Submits spec, approves/rejects")\n'
        '    Rel(pipeline, postgres, "Reads/writes")\n'
    ),
    diagram_source=DiagramProvenance(
        model=GEMMA_MODEL_NAME,
        generated_at=datetime.now(timezone.utc),
        spec_version=1,
        informed_by_adrs=[],
    ),
    docs=(
        "The pipeline is a single system that talks to a solo engineer and a "
        "PostgreSQL instance for all state. No other external systems are shown."
    ),
    components=[
        Component(
            id="postgres",
            name="PostgreSQL",
            type="external_system",
            description="Workflow state, blackboard events, versioned artifact store.",
            technology="PostgreSQL 18",
            redundant=False,
        ),
    ],
)

WEAK_ADR_OUTPUT = ADROutput(
    context="Spec now requires durable state across all five pipeline agents.",
    decision="Use a single native PostgreSQL instance for all workflow and artifact state.",
    consequences="Postgres becomes the only persistence layer; no redundancy configured for MVP.",
    diff_summary="Added PostgreSQL as the sole state store; no prior version existed.",
    affected_diagrams=["context"],
)

# Deliberately mentions integrations that never made it into components above --
# this is what should trigger missing_integrations.
WEAK_BLACKBOARD_CONTEXT = (
    "Spec integration_points: Infracost GraphQL pricing API (localhost:4000), "
    "mermaid.ink diagram rendering (localhost:3001), Arize Phoenix observability "
    "(localhost:6006). Researcher pricing context: stub response, no live query "
    "executed for MVP."
)


@pytest.mark.asyncio
async def test_critic_flags_weak_spec() -> None:
    result = await run_critic(
        architect_output=WEAK_ARCHITECT_OUTPUT,
        adr_output=WEAK_ADR_OUTPUT,
        blackboard_context=WEAK_BLACKBOARD_CONTEXT,
    )

    print(f"\ngaps ({len(result.gaps)}):")
    for g in result.gaps:
        print(f"  - [{g.severity}] {g.description}  (component: {g.related_component})")

    print(f"\nspofs ({len(result.spofs)}):")
    for s in result.spofs:
        print(f"  - {s}")

    print(f"\nmissing_integrations ({len(result.missing_integrations)}):")
    for m in result.missing_integrations:
        print(f"  - {m}")

    assert result.gaps, "expected non-empty gaps against a deliberately weak spec"
    assert result.spofs, "expected Postgres to be flagged as a SPOF (no redundancy, sole state store)"
    assert result.missing_integrations, (
        "expected Infracost/mermaid.ink/Phoenix to be flagged as missing from "
        "components despite being named in blackboard context"
    )