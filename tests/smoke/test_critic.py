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

from agents.critic import (
    run_critic,
    _already_flagged,
    _flag_cross_field_duplication,
    _flag_duplicate_list_items,
    _flag_example_copying,
    _flag_example_domain_leak,
    _salvage_malformed_critic_output,
    _flag_missing_integrations_without_gap_language,
    _flag_fabricated_component_references,
    _attempt_regeneration_pass,
    _regenerate_duplicate_entry,
    _still_duplicate,
    _strip_flag_prefix,
    _DUPLICATE_FLAG_PREFIX,
    _CROSS_FIELD_FLAG_PREFIX,
    _EXAMPLE_COPY_FLAG_PREFIX,
)
from schemas.adr import ADROutput
from schemas.architect import ArchitectOutput, Component, DiagramProvenance
from schemas.critic import CriticOutput

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
    diff_hunk_count=1,
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


# ---------------------------------------------------------------------------
# Unit tests for the guard-wiring and malformed-JSON salvage fix
# (21 Aug 2026). These are pure functions -- no llama-server call, no
# @pytest.mark.asyncio needed -- and are regression fixtures for two
# confirmed-live failures rather than synthetic edge cases: workflow
# a9b0d6df-... (spofs 3x identical, detected but never flagged in the
# persisted output -- the guard-wiring bug) and a real test_critic.py
# run that hit malformed JSON from a missing delimiter between two gap
# objects (the salvage-machinery gap). Run these with the rest of the
# suite (pytest -v tests/smoke/test_critic.py); they don't need
# ./scripts/start_llama_router.sh running.
# ---------------------------------------------------------------------------


def test_flag_duplicate_list_items_flags_every_occurrence():
    """Regression fixture for workflow a9b0d6df-...: spofs came back as
    the same string 3x. Pre-fix, _detect_duplicate_list_items() correctly
    logged a warning but never touched the returned CriticOutput, so all
    three reached the persisted review doc unflagged. This asserts the
    fixed version both flags the field AND mutates every occurrence, not
    just the first duplicate found."""
    parsed = {
        "gaps": [],
        "spofs": [
            "RAG System interfaces with several external document sources "
            "and APIs (Embedding and LLM)",
            "RAG System interfaces with several external document sources "
            "and APIs (Embedding and LLM)",
            "RAG System interfaces with several external document sources "
            "and APIs (Embedding and LLM)",
        ],
        "missing_integrations": [
            "RAG System interacts with the LLM API for generating grounded answers",
        ],
    }

    flagged_fields = _flag_duplicate_list_items(parsed)

    assert flagged_fields == ["spofs"]
    assert len(parsed["spofs"]) == 3
    for item in parsed["spofs"]:
        assert item.startswith("POSSIBLE DUPLICATE -- FLAG FOR HUMAN REVIEW: ")
    # missing_integrations has no duplicates -- must be untouched.
    assert parsed["missing_integrations"] == [
        "RAG System interacts with the LLM API for generating grounded answers",
    ]

    # The mutated dict must still validate against the real schema, the
    # same shape run_critic() would hand to CriticOutput.model_validate().
    CriticOutput.model_validate(parsed)


def test_flag_duplicate_list_items_leaves_clean_output_untouched():
    """No duplicates anywhere -- every guard should be a no-op, byte-for-
    byte. A guard that mutates content it shouldn't is at least as bad as
    one that misses content it should catch."""
    parsed = {
        "gaps": [
            {"description": "No cache layer for hot reads.", "severity": "low", "related_component": None},
        ],
        "spofs": ["Single instance of the ingestion service, no failover."],
        "missing_integrations": ["Slack alerting is referenced in docs but not declared."],
    }
    import copy
    original = copy.deepcopy(parsed)

    flagged = _flag_duplicate_list_items(parsed)

    assert flagged == []
    assert parsed == original


def test_flag_duplicate_list_items_is_idempotent():
    """Running the guard twice (e.g. if run_critic() were ever called
    again on already-flagged output) must not double-wrap an entry --
    _already_flagged() is what prevents that."""
    parsed = {"gaps": [], "spofs": ["same thing", "same thing"], "missing_integrations": []}

    _flag_duplicate_list_items(parsed)
    _flag_duplicate_list_items(parsed)

    for item in parsed["spofs"]:
        assert item.count("POSSIBLE DUPLICATE") == 1
        assert _already_flagged(item)


