"""Critic agent (LFM2.5-VL-1.6B). No tools -- devil's advocate against the
Architect's diagram, given the ADR Scribe just wrote to explain the change.

Standalone here per KICKOFF_CHECKLIST.md §6, same pattern as agents/scribe.py:
plain typed inputs, no @DBOS.step() wrapper yet and no blackboard
set_event/get_event reads (those get added at §7) -- this module takes
what the blackboard would have handed it, so it can be smoke-tested without
a running DBOS workflow.

architect_output and adr_output are typed (ArchitectOutput / ADROutput) since
both schemas already exist and the data flow names them explicitly. Anything
else Critic might need -- spec-level integration_points, Researcher pricing
context -- comes through blackboard_context as an opaque summarized string,
same shape Scribe already takes for its own blackboard read.
"""
import json
import os

import httpx
from pydantic import ValidationError

from schemas.adr import ADROutput
from schemas.architect import ArchitectOutput, Component
from schemas.critic import CriticOutput

from dotenv import load_dotenv
load_dotenv()

LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL") + "/v1/chat/completions"
LFM_MODEL_NAME = os.environ.get("LFM_MODEL_NAME")  # must match the model name in models.ini

# NOTE: spec §4's "Critic ~700 tokens" context-window figure is an INPUT prompt
# budget, not an output completion cap -- Critic's output is a variable-length
# list of gap objects (each with a full-sentence description), not a fixed-shape
# record like Scribe's ADR, so it needs meaningfully more room than the input
# budget number to avoid truncating mid-JSON.
CRITIC_MAX_OUTPUT_TOKENS = int(os.environ.get("CRITIC_TOKEN_BUDGET"))

CRITIC_SYSTEM_PROMPT = """You are the Critic agent in an architecture review pipeline.
You play devil's advocate against a C4 System Context diagram and the ADR that explains it.
You have no tools. Base your critique only on the diagram, components, ADR, and blackboard
context given to you. Surface gaps, single points of failure, and integrations implied by
the context but missing from the diagram. Do not default to agreement -- a diagram with zero
flagged gaps should be the rare case, not the norm. If nothing is genuinely wrong, say so with
an empty gaps list rather than inventing filler. Keep each gap description to one sentence.
Respond with a single JSON object matching this schema, and nothing else:
{"gaps": [{"description": str, "severity": "low"|"medium"|"high", "related_component": str|null}],
 "spofs": [str], "missing_integrations": [str]}"""


def strip_code_fence(raw: str) -> str:
    """Strip a markdown code fence (```json ... ``` or ``` ... ```) if present.

    LFM sometimes wraps JSON output in a fence despite the system prompt
    saying "a single JSON object and nothing else" -- prompting alone isn't
    reliable enough to rule this out, so defend at the parsing layer too.
    No-op if no fence is present.
    """
    stripped = raw.strip()
    if stripped.startswith("```"):
        # Drop the opening fence line (``` or ```json) and the closing ```.
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped[3:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def summarize_components(components: list[Component]) -> str:
    """Compact bullet list of components for the prompt, staying inside the
    ~700 token Critic input budget (spec §4 Context Window Budget)."""
    lines = []
    for c in components:
        redundancy = "redundant" if c.redundant else "no redundancy"
        tech = f", {c.technology}" if c.technology else ""
        lines.append(f"- [{c.id}] {c.name} ({c.type}{tech}, {redundancy}): {c.description}")
    return "\n".join(lines) if lines else "No components listed."


def build_user_prompt(
    architect_output: ArchitectOutput,
    adr_output: ADROutput,
    blackboard_context: str,
) -> str:
    return (
        f"C4 System Context diagram (Mermaid):\n{architect_output.context_diagram}\n\n"
        f"Components:\n{summarize_components(architect_output.components)}\n\n"
        f"Architect's supporting docs:\n{architect_output.docs}\n\n"
        f"ADR just recorded for this change:\n"
        f"Context: {adr_output.context}\n"
        f"Decision: {adr_output.decision}\n"
        f"Consequences: {adr_output.consequences}\n\n"
        f"Blackboard context (spec + Researcher pricing, summarized):\n{blackboard_context}\n\n"
        "Critique this now."
    )


async def run_critic(
    architect_output: ArchitectOutput,
    adr_output: ADROutput,
    blackboard_context: str,
    client: httpx.AsyncClient | None = None,
) -> CriticOutput:
    payload = {
        "model": LFM_MODEL_NAME,
        "messages": [
            {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_user_prompt(architect_output, adr_output, blackboard_context),
            },
        ],
        "temperature": 0.2,
        "max_tokens": CRITIC_MAX_OUTPUT_TOKENS,
    }

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=150.0)
    try:
        response = await client.post(LLAMA_SERVER_URL, json=payload)
        
        if response.status_code >= 400:
            raise ValueError(
                f"Scribe: llama-server returned {response.status_code}. "
                f"Body: {response.text[:500]}"
            )
        
        choice = response.json()["choices"][0]
        raw = choice["message"]["content"]
    
    finally:
        if owns_client:
            await client.aclose()

    # Catch truncation explicitly, before the JSON parser turns it into a
    # confusing "Unterminated string" error further down.
    if choice.get("finish_reason") == "length":
        raise ValueError(
            f"Critic: LFM hit max_tokens ({CRITIC_MAX_OUTPUT_TOKENS}) before finishing "
            f"output. Raise CRITIC_MAX_OUTPUT_TOKENS or shorten the prompt. "
            f"Partial output: {raw[:200]}"
        )

    try:
        parsed = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"Critic: LFM did not return valid JSON: {raw[:200]}") from e

    try:
        return CriticOutput.model_validate(parsed)
    except ValidationError as e:
        raise ValueError(f"Critic: LFM output failed CriticOutput validation: {e}") from e
