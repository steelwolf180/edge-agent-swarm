"""
agents/judge.py

Judge agent (Gemma 4 E4B QAT). Reads blackboard context, calls the
calculator tool below to score the run against eval/rubric_v1.json's five
metrics (spof_count, redundancy_ratio, cost_per_component,
integration_coverage, adrs_per_diff -- spec §5 Data Flow, §2 Tools per
Agent). Deterministic: Gemma supplies typed inputs, calculate_metrics()
does the arithmetic and threshold comparison. No LLM math, matching the
same code-attached-not-model-output treatment as ArchitectOutput's
diagram_source and ADROutput's diff_hunk_count.

calculate_metrics() is a pure function -- typed Pydantic instances in,
JudgeOutput out, no DB or blackboard I/O inside it -- so it can be smoke
tested the same way Critic and Scribe were (KICKOFF_CHECKLIST.md §6),
without a live Postgres connection.
"""
from __future__ import annotations

import json
from pathlib import Path

from schemas.adr import ADROutput
from schemas.architect import ArchitectOutput
from schemas.critic import CriticOutput
from schemas.judge import JudgeOutput, MetricScore
from schemas.researcher import ResearcherOutput
from schemas.spec import ArchitectureSpec


def load_rubric(path: str = "eval/rubric_v1.json") -> dict:
    """Reads eval/rubric_v1.json at runtime (KICKOFF_CHECKLIST.md §6 gate).
    Returns the "metrics" sub-dict keyed by metric name."""
    with open(path) as f:
        return json.load(f)["metrics"]


def _flag(direction: str, value: float, flag_threshold: float) -> bool:
    """Direction-aware threshold comparison.

    lower_is_better -> flagged if value >= flag_threshold (spof_count: any
    SPOF flags, i.e. >=1; cost_per_component: >=5 flags -- slightly
    stricter than the rubric's "above 5" wording at exactly 5.0, accepted
    as a placeholder tradeoff per rubric's own flag_reason note pending
    system-type-aware thresholds).

    higher_is_better -> flagged if value < flag_threshold (redundancy_ratio,
    integration_coverage, adrs_per_diff: "below X flags", exact match).
    """
    if direction == "lower_is_better":
        return value >= flag_threshold
    return value < flag_threshold


def calculate_metrics(
    researcher: ResearcherOutput,
    architect: ArchitectOutput,
    critic: CriticOutput,
    spec: ArchitectureSpec,
    adr: ADROutput,
    adr_count: int,
    rubric: dict | None = None,
) -> JudgeOutput:
    """Deterministic calculator tool. Judge (Gemma) calls this with typed
    blackboard outputs already on hand -- it does not compute scores itself.

    adr_count: number of ADRs generated for this diff (typically 1, but
    passed explicitly rather than hardcoded in case a future revision cycle
    produces more than one before approval).
    """
    rubric = rubric or load_rubric()

    required_integrations = spec.functional_requirements.integration_points
    total_components = len(architect.components)
    total_cost = sum(item.monthly_cost_usd for item in researcher.pricing)
    diff_hunks = adr.diff_hunk_count

    raw = {
        "spof_count": float(len(critic.spofs)),
        "redundancy_ratio": (
            sum(1 for c in architect.components if c.redundant) / total_components
            if total_components else 0.0
        ),
        "cost_per_component": (total_cost / total_components) if total_components else 0.0,
        "integration_coverage": (
            (len(required_integrations) - len(critic.missing_integrations))
            / len(required_integrations)
            if required_integrations else 1.0
        ),
        "adrs_per_diff": (adr_count / diff_hunks) if diff_hunks else float(adr_count > 0),
    }

    scores: dict[str, MetricScore] = {}
    flagged: list[str] = []

    for name, value in raw.items():
        m = rubric[name]
        is_flagged = _flag(m["direction"], value, m["flag_threshold"])
        scores[name] = MetricScore(
            value=value,
            target=m["target"],
            flag_threshold=m["flag_threshold"],
            direction=m["direction"],
            flagged=is_flagged,
            flag_reason=m["flag_reason"] if is_flagged else None,
        )
        if is_flagged:
            flagged.append(name)

    return JudgeOutput(
        scores=scores,
        cost_estimate=total_cost,
        recommendation="revise" if flagged else "approve",
        flagged_for_review=flagged,
    )
