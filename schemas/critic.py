"""
schemas/critic.py

Pydantic v2 models for the Critic agent (LFM2.5-VL-1.6B).

Critic reads ArchitectOutput (diagram + components) and ADROutput (the
decision Scribe just recorded) and plays devil's advocate: surfaces design
gaps, single points of failure, and integration points implied by the spec
but missing from the diagram (spec §5 Data Flow: "Critic (LFM) reads
ArchitectOutput + ADROutput -> generates CriticOutput (gap list)"). No
tools -- pure text generation consuming blackboard context only
(spec §2 Tools per Agent).

Deliberately no "gaps must be non-empty" validator here, unlike
ArchitectOutput.components. An empty gap list can be a legitimate verdict
for a well-specified diagram; forcing non-empty at the schema level would
make that unrepresentable. Whether gaps actually appear against a
deliberately weak test spec (KICKOFF_CHECKLIST.md §6) is a behavioral
smoke-test concern, not a structural invariant -- unlike a diagram with
zero components, which has nothing to render at all.
"""
from typing import Literal

from pydantic import BaseModel, Field


class Gap(BaseModel):
    """A single design gap flagged against the Architect's diagram."""

    description: str = Field(
        ..., description="What's missing or under-specified, and why it matters"
    )
    severity: Literal["low", "medium", "high"] = Field(
        ..., description="Critic's own judgment of impact if left unaddressed"
    )
    related_component: str | None = Field(
        default=None,
        description="Component.id from ArchitectOutput this gap relates to, if any. "
        "None for gaps that concern the system as a whole rather than one component.",
    )


class CriticOutput(BaseModel):
    """Structured output contract for the Critic agent.

    spofs and missing_integrations are kept as flat list[str], matching
    spec §5 Pydantic Models exactly -- they feed Judge's spof_count and
    integration_coverage metrics as raw counts, not structured objects
    Judge would need to parse further.
    """

    gaps: list[Gap] = Field(default_factory=list)
    spofs: list[str] = Field(
        default_factory=list,
        description="Component ids or names flagged as single points of failure. "
        "Feeds Judge's spof_count.",
    )
    missing_integrations: list[str] = Field(
        default_factory=list,
        description="Integration points implied by the spec/blackboard context but absent "
        "from ArchitectOutput.components. Feeds Judge's integration_coverage.",
    )
