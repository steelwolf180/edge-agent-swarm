"""
Smoke tests for the Researcher agent (Kickoff Checklist §6).

Adjust the two imports below if your actual package layout differs
(assumed: agents/researcher.py + schemas/researcher.py per the repo
scaffold in Checklist §2).

Tiers, run bottom-to-top confidence-wise:
    1. Stub / pure-function tests      -- no network, no model, no DBOS
    2. Schema (Pydantic) tests         -- no network, no model
    3. Mocked-Gemma integration tests  -- no real llama-server call,
       but exercises run_researcher() end-to-end logic
    4. Blackboard test (marked)        -- needs a real DBOS + Postgres
       context; skipped unless RUN_DBOS_TESTS=1

Run:
    pytest tests/smoke/test_researcher.py -v
    RUN_DBOS_TESTS=1 pytest tests/smoke/test_researcher.py -v -m integration
"""
from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

from agents.researcher import (
    _extract_services_fallback,
    _spec_signals_no_hosting,
    query_infracost,
    run_researcher,
    write_to_blackboard,
)
from schemas.researcher import PricingLineItem, ResearcherOutput


# ---------------------------------------------------------------------------
# Tier 1: stub / pure-function tests (Checklist §6: "Infracost GraphQL stub
# call validates")
# ---------------------------------------------------------------------------

def test_stub_pricing_returns_expected_shape():
    result = query_infracost(["EC2", "RDS"])
    assert result["provider"] == "aws"
    assert len(result["line_items"]) == 2
    services = {item["service"] for item in result["line_items"]}
    assert services == {"EC2", "RDS"}
    for item in result["line_items"]:
        assert isinstance(item["monthly_cost_usd"], float)
        assert item["monthly_cost_usd"] >= 0


def test_stub_unknown_service_falls_back_to_default_price():
    result = query_infracost(["SOME_MADE_UP_SERVICE"])
    assert result["line_items"][0]["monthly_cost_usd"] == 10.0


def test_stub_is_case_insensitive():
    result = query_infracost(["ec2"])
    assert result["line_items"][0]["monthly_cost_usd"] == 30.0


def test_extract_services_fallback_finds_known_services():
    spec = "A web app on EC2 with an RDS database and S3 storage."
    found = _extract_services_fallback(spec)
    assert set(found) == {"EC2", "RDS", "S3"}


def test_extract_services_fallback_defaults_to_ec2_when_none_found():
    spec = "A system with no recognizable cloud services mentioned."
    found = _extract_services_fallback(spec)
    assert found == ["EC2"]


# ---------------------------------------------------------------------------
# Tier 1b: no-hosting guard tests. Motivated by a real fabrication caught
# on openrouter_rag.json (workflow 6b0b7b10-7550-4409-bda2-6511b2b96fa6):
# a spec explicitly stating no cloud hosting is required contains none of
# the known service tokens, so _extract_services_fallback's
# `found or ["EC2"]` default silently invented a $30/mo EC2 line item.
# ---------------------------------------------------------------------------

def test_spec_signals_no_hosting_detects_explicit_phrase():
    spec = "Deployment: single-machine local script/CLI, no hosting required."
    assert _spec_signals_no_hosting(spec) is True


def test_spec_signals_no_hosting_detects_no_self_hosted_inference():
    spec = "Budget: pay-per-use pricing via embedding API and OpenRouter; no self-hosted inference."
    assert _spec_signals_no_hosting(spec) is True


def test_spec_signals_no_hosting_false_when_absent():
    spec = "A web app on EC2 with an RDS database and S3 storage."
    assert _spec_signals_no_hosting(spec) is False


def test_spec_signals_no_hosting_is_case_insensitive():
    spec = "NO HOSTING REQUIRED for this deployment."
    assert _spec_signals_no_hosting(spec) is True


def test_spec_signals_no_hosting_on_real_openrouter_rag_fixture():
    """Pins the failure that motivated this guard: openrouter_rag.json's
    deployment_environment and budget_infra_limits fields both state no
    cloud hosting is needed, but contain zero known-service tokens
    (EC2/RDS/S3/...), so pre-guard _extract_services_fallback's
    `found or ["EC2"]` fabricated a $30/mo EC2 line item (workflow
    6b0b7b10-7550-4409-bda2-6511b2b96fa6)."""
    spec_text = (
        "Deployment: Single-machine local script/CLI, no hosting required; "
        "only outbound calls are to the embedding API and OpenRouter. "
        "Budget: Pay-per-use pricing via embedding API and OpenRouter; "
        "no self-hosted inference."
    )
    assert _spec_signals_no_hosting(spec_text) is True
    # Confirms the pre-guard assumption too: no known service token present,
    # so the old code path really did fall through to the ["EC2"] default --
    # this is *why* run_researcher must check the guard before calling
    # _extract_services_fallback, not a claim that the fallback itself changed.
    assert _extract_services_fallback(spec_text) == ["EC2"]


