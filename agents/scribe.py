"""Scribe agent (LFM2.5-VL-1.6B). No tools -- consumes DBOS blackboard context only.

Standalone here per KICKOFF_CHECKLIST.md §6: build and validate each agent in
isolation before wiring into the DBOS pipeline (§7). The @DBOS.step() wrapper,
set_event/get_event blackboard reads, and prior-spec lookup from
`spec_versions` get added at §7 -- this module takes plain dicts so it can be
smoke-tested without a running DBOS workflow.
"""
import json
import os

import httpx
from deepdiff import DeepDiff
from pydantic import ValidationError

from schemas.adr import ADROutput
from schemas.spec import ArchitectureSpec

from dotenv import load_dotenv
load_dotenv()

LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL") + "/v1/chat/completions"
LFM_MODEL_NAME = os.environ.get("LFM_MODEL_NAME")  # must match the model name in models.ini
SCRIBE_MAX_OUTPUT_TOKENS = int(os.environ.get("SCRIBE_TOKEN_BUDGET"))

SCRIBE_SYSTEM_PROMPT = """You are the Scribe agent in an architecture review pipeline.
You write Architecture Decision Records (ADRs) in Context / Decision / Consequences form.
You have no tools. Base your ADR only on the spec diff and blackboard context given to you.

CRITICAL GROUNDING RULE: Your "decision", "consequences", and "diff_summary" fields must
describe ONLY the specific change shown in the "Spec diff" section below. Do not invent,
assume, or generalize to a larger architectural decision. Do not discuss data centralization,
service consolidation, or any other topic unless that exact topic appears in the diff text
you were given. If the diff is a single small change (e.g. one infrastructure detail), your
ADR must be correspondingly narrow and specific to that one change -- not a broader redesign.

Before writing, re-read the "Spec diff" section and identify the exact field path and the
exact old_value/new_value shown. Your decision must directly reference what changed, in
concrete terms (which field, old state, new state) -- not a paraphrase of a generic
architectural theme.

Respond with a single JSON object matching this schema, and nothing else:
{"context": str, "decision": str, "consequences": str, "diff_summary": str, "affected_diagrams": ["context"]}
For this MVP, affected_diagrams must always be exactly ["context"] -- L1 System Context is the
only diagram level in scope. Do not emit "container" even if the diff suggests a container-level change."""


# TODO(reconcile): identical copy of agents/critic.py's strip_code_fence().
# Same underlying issue -- LFM wraps JSON output in a markdown fence despite
# both system prompts saying "a single JSON object and nothing else" -- so
# this needs the same defense here that Critic already validated. Move both
# copies to a shared module (e.g. agents/_json_utils.py) and import from
# there instead of keeping two files in sync by hand.
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


def compute_spec_diff(
    prior_spec: ArchitectureSpec | None, current_spec: ArchitectureSpec
) -> DeepDiff:
    """Semantic diff between spec versions, typed at the boundary.

    Diffing model_dump() of two ArchitectureSpec instances (rather than raw
    dicts) means both sides share the same field shape and defaults, so a
    missing optional field never masquerades as a spurious add/remove -- and
    diff paths (e.g. functional_requirements.core_features[2]) map directly
    onto real spec fields for the ADR's diff_summary.

    prior_spec=None (spec_version 1, no prior approved spec) still returns a
    full "creation" diff for logging -- the caller decides whether
    spec_version 1 should skip ADR generation entirely (spec §5 data flow).
    """
    baseline = prior_spec.model_dump() if prior_spec else {}
    return DeepDiff(baseline, current_spec.model_dump(), ignore_order=True, view="tree")


def summarize_diff(diff: DeepDiff, max_items: int = 8) -> str:
    """Compress a DeepDiff into a short bullet list for the prompt.

    Truncated to max_items to stay inside the ~800 token Scribe budget (spec §4,
    Context Window Budget: Scribe ~800 tokens).
    """
    lines: list[str] = []
    for change_type, items in diff.to_dict().items():
        for path, detail in list(items.items())[:max_items]:
            lines.append(f"- {change_type}: {path} -> {detail}")
    return "\n".join(lines[:max_items]) if lines else "No field-level changes detected."

def hunk_count_from_diff(diff: DeepDiff) -> int:
    """Total discrete change entries across all DeepDiff categories
    (values_changed, dictionary_item_added/removed, etc.). Unbounded by
    summarize_diff()'s max_items truncation -- this feeds Judge's
    adrs_per_diff metric and should reflect the real diff, not the
    prompt-truncated view Scribe's LLM saw. Never sent to the LLM, so it
    costs nothing against Scribe's ~800 token input budget."""
    return sum(len(items) for items in diff.to_dict().values())

def build_user_prompt(diff_summary: str, blackboard_context: str) -> str:
    return (
        f"Spec diff:\n{diff_summary}\n\n"
        f"Blackboard context (Researcher + Architect output, summarized):\n{blackboard_context}\n\n"
        "Write the ADR now."
    )


async def run_scribe(
    prior_spec: ArchitectureSpec | None,
    current_spec: ArchitectureSpec,
    blackboard_context: str,
    client: httpx.AsyncClient | None = None,
) -> ADROutput:
    diff = compute_spec_diff(prior_spec, current_spec)
    diff_summary = summarize_diff(diff)
    hunk_count = hunk_count_from_diff(diff)

    payload = {
        "model": LFM_MODEL_NAME,
        "messages": [
            {"role": "system", "content": SCRIBE_SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(diff_summary, blackboard_context)},
        ],
        "temperature": 0.2,
        "max_tokens": SCRIBE_MAX_OUTPUT_TOKENS,
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
    
    if choice.get("finish_reason") == "length":
        raise ValueError(
            f"Scribe: LFM hit max_tokens ({SCRIBE_MAX_OUTPUT_TOKENS}) before finishing "
            f"output. Raise SCRIBE_TOKEN_BUDGET or shorten the prompt. "
            f"Partial output: {raw[:200]}"
        )

    try:
        parsed = json.loads(strip_code_fence(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"Scribe: LFM did not return valid JSON: {raw[:200]}") from e

    # MVP guardrail: force this regardless of what the model returned, rather than
    # trusting prompt compliance alone -- 'container' is out of scope until v2.
    parsed["affected_diagrams"] = ["context"]
    # diff_hunk_count is never model output -- attached by code from the same
    # DeepDiff object diff_summary was built from. Same treatment as
    # affected_diagrams above and ArchitectOutput.diagram_source.
    parsed["diff_hunk_count"] = hunk_count            # <-- new

    try:
        return ADROutput.model_validate(parsed)
    except ValidationError as e:
        raise ValueError(f"Scribe: LFM output failed ADROutput validation: {e}") from e