"""
tests/smoke/test_judge.py

§6 smoke test for the Judge agent's calculator tool. Per
KICKOFF_CHECKLIST.md §6, calculate_metrics() is a pure function -- typed
Pydantic instances in, JudgeOutput out -- so this runs without a live
llama-server, Postgres, or DBOS workflow, same as Critic's standalone test.

Covers the three remaining §6 Judge checkboxes:
    - Calculator tool fires, returns deterministic scores
    - All five metrics present in JudgeOutput.scores
    - Reads thresholds from eval/rubric_v1.json at runtime
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.judge import calculate_metrics, load_rubric
from schemas.adr import ADROutput
from schemas.architect import ArchitectOutput, Component, DiagramProvenance
from schemas.critic import CriticOutput
from schemas.researcher import PricingLineItem, ResearcherOutput
from schemas.spec import (
    ArchitectureSpec,
    DataArchitecture,
    FunctionalRequirements,
    NonFunctionalRequirements,
    ProjectOverview,
    TechnicalConstraints,
)

EXPECTED_METRICS = {
    "spof_count",
    "redundancy_ratio",
    "cost_per_component",
    "integration_coverage",
    "adrs_per_diff",
}


# ---------------------------------------------------------------------------
# Fixtures -- minimal valid instances of each upstream schema
# ---------------------------------------------------------------------------

@pytest.fixture
def spec() -> ArchitectureSpec:
    return ArchitectureSpec(
        project_overview=ProjectOverview(
            purpose="Test system",
            target_users="Solo devs",
            deployment_environment="Edge",
        ),
        functional_requirements=FunctionalRequirements(
            core_features=["diagram generation"],
            key_user_flows=["submit spec"],
            integration_points=["llama-server", "mermaid.ink", "Infracost"],
        ),
        non_functional_requirements=NonFunctionalRequirements(),
        technical_constraints=TechnicalConstraints(),
        data_architecture=DataArchitecture(),
        open_questions=[],
    )


@pytest.fixture
def researcher() -> ResearcherOutput:
    return ResearcherOutput(
        services_identified=["EC2", "RDS"],
        pricing=[
            PricingLineItem(service="EC2", monthly_cost_usd=20.0),
            PricingLineItem(service="RDS", monthly_cost_usd=30.0),
        ],
        pricing_context={"EC2": 20.0, "RDS": 30.0},
        summary="Two services priced.",
        tool_call_succeeded=True,
    )


@pytest.fixture
def architect() -> ArchitectOutput:
    return ArchitectOutput(
        context_diagram="C4Context\n    Person(user, \"User\")\n    System(sys, \"System\")",
        diagram_source=DiagramProvenance(
            model="gemma-4-e4b-qat",
            generated_at=datetime.now(timezone.utc),
            spec_version=1,
            informed_by_adrs=[],
        ),
        docs="System context for the pipeline.",
        components=[
            Component(
                id="pipeline",
                name="Agent Pipeline",
                type="internal_system",
                description="Runs the five-agent sequence.",
                redundant=False,
            ),
            Component(
                id="postgres",
                name="PostgreSQL",
                type="external_system",
                description="Blackboard + artifact store.",
                technology="PostgreSQL 18",
                redundant=True,
            ),
        ],
    )


@pytest.fixture
def researcher_cheap() -> ResearcherOutput:
    """Priced low enough that cost_per_component clears the rubric's
    flag_threshold=5 -- used only by the 'healthy run' test. The shared
    `researcher` fixture ($50 total / 2 components = $25/component) is
    deliberately expensive and used by tests that check flagging behavior."""
    return ResearcherOutput(
        services_identified=["EC2"],
        pricing=[PricingLineItem(service="EC2", monthly_cost_usd=4.0)],
        pricing_context={"EC2": 4.0},
        summary="One cheap service priced.",
        tool_call_succeeded=True,
    )


@pytest.fixture
def critic_clean() -> CriticOutput:
    """No SPOFs, no missing integrations -- a well-specified diagram."""
    return CriticOutput(gaps=[], spofs=[], missing_integrations=[])


@pytest.fixture
def critic_with_gaps() -> CriticOutput:
    """One SPOF, one missing integration -- should trip flags."""
    return CriticOutput(
        gaps=[],
        spofs=["pipeline"],
        missing_integrations=["Infracost"],
    )


@pytest.fixture
def adr_healthy() -> ADROutput:
    """1 ADR generated against a 1-hunk diff -- adrs_per_diff = 1.0, on target."""
    return ADROutput(
        context="Spec added Infracost integration.",
        decision="Adopt Infracost GraphQL stub for pricing.",
        consequences="Adds a Docker dependency.",
        diff_summary="- dictionary_item_added: integration_points[2] -> Infracost",
        diff_hunk_count=1,
        affected_diagrams=["context"],
    )


@pytest.fixture
def adr_under_documented() -> ADROutput:
    """5 hunks, still only 1 ADR -- adrs_per_diff = 0.2, should flag."""
    return ADROutput(
        context="Multiple spec fields changed.",
        decision="Single ADR covering all changes.",
        consequences="Broad decision record.",
        diff_summary="- multiple changes truncated",
        diff_hunk_count=5,
        affected_diagrams=["context"],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_rubric_loads_from_file():
    """eval/rubric_v1.json reads at runtime and has all five metric keys."""
    rubric = load_rubric()
    assert set(rubric.keys()) == EXPECTED_METRICS
    for name, m in rubric.items():
        assert "direction" in m
        assert "target" in m
        assert "flag_threshold" in m
        assert "flag_reason" in m


def test_all_five_metrics_present(spec, researcher, architect, critic_clean, adr_healthy):
    """Calculator tool fires and returns all five metrics, deterministically."""
    result = calculate_metrics(
        researcher=researcher,
        architect=architect,
        critic=critic_clean,
        spec=spec,
        adr=adr_healthy,
        adr_count=1,
    )
    assert set(result.scores.keys()) == EXPECTED_METRICS


def test_healthy_run_is_not_flagged(spec, researcher_cheap, architect, critic_clean, adr_healthy):
    """No SPOFs, full redundancy, full integration coverage, cheap cost,
    1:1 ADR ratio -> nothing should flag, recommendation should be 'approve'.

    Uses researcher_cheap rather than the shared researcher fixture: the
    shared fixture prices at $50 total / 2 components = $25/component, which
    is well above rubric_v1.json's cost_per_component flag_threshold=5 and
    would correctly flag -- that fixture is deliberately expensive for tests
    that check flagging behavior, not a "healthy" baseline."""
    result = calculate_metrics(
        researcher=researcher_cheap,
        architect=architect,
        critic=critic_clean,
        spec=spec,
        adr=adr_healthy,
        adr_count=1,
    )
    assert result.flagged_for_review == []
    assert result.recommendation == "approve"
    assert result.scores["spof_count"].flagged is False
    assert result.scores["adrs_per_diff"].flagged is False
    assert result.scores["cost_per_component"].flagged is False


def test_spof_flags_lower_is_better(spec, researcher, architect, critic_with_gaps, adr_healthy):
    """spof_count: lower_is_better, flag_threshold=1 -> any SPOF flags (value >= 1)."""
    result = calculate_metrics(
        researcher=researcher,
        architect=architect,
        critic=critic_with_gaps,
        spec=spec,
        adr=adr_healthy,
        adr_count=1,
    )
    assert result.scores["spof_count"].value == 1.0
    assert result.scores["spof_count"].flagged is True
    assert result.scores["spof_count"].flag_reason is not None
    assert "revise" == result.recommendation
    assert "spof_count" in result.flagged_for_review


def test_integration_coverage_flags_higher_is_better(
    spec, researcher, architect, critic_with_gaps, adr_healthy
):
    """integration_coverage: higher_is_better, flag_threshold=0.8.
    3 required integrations, 1 missing -> coverage = 2/3 ≈ 0.667, below 0.8 -> flagged."""
    result = calculate_metrics(
        researcher=researcher,
        architect=architect,
        critic=critic_with_gaps,
        spec=spec,
        adr=adr_healthy,
        adr_count=1,
    )
    coverage = result.scores["integration_coverage"]
    assert coverage.value == pytest.approx(2 / 3)
    assert coverage.flagged is True


def test_adrs_per_diff_uses_diff_hunk_count(
    spec, researcher, architect, critic_clean, adr_under_documented
):
    """adrs_per_diff = adr_count / adr.diff_hunk_count. 1 ADR / 5 hunks = 0.2,
    below flag_threshold=1 (higher_is_better) -> flagged."""
    result = calculate_metrics(
        researcher=researcher,
        architect=architect,
        critic=critic_clean,
        spec=spec,
        adr=adr_under_documented,
        adr_count=1,
    )
    adrs_per_diff = result.scores["adrs_per_diff"]
    assert adrs_per_diff.value == pytest.approx(0.2)
    assert adrs_per_diff.flagged is True
    assert "adrs_per_diff" in result.flagged_for_review


def test_cost_estimate_sums_researcher_pricing(spec, researcher, architect, critic_clean, adr_healthy):
    """cost_estimate = sum of ResearcherOutput.pricing line items, independent
    of cost_per_component (which divides by component count)."""
    result = calculate_metrics(
        researcher=researcher,
        architect=architect,
        critic=critic_clean,
        spec=spec,
        adr=adr_healthy,
        adr_count=1,
    )
    assert result.cost_estimate == pytest.approx(50.0)  # 20.0 + 30.0
    # 2 components -> 50.0 / 2 = 25.0 per component
    assert result.scores["cost_per_component"].value == pytest.approx(25.0)


def test_zero_division_guards(spec, researcher, critic_clean, adr_healthy):
    """No components, no required integrations, zero diff hunks -- calculator
    must not raise ZeroDivisionError, and must fall back to sane defaults."""
    architect_no_components_spec_diagram = ArchitectOutput(
        context_diagram="C4Context\n    Person(user, \"User\")",
        diagram_source=DiagramProvenance(
            model="gemma-4-e4b-qat",
            generated_at=datetime.now(timezone.utc),
        ),
        docs="Minimal diagram.",
        components=[
            # ArchitectOutput requires non-empty components (validator), so
            # this exercises total_components > 0 but redundant=False for all
            # -- true zero-component case is structurally impossible upstream,
            # so we only need to guard integration_points and diff_hunks here.
            Component(
                id="sys", name="System", type="internal_system",
                description="Single node.", redundant=False,
            )
        ],
    )
    spec_no_integrations = spec.model_copy(deep=True)
    spec_no_integrations.functional_requirements.integration_points = []

    adr_zero_hunks = adr_healthy.model_copy(update={"diff_hunk_count": 0})

    result = calculate_metrics(
        researcher=researcher,
        architect=architect_no_components_spec_diagram,
        critic=critic_clean,
        spec=spec_no_integrations,
        adr=adr_zero_hunks,
        adr_count=1,
    )
    # integration_points empty -> coverage defaults to 1.0 (nothing required, nothing missing)
    assert result.scores["integration_coverage"].value == 1.0
    # diff_hunks == 0 -> falls back to float(adr_count > 0) == 1.0
    assert result.scores["adrs_per_diff"].value == 1.0


def test_explicit_rubric_override(spec, researcher, architect, critic_clean, adr_healthy):
    """Passing a rubric dict directly bypasses load_rubric() -- useful for
    testing rubric changes without touching eval/rubric_v1.json."""
    strict_rubric = load_rubric()
    strict_rubric = {**strict_rubric}
    strict_rubric["cost_per_component"] = {
        **strict_rubric["cost_per_component"],
        "flag_threshold": 1,  # much stricter than default 5
    }
    result = calculate_metrics(
        researcher=researcher,
        architect=architect,
        critic=critic_clean,
        spec=spec,
        adr=adr_healthy,
        adr_count=1,
        rubric=strict_rubric,
    )
    # 25.0/component >= 1 under the stricter override -> now flags
    assert result.scores["cost_per_component"].flagged is True