def test_flag_cross_field_duplication_flags_exact_overlap():
    """spofs and missing_integrations sharing an identical entry means the
    model treated them as the same list twice -- both sides should be
    flagged, and gaps that restate either entry as a full sentence should
    be flagged too."""
    parsed = {
        "gaps": [
            {
                "description": "Database instance is a single point of failure with no redundancy.",
                "severity": "high",
                "related_component": "postgres",
            },
        ],
        "spofs": ["Database instance is a single point of failure with no redundancy."],
        "missing_integrations": ["Database instance is a single point of failure with no redundancy."],
    }

    flagged = _flag_cross_field_duplication(parsed)

    assert set(flagged) == {"spofs", "missing_integrations", "gaps"}
    assert parsed["spofs"][0].startswith("POSSIBLE CROSS-FIELD DUPLICATE")
    assert parsed["missing_integrations"][0].startswith("POSSIBLE CROSS-FIELD DUPLICATE")
    assert parsed["gaps"][0]["description"].startswith("POSSIBLE CROSS-FIELD DUPLICATE")

    CriticOutput.model_validate(parsed)


def test_flag_example_copying_and_domain_leak_catch_worked_example_text():
    """Confirmed live (KICKOFF_CHECKLIST.md, 21 Aug 2026): CRITIC_SYSTEM_
    PROMPT's OrderService/NotificationService worked example leaked into
    real output on 2 of 5 sampled runs. Both the fuzzy-match guard (exact/
    near-verbatim text) and the domain-token guard (reworded but still
    containing a fictional-domain proper noun) need their own regression
    coverage."""
    exact_copy = {
        "gaps": [],
        "spofs": ["OrderService is a single instance handling both critical paths with no stated redundancy."],
        "missing_integrations": [],
    }
    copied = _flag_example_copying(exact_copy)
    assert copied == ["spofs"]
    assert exact_copy["spofs"][0].startswith("POSSIBLE EXAMPLE COPY -- FLAG FOR HUMAN REVIEW: ")

    reworded_leak = {
        "gaps": [
            {
                "description": "Our RAG System, much like OrderService, has no stated redundancy.",
                "severity": "medium",
                "related_component": None,
            }
        ],
        "spofs": [],
        "missing_integrations": [],
    }
    leaked = _flag_example_domain_leak(reworded_leak)
    assert leaked == ["gaps"]
    assert reworded_leak["gaps"][0]["description"].startswith(
        "POSSIBLE EXAMPLE COPY (fictional-domain term) -- FLAG FOR HUMAN REVIEW: "
    )


def test_salvage_malformed_critic_output_recovers_gaps_missing_delimiter():
    """Regression fixture for the real failure this test file hit live:
    Critic returned a 'gaps' array with a missing comma between two
    otherwise well-formed objects, which broke json.loads() on the whole
    document even though every individual field was fine. Confirms both
    gap objects are recovered, flagged, and spofs/missing_integrations
    (unaffected by the malformed span) pass through untouched."""
    raw = (
        '{"gaps": [{"description": "The diagram does not show any external systems '
        'or integrations beyond the specified components and blackboard.", '
        '"severity": "high", "related_component": "postgres"} '
        '{"description": "No caching layer mentioned for repeated queries.", '
        '"severity": "medium", "related_component": null}], '
        '"spofs": ["Single PostgreSQL instance handles all state with no stated redundancy."], '
        '"missing_integrations": ["No integration shown for the Researcher pricing context in the blackboard."]}'
    )

    result = _salvage_malformed_critic_output(raw)

    assert len(result["gaps"]) == 2
    for gap in result["gaps"]:
        assert gap["description"].startswith("SALVAGED (recovered from malformed JSON) -- FLAG FOR HUMAN REVIEW: ")
        assert gap["severity"] in ("low", "medium", "high")
    assert result["spofs"] == [
        "Single PostgreSQL instance handles all state with no stated redundancy."
    ]
    assert result["missing_integrations"] == [
        "No integration shown for the Researcher pricing context in the blackboard."
    ]

    # Must be a valid CriticOutput on its own, same as what run_critic()
    # would hand to CriticOutput.model_validate() after this salvage path.
    CriticOutput.model_validate(result)


