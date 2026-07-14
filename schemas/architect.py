"""
schemas/architect.py

Pydantic v2 models for the Architect agent (Gemma 4 E4B QAT).

Architect reads the DBOS blackboard (spec + Researcher's pricing context)
and emits a C4 System Context (L1) diagram as Mermaid source, plus
supporting docs and a structured component list.

Field split (per spec §5, disambiguated here — REVISED):
    - context_diagram:  raw Mermaid `C4Context` block, exactly what gets
                         base64-urlsafe-encoded and sent to mermaid.ink to
                         render the image. MUST start with the literal
                         string "C4Context". This is what "the diagram" is.
    - diagram_source:   NOT model output. Structured provenance metadata
                         attached by the agent code (not the LLM) after
                         generation: which model produced it, when, against
                         which spec_version, and which prior ADRs (if any)
                         were loaded as context to keep this diagram
                         consistent with settled decisions. Exists so a
                         rejected diagram, a stale diagram, and a
                         re-generated diagram are all distinguishable in
                         the versioned artifact store, independent of
                         docs/components.
    - docs:              prose explanation of the diagram, plain text/markdown.
    - components:        structured list backing the diagram, used by Critic
                         (SPOF / integration gap analysis) and Judge
                         (redundancy_ratio, integration_coverage scoring).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Component(BaseModel):
    """A single actor or system node in the C4 System Context diagram."""

    id: str = Field(..., description="Short stable identifier, e.g. 'pipeline', 'postgres'")
    name: str = Field(..., description="Display name as shown in the diagram")
    type: Literal["person", "internal_system", "external_system"]
    description: str = Field(..., description="One or two sentence role description")
    technology: str | None = Field(
        default=None, description="Optional tech label, e.g. 'PostgreSQL 18', 'Docker'"
    )
    redundant: bool = Field(
        default=False,
        description="Whether this component has a redundant counterpart. "
        "Feeds Judge's redundancy_ratio metric.",
    )


class DiagramProvenance(BaseModel):
    """Provenance metadata for a generated diagram. Set by agent code, never by the LLM."""

    model: str = Field(..., description="Model alias that generated the diagram, e.g. 'gemma-4-e4b-qat'")
    generated_at: datetime = Field(..., description="UTC timestamp of generation")
    spec_version: int | None = Field(
        default=None, description="Spec version this diagram was generated against, if known"
    )
    informed_by_adrs: list[str] = Field(
        default_factory=list,
        description="adr_id values from artifacts/adr/ that were loaded into the prompt "
        "as prior-decision context for this generation. Empty on the first run.",
    )


class ArchitectOutput(BaseModel):
    """Structured output contract for the Architect agent."""

    context_diagram: str = Field(..., description="Raw Mermaid C4Context block — the diagram itself")
    diagram_source: DiagramProvenance = Field(..., description="Provenance metadata, not model output")
    docs: str = Field(..., description="Supporting prose describing the diagram")
    components: list[Component] = Field(default_factory=list)

    @field_validator("context_diagram")
    @classmethod
    def diagram_must_start_with_c4context(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped.startswith("C4Context"):
            raise ValueError(
                "context_diagram must start with literal 'C4Context' "
                f"(got: {stripped[:30]!r}...)"
            )
        return stripped

    @field_validator("components")
    @classmethod
    def components_non_empty(cls, v: list[Component]) -> list[Component]:
        if not v:
            raise ValueError("components must not be empty — mermaid.ink render has nothing to back it")
        return v
