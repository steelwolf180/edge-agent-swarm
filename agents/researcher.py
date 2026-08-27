"""
Researcher agent (Gemma 4 E4B QAT).

Role per spec §2: enriches the blackboard with context and Infracost
cloud pricing data via tool call. The only agent with a live external
tool call (per Kickoff Checklist §6 — build this one first).

This module is deliberately standalone: it can be run and smoke-tested
in isolation (checklist §6) before DBOS pipeline wiring (§7) exists.
Once §7 lands, wrap `run_researcher` in a `@DBOS.step()` — don't
duplicate the logic there.

MVP scoping (spec §8): live Infracost call is v1.1. Default here is
the stub. Set INFRACOST_LIVE=1 to hit the real GraphQL endpoint.
"""
from __future__ import annotations

import json
import os
import re
import httpx

from schemas.researcher import PricingLineItem, ResearcherOutput
from dotenv import load_dotenv

load_dotenv()

LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL")
INFRACOST_URL = os.environ.get("INFRACOST_URL")
GEMMA_MODEL_NAME = os.environ.get("GEMMA_MODEL_NAME")  # must match models.ini preset name
INFRACOST_LIVE = os.environ.get("INFRACOST_LIVE") == "1"
RESEARCHER_HTTP_TIMEOUT_S = float(os.environ.get("RESEARCHER_HTTP_TIMEOUT_S"))
RESEARCHER_MAX_OUTPUT_TOKENS = int(os.environ.get("RESEARCHER_TOKEN_BUDGET"))

SYSTEM_PROMPT = """You are the Researcher agent in an architecture review pipeline.
Given a spec, identify the cloud services it depends on (e.g. EC2, RDS, S3),
call the infracost_query tool with those service names, then summarise the
pricing context in under 150 words for the Architect agent that follows you.
Do not invent services that are not implied by the spec."""

INFRACOST_TOOL = {
    "type": "function",
    "function": {
        "name": "infracost_query",
        "description": "Get monthly USD cost estimates for named cloud services.",
        "parameters": {
            "type": "object",
            "properties": {
                "services": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Cloud service identifiers, e.g. ['EC2', 'RDS']",
                },
                "provider": {"type": "string", "default": "aws"},
            },
            "required": ["services"],
        },
    },
}

# Hardcoded stub — MVP scoping per spec §8. Swap for a real GraphQL
# query body against INFRACOST_URL when INFRACOST_LIVE=1.
_STUB_PRICES = {
    "EC2": 30.0,
    "RDS": 45.0,
    "S3": 5.0,
    "LAMBDA": 2.0,
    "ELB": 18.0,
    "CLOUDFRONT": 8.0,
}