def test_salvage_malformed_critic_output_drops_unrecoverable_gap():
    """One gap object is well-formed, the other is genuinely broken
    (unterminated, stray quote). The broken one must be DROPPED, never
    fabricated a description for -- salvage recovers real content, it
    doesn't invent plausible-sounding content the model never produced."""
    raw = (
        '{"gaps": [{"description": "Fine gap here.", "severity": "low", "related_component": null}, '
        '{"description": "This one never closes properly and has a stray quote " inside it'
        '], "spofs": [], "missing_integrations": ["Some integration."]}'
    )

    result = _salvage_malformed_critic_output(raw)

    assert len(result["gaps"]) == 1
    assert result["gaps"][0]["description"] == (
        "SALVAGED (recovered from malformed JSON) -- FLAG FOR HUMAN REVIEW: Fine gap here."
    )
    assert result["spofs"] == []
    assert result["missing_integrations"] == ["Some integration."]

    CriticOutput.model_validate(result)
    

def test_flag_missing_integrations_without_gap_language_catches_restatements():
    """Confirms on workflow ae941ea4-... (26 Aug 2026): all three
    missing_integrations entries were pure restatements of an existing
    integration ('RAG System interacts with X to do Y'), never asserting
    anything was missing/undocumented/unhandled. Two of three happened to
    also trip _flag_diagram_relationship_echo() via Rel()-token overlap;
    the third ('Git-hosted docs repo... versioned documentation') echoed
    the Scribe diff's integration_points bullet instead of a Rel() edge
    and went unflagged by that guard. This guard catches all three via a
    different mechanism (absence of gap-indicating vocabulary), confirming
    it targets the shared root symptom rather than patching one guard's
    anchor set."""
    parsed = {
        "missing_integrations": [
            "RAG System interacts with Confluence API to retrieve updated documentation",
            "RAG System interacts with Git-hosted docs repo to pull versioned documentation",
            "RAG System interacts with Hosted embedding model API to use pre-trained vector embeddings",
        ]
    }
    flagged_fields = _flag_missing_integrations_without_gap_language(parsed)
 
    assert flagged_fields == ["missing_integrations"]
    for item in parsed["missing_integrations"]:
        assert item.startswith("POSSIBLE RESTATEMENT, NOT A GAP -- FLAG FOR HUMAN REVIEW: ")
 
 
def test_flag_missing_integrations_without_gap_language_no_false_positive_on_real_gaps():
    """Legitimate missing_integrations entries -- ones that actually assert
    something is absent/undocumented/unmodeled -- must pass through
    unflagged. This is the control case for the false-positive class this
    guard was deliberately designed to avoid (see the module-level
    docstring on _GAP_LANGUAGE_VOCAB re: why overlap-with-source was
    rejected in favor of this vocabulary-presence approach)."""
    parsed = {
        "missing_integrations": [
            "Zendesk API is referenced in the spec for ticket escalation but is not shown as a System_Ext in the diagram",
            "No explicit integration with the Admin Console is modeled despite the spec requiring docs-team review access",
        ]
    }
    flagged_fields = _flag_missing_integrations_without_gap_language(parsed)
 
    assert flagged_fields == []
    assert parsed["missing_integrations"] == [
        "Zendesk API is referenced in the spec for ticket escalation but is not shown as a System_Ext in the diagram",
        "No explicit integration with the Admin Console is modeled despite the spec requiring docs-team review access",
    ]
 
def test_flag_missing_integrations_without_gap_language_idempotent():
    """Running the guard twice must not double-prefix an already-flagged
    entry -- same _already_flagged() contract every other guard in this
    module follows."""
    parsed = {
        "missing_integrations": [
            "RAG System interacts with Confluence API to retrieve updated documentation",
        ]
    }
    _flag_missing_integrations_without_gap_language(parsed)
    after_first_pass = list(parsed["missing_integrations"])
 
    _flag_missing_integrations_without_gap_language(parsed)
    after_second_pass = parsed["missing_integrations"]
 
    assert after_first_pass == after_second_pass
 
 
def test_flag_missing_integrations_without_gap_language_empty_field_noop():
    """Empty/missing missing_integrations field should be a no-op, not an
    error -- same contract as the other list-valued guards in this
    module."""
    parsed = {"missing_integrations": []}
    flagged_fields = _flag_missing_integrations_without_gap_language(parsed)
 
    assert flagged_fields == []
    assert parsed["missing_integrations"] == []
 
    parsed_missing_key = {}
    flagged_fields2 = _flag_missing_integrations_without_gap_language(parsed_missing_key)
    assert flagged_fields2 == []


