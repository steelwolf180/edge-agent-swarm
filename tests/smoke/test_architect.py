"""
tests/smoke/test_architect.py

Closes out KICKOFF_CHECKLIST.md §6 for Architect:
    [ ] C4Context output starts with C4Context
    [ ] Diagram renders correctly in mermaid.ink
    [ ] ArchitectOutput validates

Not mocked — hits your real local stack (llama-server + mermaid.ink),
same as the rest of your infra smoke tests. One live call is shared
across all three assertions via the module-scoped fixture, so a slow
Gemma completion only happens once per run.

Preconditions:
    - llama-server running with the router preset (checklist §5)
    - mermaid.ink reachable at MERMAID_INK_URL
    - .env present in the current working directory

Run from repo root:
    pytest tests/smoke/test_architect.py -v
"""

from __future__ import annotations

import pytest

from agents.architect import call_architect
from pipeline.render import render_diagram_image
from schemas.architect import ArchitectOutput

STUB_SPEC = {
    "spec_version": 1,
    "project_name": "Agent Swarm at the Edge",
    "purpose": "Local multi-agent pipeline for C4 + ADR generation",
    "components_hint": ["llama-server", "PostgreSQL", "DBOS pipeline", "mermaid.ink"],
}
STUB_PRICING_CONTEXT = {
    "postgres_rds_equivalent_usd_month": 0,
    "note": "Fully local deployment, Infracost stub for MVP",
}


@pytest.fixture(scope="module")
def architect_output() -> ArchitectOutput:
    return call_architect(STUB_SPEC, STUB_PRICING_CONTEXT)


def test_diagram_starts_with_c4context(architect_output: ArchitectOutput) -> None:
    assert architect_output.context_diagram.startswith("C4Context")


def test_architect_output_validates(architect_output: ArchitectOutput) -> None:
    # Round-trip through the schema again, not just trust the object call_architect
    # already built — catches anything that only "passed" because it skipped validation.
    dumped = architect_output.model_dump()
    revalidated = ArchitectOutput.model_validate(dumped)
    assert revalidated == architect_output


def test_diagram_renders_in_mermaid_ink(architect_output: ArchitectOutput) -> None:
    image_bytes = render_diagram_image(architect_output.context_diagram)
    # JPEG SOI magic bytes — confirms mermaid.ink returned a real image, not an
    # HTML error page or empty response. mermaid.ink's /img/ endpoint returns
    assert image_bytes[:3] == b"\xff\xd8\xff"