def query_infracost(services: list[str], provider: str = "aws") -> dict:
    """
    Infracost GraphQL tool call. Stub by default (checklist §6 gate:
    "Infracost GraphQL stub call validates").
    """
    if not INFRACOST_LIVE:
        return {
            "provider": provider,
            "line_items": [
                {
                    "service": s,
                    "monthly_cost_usd": _STUB_PRICES.get(s.upper(), 10.0),
                    "notes": "stub estimate, MVP scoping per spec §8",
                }
                for s in services
            ],
        }

    query = """
    query($services: [String!], $provider: String!) {
      pricing(services: $services, provider: $provider) {
        service
        monthlyCostUsd
        notes
      }
    }
    """
    resp = httpx.post(
        INFRACOST_URL,
        json={"query": query, "variables": {"services": services, "provider": provider}},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()["data"]["pricing"]
    return {
        "provider": provider,
        "line_items": [
            {"service": d["service"], "monthly_cost_usd": d["monthlyCostUsd"], "notes": d.get("notes")}
            for d in data
        ],
    }

_NO_HOSTING_SIGNALS = (
    "no hosting required",
    "no self-hosted inference",
    "no self-hosted",
    "no cloud hosting",
    "local-only",
    "local only",
    "on-prem",
    "air-gapped",
)

def _spec_signals_no_hosting(spec_text: str) -> bool:
    """Plain substring check, same zero-false-positive-risk class as Scribe's
    _DIFF_SYNTAX_TOKENS / _EXAMPLE_DOMAIN_TOKENS checks -- these phrases don't
    legitimately co-occur with a spec that also needs cloud pricing."""
    lowered = spec_text.lower()
    return any(signal in lowered for signal in _NO_HOSTING_SIGNALS)

def _extract_services_fallback(spec_text: str) -> list[str]:
    """If the model's tool call omits services, sniff obvious ones from spec text."""
    known = list(_STUB_PRICES.keys())
    found = [s for s in known if re.search(rf"\b{s}\b", spec_text, re.IGNORECASE)]
    return found or ["EC2"]  # never call Infracost with an empty list

def _call_gemma(messages: list[dict], tools: list[dict] | None = None) -> dict:
    """Single call to llama-server's OpenAI-compatible endpoint."""
    payload = {
        "model": GEMMA_MODEL_NAME,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": RESEARCHER_MAX_OUTPUT_TOKENS,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    resp = httpx.post(f"{LLAMA_SERVER_URL}/v1/chat/completions", json=payload, timeout=RESEARCHER_HTTP_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    
    if data["choices"][0].get("finish_reason") == "length":
        partial = data["choices"][0].get("message", {}).get("content", "")
        raise ValueError(
            f"Researcher: Gemma hit max_tokens ({RESEARCHER_MAX_OUTPUT_TOKENS}) before "
            f"finishing output. Raise RESEARCHER_TOKEN_BUDGET or shorten the prompt. "
            f"Partial output: {partial[:200]}"
        )

    return data

def run_researcher(spec_text: str) -> ResearcherOutput:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Spec:\n{spec_text}"},
    ]

    no_hosting = _spec_signals_no_hosting(spec_text)

    first = _call_gemma(messages, tools=[INFRACOST_TOOL])
    choice = first["choices"][0]["message"]
    tool_calls = choice.get("tool_calls") or []
    fallback_reason: str | None = None

    if not tool_calls:
        if no_hosting:
            # Spec explicitly says no cloud hosting -- _extract_services_fallback's
            # `found or ["EC2"]` default would fabricate a cost line item with no
            # basis in the spec (confirmed on openrouter_rag.json / workflow
            # 6b0b7b10-...: EC2 $30/mo guessed against a spec stating "no hosting
            # required"). Skip Infracost entirely rather than guess.
            services = []
            tool_result = {"provider": "aws", "line_items": []}
            tool_call_succeeded = False
            fallback_reason = "no_hosting_detected"
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "No pricing lookup performed: spec indicates no cloud "
                        "hosting is required."
                    ),
                }
            )
        else:
            services = _extract_services_fallback(spec_text)
            tool_result = query_infracost(services)
            tool_call_succeeded = False
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Pricing data (from a stubbed/fallback lookup, not a "
                        f"model-initiated tool call): {json.dumps(tool_result)}"
                    ),
                }
            )
    else:
        call = tool_calls[0]
        args = json.loads(call["function"]["arguments"])
        services = args.get("services") or _extract_services_fallback(spec_text)
        tool_result = query_infracost(services, provider=args.get("provider", "aws"))
        tool_call_succeeded = True

        if no_hosting and services:
            # Model called Infracost anyway despite an explicit no-hosting spec --
            # a different failure shape from the fallback guessing case (this is
            # the model choosing to hallucinate a cloud dependency, not the code
            # defaulting to one). Flag, don't suppress -- a real tool call
            # succeeded and the reviewer should see what happened, same
            # "flag inline, don't silently drop" pattern as Scribe/Critic guards.
            fallback_reason = "model_called_infracost_despite_no_hosting_spec"

        messages.append(choice)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(tool_result),
            }
        )

    final = _call_gemma(
        messages + [{"role": "user", "content": "Summarise the pricing context in under 150 words."}]
    )
    summary = final["choices"][0]["message"]["content"].strip()

    pricing_items = [PricingLineItem(**item) for item in tool_result["line_items"]]

    output = ResearcherOutput(
        services_identified=services,
        pricing=pricing_items,
        pricing_context={item.service: item.monthly_cost_usd for item in pricing_items},
        summary=summary,
        tool_call_succeeded=tool_call_succeeded,
        fallback_reason=fallback_reason,
    )
    return output

def write_to_blackboard(output: ResearcherOutput, workflow_id: str | None = None) -> None:
    """
    Writes pricing_context to the DBOS blackboard.

    Deferred import: keeps this module importable/runnable in isolation
    (checklist §6) even before DBOS pipeline wiring (§7) exists. Must be
    called from inside a DBOS workflow context.
    """
    from dbos import DBOS  # local import, see docstring

    DBOS.set_event("pricing_context", output.to_blackboard_payload())


if __name__ == "__main__":
    import sys

    spec = sys.argv[1] if len(sys.argv) > 1 else "A web app on EC2 with an RDS database and S3 storage."
    result = run_researcher(spec)
    print(result.model_dump_json(indent=2))