# ---------------------------------------------------------------------------
# Unit tests for the fabricated-component guard (26 Aug 2026)
# ---------------------------------------------------------------------------
# Regression fixture for workflow 3ae9e752-...: gaps/spofs/missing_
# integrations all referenced a "notification_service" component that
# does not exist anywhere -- not in the spec, not in architect_output.
# components, not in the diagram. Pure function, no llama-server call
# needed, same as the other guard unit tests above.

WEAK_COMPONENTS = WEAK_ARCHITECT_OUTPUT.components  # [postgres]


def test_flag_fabricated_component_references_catches_snake_and_title_case():
    """Regression fixture for workflow 3ae9e752-...: the same fabricated
    entity appeared as 'notification_service' (snake_case, in gaps/
    missing_integrations) and 'Notification Service' (Title Case, in
    spofs). Both forms must be caught and normalized-matched the same way."""
    parsed = {
        "gaps": [
            {
                "description": (
                    "The diagram does not show the interaction between the "
                    "user and the notification_service, nor does it indicate "
                    "how the user's local data is accessed by the "
                    "notification_service for sending notifications."
                ),
                "severity": "medium",
                "related_component": "notification_service",
            }
        ],
        "spofs": [
            "Notification Service is a single service handling notification "
            "tasks, but its role is not explicitly shown in the diagram.",
        ],
        "missing_integrations": [],
    }

    flagged_fields = _flag_fabricated_component_references(parsed, WEAK_COMPONENTS)

    assert set(flagged_fields) == {"gaps", "spofs"}
    assert parsed["gaps"][0]["description"].startswith(
        "POSSIBLE FABRICATED COMPONENT (notification service not present in diagram) -- FLAG FOR HUMAN REVIEW: "
    )
    assert parsed["spofs"][0].startswith(
        "POSSIBLE FABRICATED COMPONENT (notification service not present in diagram) -- FLAG FOR HUMAN REVIEW: "
    )


def test_flag_fabricated_component_references_no_false_positive_on_real_components():
    """Legitimate entries referencing real components -- including ones
    reworded from the component's id (snake_case) into prose -- must pass
    through unflagged. Control case for the guard, same role as the
    missing_integrations false-positive control above."""
    parsed = {
        "gaps": [
            {
                "description": "PostgreSQL is not shown handling concurrent writes from multiple agents.",
                "severity": "low",
                "related_component": "postgres",
            }
        ],
        "spofs": [
            "PostgreSQL is a single instance with no stated redundancy or failover.",
        ],
        "missing_integrations": [],
    }

    flagged_fields = _flag_fabricated_component_references(parsed, WEAK_COMPONENTS)

    assert flagged_fields == []
    assert parsed["gaps"][0]["description"] == (
        "PostgreSQL is not shown handling concurrent writes from multiple agents."
    )
    assert parsed["spofs"][0] == (
        "PostgreSQL is a single instance with no stated redundancy or failover."
    )


def test_flag_fabricated_component_references_idempotent():
    """Running the guard twice must not double-prefix -- same
    _already_flagged() contract every other guard in this module follows."""
    parsed = {
        "gaps": [],
        "spofs": [
            "Notification Service is a single point of failure not shown in the diagram.",
        ],
        "missing_integrations": [],
    }
    _flag_fabricated_component_references(parsed, WEAK_COMPONENTS)
    after_first_pass = list(parsed["spofs"])

    _flag_fabricated_component_references(parsed, WEAK_COMPONENTS)
    after_second_pass = parsed["spofs"]

    assert after_first_pass == after_second_pass


def test_flag_fabricated_component_references_empty_fields_noop():
    """Empty/missing fields should be a no-op, not an error."""
    parsed = {"gaps": [], "spofs": [], "missing_integrations": []}
    flagged_fields = _flag_fabricated_component_references(parsed, WEAK_COMPONENTS)
    assert flagged_fields == []

    parsed_missing_keys = {}
    flagged_fields2 = _flag_fabricated_component_references(parsed_missing_keys, WEAK_COMPONENTS)
    assert flagged_fields2 == []


