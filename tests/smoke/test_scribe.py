"""Smoke test for the Scribe agent -- run against a live llama-server (LFM loaded).

    conda activate swarm
    python tests/smoke/test_scribe.py

Checks the three unchecked KICKOFF_CHECKLIST.md §6 Scribe items:
  1. deepdiff on model_dump() produces diff input
  2. ADROutput validates
  3. affected_diagrams restricted to 'context' only for MVP
"""
import asyncio

from agents.scribe import (
    ADROutput,
    _detect_diff_bullet_echo,
    compute_spec_diff,
    run_scribe,
    summarize_creation_diff,
)
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


# ---------------------------------------------------------------------------
# Unit tests for the key_user_flows-exclusion and diff-bullet-echo fixes
# (21 Aug 2026). Pure functions -- no llama-server call, no asyncio needed --
# regression fixtures for two confirmed-live findings rather than synthetic
# edge cases, same spirit as tests/smoke/test_critic.py's non-live tests.
# Run alongside the rest of the suite (pytest -v tests/smoke/test_scribe.py);
# these don't need ./scripts/start_llama_router.sh running.
# ---------------------------------------------------------------------------

_FLOW_TEXT = (
    "Customer asks a question -> Retrieval Service fetches chunks -> "
    "Generation Service answers -> customer rates it"
)


def _rag_style_spec(key_user_flows: list[str] | None = None) -> ArchitectureSpec:
    """A cloud_rag.json-shaped creation-diff spec, small enough to keep
    these tests fast but structurally the same shape as the real spec
    that surfaced both bugs these tests guard against."""
    return ArchitectureSpec(
        project_overview=ProjectOverview(
            purpose="Answer support questions with retrieval-augmented generation.",
            target_users="Customers and support agents",
            deployment_environment="Cloud, AWS, single region",
        ),
        functional_requirements=FunctionalRequirements(
            core_features=["Ingestion Service", "Retrieval Service"],
            **({"key_user_flows": key_user_flows} if key_user_flows else {}),
        ),
        non_functional_requirements=NonFunctionalRequirements(security="Encrypted at rest"),
        technical_constraints=TechnicalConstraints(language_framework=["Python"]),
        data_architecture=DataArchitecture(storage_requirements="Postgres + vector store"),
    )


def test_summarize_creation_diff_counts_key_user_flows_without_leaking_it():
    """Regression fixture for the key_user_flows copy-bait fix. Two prior
    fix attempts on this exact field failed identically (workflow
    7e7b0948-...: instruction-level; workflow 8b89bcad-...: structural
    terse-labeling) -- the current fix excludes the field from the prompt
    bullets entirely rather than reshaping it a third time. hunk_count
    must still reflect the change (it's real, and Judge's adrs_per_diff
    metric depends on an accurate count) even though the prompt text
    never sees it.

    Compares against an otherwise-identical spec with no key_user_flows,
    so this doesn't depend on hard-coding the hunk count for every other
    section -- only the delta and the leak (or lack of one) matter here.
    """
    spec_without_flows = _rag_style_spec()
    spec_with_flows = _rag_style_spec(key_user_flows=[_FLOW_TEXT])

    summary_without, hunk_without = summarize_creation_diff(spec_without_flows)
    summary_with, hunk_with = summarize_creation_diff(spec_with_flows)

    assert hunk_with == hunk_without + 1, "key_user_flows change must still be counted"
    assert "->" not in summary_with, "key_user_flows' narrative chain leaked into the prompt"
    assert "Customer asks a question" not in summary_with, "key_user_flows content leaked into the prompt"
    assert summary_with == summary_without, (
        "the prompt text Scribe actually sees must be identical whether or "
        "not key_user_flows is populated -- it's excluded entirely, not "
        "just shortened or reformatted"
    )


def test_detect_diff_bullet_echo_catches_real_verbatim_copy():
    """Regression fixture for workflow a9b0d6df-... (21 Aug 2026, surfaced
    right after the key_user_flows fix landed): with the usual copy-bait
    target removed, the model reached for the next-easiest one instead --
    the shortest bullet in the diff, echoed almost character-for-character
    into both 'decision' and 'diff_summary'. Neither _detect_diff_dump()
    (too short to trip the length/word-count thresholds) nor
    _detect_ungrounded_content() (too much real overlap to look like
    fabrication) would have caught this -- built from a real
    summarize_creation_diff() call, not a hand-typed diff string, so this
    exercises the actual bullet format Scribe's prompt receives."""
    spec = _rag_style_spec()
    diff_summary, _ = summarize_creation_diff(spec)
    project_overview_bullet = diff_summary.splitlines()[0]
    assert project_overview_bullet.startswith("Added project_overview:")

    parsed_bad = {
        "decision": project_overview_bullet,
        "consequences": "Define the project's purpose, target users, and deployment environment.",
        "diff_summary": project_overview_bullet + ".",
    }

    flagged = _detect_diff_bullet_echo(parsed_bad, diff_summary)

    assert "decision" in flagged
    assert "diff_summary" in flagged
    assert "consequences" not in flagged, "legitimate paraphrase must not be flagged"


def test_detect_diff_bullet_echo_no_false_positive_on_paraphrase():
    """A genuinely paraphrased ADR -- reworded, not restructured-but-
    verbatim -- must not be flagged. A guard that fires on correct output
    is at least as harmful as one that misses bad output."""
    spec = _rag_style_spec()
    diff_summary, _ = summarize_creation_diff(spec)

    parsed_clean = {
        "decision": (
            "Define the system's purpose, target users, and deployment "
            "target as an AWS-hosted support RAG system."
        ),
        "consequences": "The project now has an explicit scope and hosting target.",
        "diff_summary": "Established the baseline project overview and core services.",
    }

    flagged = _detect_diff_bullet_echo(parsed_clean, diff_summary)
    assert flagged == []


def test_detect_diff_bullet_echo_skips_legitimate_zero_diff():
    """Example 2's correct zero-diff boilerplate must never be flagged --
    already covered by _detect_example_copying()'s own zero-diff branch,
    but this guard has an independent early-return for the same case and
    needs its own regression coverage rather than relying on that."""
    diff_summary = "No field-level changes detected."
    parsed = {
        "decision": "No meaningful decision to record: no spec changes were detected in this run.",
        "consequences": "None. No architectural change occurred, so no consequence follows.",
        "diff_summary": "No field-level changes detected.",
    }

    flagged = _detect_diff_bullet_echo(parsed, diff_summary)
    assert flagged == []


if __name__ == "__main__":
    # Direct-run path (no pytest / pytest-asyncio needed):
    #   conda activate swarm && python tests/smoke/test_scribe.py
    asyncio.run(test_scribe_end_to_end())