"""
schemas/judge.py

Pydantic v2 models for the Judge agent (Gemma 4 E4B QAT).

Judge reads all blackboard context (spec §5: "summarised to 200 tok/agent
max") and calls the calculator tool (agents/judge.py:calculate_metrics) to
score the run against eval/rubric_v1.json. Scoring itself is deterministic
Python, not LLM arithmetic -- Gemma supplies the typed inputs via tool call,
the tool computes flags and returns JudgeOutput. This mirrors Researcher's
tool-calling pattern (Infracost) rather than Critic/Scribe's pure-text
generation (spec §2 Tools per Agent).

MetricScore vs. the spec's literal `scores: dict[str, float]` (§5 Pydantic
Models): deliberately widened here so the rubric comparison is explicit and
auditable in the output itself, rather than something recomputed downstream
or left implicit in a bare float. Every value carries its own target,
threshold, direction, and flag state -- a reviewer (or the /review page,
v1.1) can render pass/fail without re-reading rubric_v1.json.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MetricScore(BaseModel):
    """One scored metric, rubric-aware."""

    value: float = Field(..., description="Computed value for this run")
    target: float = Field(..., description="rubric_v1.json target for this metric")
    flag_threshold: float = Field(..., description="rubric_v1.json flag_threshold")
    direction: Literal["lower_is_better", "higher_is_better"]
    flagged: bool = Field(..., description="True if value crosses flag_threshold per direction")
    flag_reason: str | None = Field(
        default=None,
        description="rubric_v1.json flag_reason, populated only when flagged=True",
    )


class JudgeOutput(BaseModel):
    """
    Structured output contract for the Judge agent.

    recommendation is advisory, not binding -- Judge flags, it doesn't
    decide. The human still approves/rejects via the CLI (spec §2 Key User
    Flows); "reject" is deliberately not a value here since that action
    belongs to the human, not Judge. "revise" if anything is flagged,
    "approve" if nothing is.
    """

    scores: dict[str, MetricScore] = Field(
        ...,
        description=(
            "Keys match rubric_v1.json exactly: spof_count, redundancy_ratio, "
            "cost_per_component, integration_coverage, adrs_per_diff"
        ),
    )
    cost_estimate: float = Field(
        ..., description="Total monthly cost across all ResearcherOutput.pricing line items (USD)"
    )
    recommendation: Literal["approve", "revise"]
    flagged_for_review: list[str] = Field(
        default_factory=list, description="Metric names where flagged=True"
    )
