"""Smoke test for the Scribe agent -- run against a live llama-server (LFM loaded).

    conda activate swarm
    python tests/smoke/test_scribe.py

Checks the three unchecked KICKOFF_CHECKLIST.md §6 Scribe items:
  1. deepdiff on model_dump() produces diff input
  2. ADROutput validates
  3. affected_diagrams restricted to 'context' only for MVP
"""
import asyncio

from agents.scribe import ADROutput, compute_spec_diff, run_scribe
from schemas.spec import (
    ArchitectureSpec,
    DataArchitecture,
    FunctionalRequirements,
    NonFunctionalRequirements,
    ProjectOverview,
    TechnicalConstraints,
)

_BASE_KWARGS = dict(
    functional_requirements=FunctionalRequirements(core_features=["C4 generation", "ADR generation"]),
    non_functional_requirements=NonFunctionalRequirements(security="on-device only, no egress"),
    technical_constraints=TechnicalConstraints(language_framework=["Python 3.11"]),
    data_architecture=DataArchitecture(storage_requirements="PostgreSQL 18"),
)

PRIOR_SPEC = ArchitectureSpec(
    project_overview=ProjectOverview(
        purpose="Automate C4 + ADR generation for solo devs.",
        target_users="Solo developers",
        deployment_environment="on-prem/edge",
    ),
    **_BASE_KWARGS,
)

CURRENT_SPEC = ArchitectureSpec(
    project_overview=ProjectOverview(
        purpose="Automate C4 + ADR generation for solo devs and small teams.",
        target_users="Solo developers and small teams",
        deployment_environment="on-prem/edge, Ubuntu 26.04 resolute",
    ),
    **_BASE_KWARGS,
)

FAKE_BLACKBOARD_CONTEXT = (
    "Architect: generated C4Context L1 diagram, 3 external actors, 2 system boundaries. "
    "No SPOFs flagged yet (Critic has not run)."
)


async def test_scribe_end_to_end() -> None:
    """pytest entry point -- requires pytest-asyncio (or pytest-anyio) and a live
    llama-server with LFM loaded. Mark async tests as such in pytest.ini/pyproject.toml:
        [tool.pytest.ini_options]
        asyncio_mode = "auto"
    """
    diff = compute_spec_diff(PRIOR_SPEC, CURRENT_SPEC)
    assert diff.to_dict(), "expected a non-empty diff between PRIOR_SPEC and CURRENT_SPEC"
    print("[1/3] deepdiff produced non-empty diff input: OK")

    adr = await run_scribe(PRIOR_SPEC, CURRENT_SPEC, FAKE_BLACKBOARD_CONTEXT)
    assert isinstance(adr, ADROutput)
    print("[2/3] ADROutput validated: OK")
    print(adr.model_dump_json(indent=2))

    assert adr.affected_diagrams == ["context"], adr.affected_diagrams
    print("[3/3] affected_diagrams restricted to ['context']: OK")


if __name__ == "__main__":
    # Direct-run path (no pytest / pytest-asyncio needed):
    #   conda activate swarm && python tests/smoke/test_scribe.py
    asyncio.run(test_scribe_end_to_end())