# ---------------------------------------------------------------------------
# Tier 2: schema tests (Checklist §6: "Output parses into ResearcherOutput")
# ---------------------------------------------------------------------------

def test_researcher_output_parses_valid_dict():
    payload = {
        "services_identified": ["EC2", "RDS"],
        "pricing": [
            {"service": "EC2", "monthly_cost_usd": 30.0},
            {"service": "RDS", "monthly_cost_usd": 45.0, "notes": "stub"},
        ],
        "pricing_context": {"EC2": 30.0, "RDS": 45.0},
        "summary": "Two services identified, estimated $75/mo combined.",
        "tool_call_succeeded": True,
    }
    output = ResearcherOutput.model_validate(payload)
    assert len(output.pricing) == 2
    assert output.pricing[0].provider == "aws"  # default applied


def test_researcher_output_rejects_negative_cost():
    with pytest.raises(ValidationError):
        PricingLineItem(service="EC2", monthly_cost_usd=-5.0)


def test_researcher_output_requires_summary():
    with pytest.raises(ValidationError):
        ResearcherOutput(services_identified=["EC2"], pricing=[])


def test_researcher_output_rejects_malformed_json():
    malformed = '{"services_identified": ["EC2"], "pricing": [}'
    with pytest.raises(ValidationError):
        ResearcherOutput.model_validate_json(malformed)


def test_to_blackboard_payload_shape():
    output = ResearcherOutput(
        services_identified=["EC2"],
        pricing=[PricingLineItem(service="EC2", monthly_cost_usd=30.0)],
        pricing_context={"EC2": 30.0},
        summary="One service, $30/mo.",
        tool_call_succeeded=True,
    )
    payload = output.to_blackboard_payload()
    assert set(payload.keys()) == {
        "services_identified",
        "pricing",
        "pricing_context",
        "summary",
    }
    assert payload["pricing"][0]["service"] == "EC2"


def test_researcher_output_accepts_fallback_reason():
    output = ResearcherOutput(
        services_identified=[],
        pricing=[],
        pricing_context={},
        summary="No pricing lookup performed: spec indicates no cloud hosting is required.",
        tool_call_succeeded=False,
        fallback_reason="no_hosting_detected",
    )
    assert output.fallback_reason == "no_hosting_detected"


def test_researcher_output_fallback_reason_defaults_none():
    output = ResearcherOutput(
        services_identified=["EC2"],
        pricing=[PricingLineItem(service="EC2", monthly_cost_usd=30.0)],
        pricing_context={"EC2": 30.0},
        summary="One service, $30/mo.",
        tool_call_succeeded=True,
    )
    assert output.fallback_reason is None


# ---------------------------------------------------------------------------
# Tier 3: mocked-Gemma integration tests. Monkeypatches httpx.post so no
# live llama-server call is required, but exercises the real control flow
# in run_researcher().
# ---------------------------------------------------------------------------

class _MockResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _gemma_response_with_tool_call():
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "infracost_query",
                                "arguments": json.dumps({"services": ["EC2", "S3"]}),
                            },
                        }
                    ],
                }
            }
        ]
    }


def _gemma_response_without_tool_call():
    return {
        "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": []}}]
    }


def _gemma_summary_response(text="Estimated combined cost is low."):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def test_run_researcher_with_tool_call(monkeypatch):
    calls = {"count": 0}

    def fake_post(url, json=None, timeout=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return _MockResponse(_gemma_response_with_tool_call())
        return _MockResponse(_gemma_summary_response("EC2 and S3 combined run about $35/mo."))

    monkeypatch.setattr("agents.researcher.httpx.post", fake_post)

    result = run_researcher("A web app on EC2 with S3 storage.")

    assert result.tool_call_succeeded is True
    assert set(result.services_identified) == {"EC2", "S3"}
    assert "35" in result.summary or result.summary  # summary present
    assert result.pricing_context == {"EC2": 30.0, "S3": 5.0}
    assert result.fallback_reason is None


def test_run_researcher_without_tool_call_summary_includes_pricing(monkeypatch):
    """
    Regression guard for the fixed fallback-path bug: when Gemma skips
    the tool call, the second ("summarise") call must still include
    pricing data. Previously it didn't (see git history / prior test
    name), so this pins the corrected behavior.
    """
    captured_second_call_messages = []

    def fake_post(url, json=None, timeout=None):
        if len(captured_second_call_messages) == 0 and "tools" in (json or {}):
            captured_second_call_messages.append(json["messages"])
            return _MockResponse(_gemma_response_without_tool_call())
        captured_second_call_messages.append(json["messages"])
        return _MockResponse(_gemma_summary_response())

    monkeypatch.setattr("agents.researcher.httpx.post", fake_post)

    result = run_researcher("A web app on EC2 with an RDS database.")

    assert result.tool_call_succeeded is False
    second_call_messages = captured_second_call_messages[-1]
    serialized = json.dumps(second_call_messages)
    # Fixed behavior: pricing numbers now reach the second call.
    assert "monthly_cost_usd" in serialized
    assert "30.0" in serialized  # EC2 stub price


def test_run_researcher_handles_malformed_tool_arguments(monkeypatch):
    """If Gemma returns invalid JSON in tool_calls[0].arguments, this
    currently raises json.JSONDecodeError uncaught. Pins current behavior
    so a future try/except addition is a visible, intentional diff."""

    def fake_post(url, json=None, timeout=None):
        return _MockResponse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {"id": "call_1", "function": {
                                    "name": "infracost_query", "arguments": "{not valid json"}}
                            ],
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("agents.researcher.httpx.post", fake_post)

    with pytest.raises(json.JSONDecodeError):
        run_researcher("A web app on EC2.")