# ---------------------------------------------------------------------------
# Unit tests for bounded duplicate regeneration (26 Aug 2026)
# ---------------------------------------------------------------------------
# These mock httpx.AsyncClient rather than requiring a live llama-server --
# unlike test_critic_flags_weak_spec() above, the point here is testing the
# accept/reject logic around the call (still-duplicate check, NO_DISTINCT_
# FINDING handling, scope restricted to duplicate-class prefixes), not LFM's
# actual output quality. A real run against openrouter_rag.json or similar
# is still the right way to confirm 0.15 produces genuinely distinct output
# in practice -- these tests only confirm the mechanism behaves correctly
# given a known model response.


def _mock_llm_client(mocker, content: str, finish_reason: str = "stop", status_code: int = 200):
    """Builds a mock matching the `async with httpx.AsyncClient(...) as client:
    response = await client.post(...)` shape used by _regenerate_duplicate_entry.
    Returns (context_manager, client) -- the client is returned separately
    so tests can inspect client.post.call_args to assert on what was
    actually sent, not just what came back.

    Uses mocker.MagicMock/mocker.AsyncMock rather than importing from
    unittest.mock directly -- pytest-mock's mocker fixture exposes these
    as the same underlying classes, so no separate import is needed in a
    pytest-mock-based suite."""
    mock_response = mocker.MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}]
    }

    mock_client = mocker.MagicMock()
    mock_client.post = mocker.AsyncMock(return_value=mock_response)

    mock_client_cm = mocker.MagicMock()
    mock_client_cm.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client_cm.__aexit__ = mocker.AsyncMock(return_value=False)

    return mock_client_cm, mock_client


@pytest.mark.asyncio
async def test_regenerate_duplicate_entry_accepts_genuinely_distinct_candidate(mocker):
    """A regeneration call that returns a candidate sharing no real overlap
    with the sibling entries should be accepted -- returns the clean new
    text (caller is responsible for the flag-prefix already having been
    stripped before display). Also confirms the actual payload sent used
    the corrective-context prompt (siblings included, flagged text with
    its review-facing prefix stripped, temperature=0.15) rather than just
    asserting some call happened."""
    flagged = f"{_DUPLICATE_FLAG_PREFIX}RAG System interacts with several external document sources and APIs."
    siblings = [
        "RAG System interacts with the LLM API for generating grounded answers.",
    ]
    mock_cm, mock_client = _mock_llm_client(
        mocker,
        "The Vector Store has no described backup or snapshot strategy, risking permanent data loss on failure."
    )
    mocker.patch("agents.critic.httpx.AsyncClient", return_value=mock_cm)

    result = await _regenerate_duplicate_entry("spofs", flagged, siblings, WEAK_ARCHITECT_OUTPUT)

    assert result == (
        "The Vector Store has no described backup or snapshot strategy, "
        "risking permanent data loss on failure."
    )

    mock_client.post.assert_called_once()
    _, kwargs = mock_client.post.call_args
    payload = kwargs["json"]
    assert payload["temperature"] == 0.15

    prompt_text = payload["messages"][-1]["content"]
    # flagged text sent with its review-facing prefix stripped, not raw
    assert "RAG System interacts with several external document sources and APIs." in prompt_text
    assert _DUPLICATE_FLAG_PREFIX not in prompt_text
    # sibling entries included as "already covered" corrective context
    assert siblings[0] in prompt_text
    # diagram context included, confirming this isn't a resend of the
    # original critique prompt but a distinct, corrective one
    assert WEAK_ARCHITECT_OUTPUT.context_diagram in prompt_text


@pytest.mark.asyncio
async def test_regenerate_duplicate_entry_rejects_still_duplicate_candidate(mocker):
    """A regeneration call that returns something still near-identical to a
    sibling must be rejected (returns None), so the caller falls back to
    the existing FLAG-FOR-REVIEW text rather than silently swapping one
    duplicate for another."""
    flagged = f"{_DUPLICATE_FLAG_PREFIX}RAG System interacts with several external document sources and APIs."
    siblings = [
        "RAG System interacts with the LLM API for generating grounded answers.",
    ]
    # Near-verbatim restatement of the sibling -- should score above the
    # 0.75 still-duplicate threshold.
    mock_cm, _ = _mock_llm_client(
        mocker,
        "RAG System interacts with the LLM API to generate grounded answers."
    )
    mocker.patch("agents.critic.httpx.AsyncClient", return_value=mock_cm)

    result = await _regenerate_duplicate_entry("spofs", flagged, siblings, WEAK_ARCHITECT_OUTPUT)

    assert result is None



