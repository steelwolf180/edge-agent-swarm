"""
ResearcherOutput schema.

Matches spec §5 (Pydantic Models) and the §6 checklist gates for the
Researcher agent:
    - Infracost GraphQL stub call validates
    - Output parses into ResearcherOutput
    - Pricing context written to blackboard via DBOS.set_event(...)

Researcher is Gemma-backed, tool-calling enabled (Infracost only).
Context budget per spec §5: ~600 tokens for this agent.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PricingLineItem(BaseModel):
    """One priced service line returned by the Infracost tool call."""

    service: str = Field(..., description="Cloud service identifier, e.g. 'EC2', 'RDS'")
    provider: str = Field(default="aws", description="Cloud provider slug")
    monthly_cost_usd: float = Field(..., ge=0.0)
    notes: str | None = Field(default=None, description="Assumptions used for the estimate")


class ResearcherOutput(BaseModel):
    """
    Structured output of the Researcher agent.

    `pricing_context` is what gets written to the DBOS blackboard
    (BlackboardState.pricing_context) for downstream agents, most
    directly the Judge's cost_per_component metric.
    """

    services_identified: list[str] = Field(
        default_factory=list,
        description="Services extracted from the spec that were queried against Infracost",
    )
    pricing: list[PricingLineItem] = Field(default_factory=list)
    pricing_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Flattened dict form written verbatim to the blackboard",
    )
    summary: str = Field(
        ...,
        max_length=3000,  # rough ceiling, not a hard token count
        description="Short enrichment note for downstream agents (~600 token budget)",
    )
    tool_call_succeeded: bool = Field(
        default=False,
        description="False if Infracost call failed/stubbed-out with no data",
    )

    def to_blackboard_payload(self) -> dict[str, Any]:
        """Shape written via DBOS.set_event('pricing_context', ...)."""
        return {
            "services_identified": self.services_identified,
            "pricing": [item.model_dump() for item in self.pricing],
            "pricing_context": self.pricing_context,
            "summary": self.summary,
        }