def test_run_researcher_no_hosting_spec_skips_infracost(monkeypatch):
    """No-tool-call branch: spec says no hosting, Gemma also doesn't call
    the tool -- services/pricing must come back empty, not defaulted to
    EC2. Mirrors the real openrouter_rag.json failure shape."""

    def fake_post(url, json=None, timeout=None):
        if "tools" in (json or {}):
            return _MockResponse(_gemma_response_without_tool_call())
        return _MockResponse(_gemma_summary_response("No cloud hosting costs apply."))

    monkeypatch.setattr("agents.researcher.httpx.post", fake_post)

    result = run_researcher(
        "Deployment: single-machine local script/CLI, no hosting required."
    )

    assert result.services_identified == []
    assert result.pricing == []
    assert result.pricing_context == {}
    assert result.fallback_reason == "no_hosting_detected"
    assert result.tool_call_succeeded is False


def test_run_researcher_model_calls_infracost_despite_no_hosting_spec(monkeypatch):
    """Tool-call branch: spec says no hosting, but Gemma calls
    infracost_query anyway -- real pricing data still flows through (not
    suppressed), but gets flagged with a distinct reason from the
    fallback-guess case, since this is model hallucination rather than a
    code-level default."""
    calls = {"count": 0}

    def fake_post(url, json=None, timeout=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return _MockResponse(_gemma_response_with_tool_call())  # calls EC2, S3
        return _MockResponse(_gemma_summary_response("Estimated $35/mo."))

    monkeypatch.setattr("agents.researcher.httpx.post", fake_post)

    result = run_researcher(
        "Deployment: single-machine local script/CLI, no hosting required. "
        "A web app on EC2 with S3 storage mentioned only as a comparison."
    )

    assert result.tool_call_succeeded is True
    assert result.fallback_reason == "model_called_infracost_despite_no_hosting_spec"
    assert result.pricing_context == {"EC2": 30.0, "S3": 5.0}  # not suppressed, just flagged


def test_run_researcher_normal_path_unaffected_by_guard(monkeypatch):
    """Regression check: a spec with no no-hosting signal and no known
    service token still falls through to the old ['EC2'] default,
    fallback_reason stays None -- confirms the guard is scoped to explicit
    no-hosting language and doesn't change unrelated behavior."""

    def fake_post(url, json=None, timeout=None):
        if "tools" in (json or {}):
            return _MockResponse(_gemma_response_without_tool_call())
        return _MockResponse(_gemma_summary_response())

    monkeypatch.setattr("agents.researcher.httpx.post", fake_post)

    result = run_researcher("A system with no recognizable cloud services mentioned.")

    assert result.services_identified == ["EC2"]
    assert result.fallback_reason is None


# ---------------------------------------------------------------------------
# Tier 4: blackboard round-trip. Needs a live DBOS workflow context bound to
# your real PG18 instance, so it's gated and skipped by default.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_DBOS_TESTS") != "1",
    reason="set RUN_DBOS_TESTS=1 to run against a live DBOS/Postgres instance",
)
def test_blackboard_roundtrip():
    from dbos import DBOS

    fixture = ResearcherOutput(
        services_identified=["EC2"],
        pricing=[PricingLineItem(service="EC2", monthly_cost_usd=30.0)],
        pricing_context={"EC2": 30.0},
        summary="Test fixture summary.",
        tool_call_succeeded=True,
    )

    @DBOS.workflow()
    def _roundtrip_workflow():
        write_to_blackboard(fixture)
        return DBOS.get_event("pricing_context", timeout_seconds=5)

    result = _roundtrip_workflow()
    assert result["pricing_context"] == {"EC2": 30.0}
    assert result["summary"] == "Test fixture summary."