@pytest.mark.asyncio
async def test_regenerate_duplicate_entry_handles_no_distinct_finding(mocker):
    """Model explicitly declining (NO_DISTINCT_FINDING) must fall back to
    None, not be treated as literal replacement text."""
    flagged = f"{_CROSS_FIELD_FLAG_PREFIX}Some duplicated finding."
    mock_cm, _ = _mock_llm_client(mocker, "NO_DISTINCT_FINDING")
    mocker.patch("agents.critic.httpx.AsyncClient", return_value=mock_cm)

    result = await _regenerate_duplicate_entry("gaps", flagged, [], WEAK_ARCHITECT_OUTPUT)

    assert result is None


@pytest.mark.asyncio
async def test_regenerate_duplicate_entry_handles_truncation_and_http_error(mocker):
    """finish_reason == 'length' and a non-2xx status must both fall back
    to None rather than raising -- a failed regeneration attempt should
    never take down the rest of the guard pass."""
    truncated_cm, _ = _mock_llm_client(mocker, "The Vector Store has no", finish_reason="length")
    mocker.patch("agents.critic.httpx.AsyncClient", return_value=truncated_cm)
    result = await _regenerate_duplicate_entry("spofs", "flagged text", [], WEAK_ARCHITECT_OUTPUT)
    assert result is None

    error_cm, _ = _mock_llm_client(mocker, "", status_code=500)
    mocker.patch("agents.critic.httpx.AsyncClient", return_value=error_cm)
    result2 = await _regenerate_duplicate_entry("spofs", "flagged text", [], WEAK_ARCHITECT_OUTPUT)
    assert result2 is None


@pytest.mark.asyncio
async def test_attempt_regeneration_pass_only_touches_duplicate_class_flags(mocker):
    """Scope check: entries flagged by non-duplicate-class guards (example-
    copy, domain-leak, echo, restatement, fabricated-component) must be
    left completely untouched by the regeneration pass, even if a
    duplicate-class entry elsewhere in the same output triggers a real
    regeneration call. Confirms the pass doesn't over-reach into failure
    classes it hasn't been validated against."""
    parsed = {
        "gaps": [],
        "spofs": [
            f"{_DUPLICATE_FLAG_PREFIX}RAG System interacts with several external document sources and APIs.",
            f"{_DUPLICATE_FLAG_PREFIX}RAG System interacts with several external document sources and APIs.",
        ],
        "missing_integrations": [
            f"{_EXAMPLE_COPY_FLAG_PREFIX}OrderService is a single instance handling both critical paths with no stated redundancy.",
        ],
    }
    mock_cm, _ = _mock_llm_client(
        mocker,
        "The Embedding API has no documented rate-limit handling, risking silent ingestion failures under burst load."
    )
    mocker.patch("agents.critic.httpx.AsyncClient", return_value=mock_cm)

    regenerated = await _attempt_regeneration_pass(parsed, WEAK_ARCHITECT_OUTPUT)

    # The example-copy-flagged missing_integrations entry must be untouched.
    assert parsed["missing_integrations"][0].startswith(_EXAMPLE_COPY_FLAG_PREFIX)
    # At least one of the duplicate-class spofs entries should have been
    # attempted (mocked to succeed identically each call, so both could
    # regenerate to the same text -- that's a known limitation of mocking
    # a single fixed response, not something this test needs to resolve).
    assert regenerated, "expected at least one duplicate-class entry to be regenerated"
    assert all(r.startswith("spofs[") for r in regenerated)


def test_still_duplicate_and_strip_flag_prefix_helpers():
    """Direct unit coverage for the two small helpers the regeneration
    machinery depends on, since a bug in either would silently corrupt
    every regeneration decision above it."""
    assert _strip_flag_prefix(
        f"{_DUPLICATE_FLAG_PREFIX}Some original text."
    ) == "Some original text."
    assert _strip_flag_prefix("No prefix here.") == "No prefix here."

    assert _still_duplicate("", ["anything"]) is True
    assert _still_duplicate("short", ["anything"]) is True  # under length floor
    assert _still_duplicate(
        "RAG System interacts with the LLM API for generating grounded answers.",
        ["RAG System interacts with the LLM API for generating grounded answers."],
    ) is True
    assert _still_duplicate(
        "The Vector Store has no described backup or snapshot strategy.",
        ["RAG System interacts with the LLM API for generating grounded answers."],
    ) is False