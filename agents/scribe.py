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
import re

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
you were given.

FORMATTING RULES (all fields):
- Never copy or quote the "Spec diff" or "Blackboard context" text verbatim into any field.
  Paraphrase concisely, in your own words, in 1-2 sentences per field.
- "context" must always be exactly the short fixed label: "L1 System Context". Do not put
  any other content in this field.
- "decision" and "consequences" must each be ONE sentence, no more than 25 words, specific
  to the one change in the diff -- not a summary of the entire architecture or blackboard context.

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


def _format_diff_detail(detail) -> str:
    """Render a single DeepDiff change entry as plain prose, never as a
    raw dict/object repr. DeepDiff's tree view yields dict-shaped detail
    objects (e.g. {'old_value': ..., 'new_value': ...}) for values_changed
    entries -- f-string interpolating that directly puts dict-looking text
    straight into the prompt. Confirmed on a real incremental diff: LFM
    mirrored that shape back into diff_summary as a nested JSON object
    instead of a string, failing ADROutput validation 3/3 times at
    temperature=0.05 (deterministic, not sampling noise a retry could
    route around). Keeping this human-readable removes the shape there
    was to imitate in the first place.
    """
    if isinstance(detail, dict):
        old = detail.get("old_value", "?")
        new = detail.get("new_value", "?")
        return f"changed from {old!r} to {new!r}"
    return str(detail)


def summarize_diff(diff: DeepDiff, max_items: int = 8) -> str:
    """Compress a DeepDiff into a short bullet list for the prompt.

    Truncated to max_items to stay inside the ~800 token Scribe budget (spec §4,
    Context Window Budget: Scribe ~800 tokens).
    """
    lines: list[str] = []
    for change_type, items in diff.to_dict().items():
        for path, detail in list(items.items())[:max_items]:
            lines.append(f"- {change_type}: {path} -> {_format_diff_detail(detail)}")
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

_FIELD_ORDER = ["context", "decision", "consequences", "diff_summary"]
_STRING_FIELD_RE = re.compile(r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _last_complete_sentence(text: str) -> str:
    """Trim partial text at the last complete sentence boundary. Falls back
    to the raw (stripped) text if no boundary is found, so callers always
    get something rather than an empty string."""
    text = text.strip()
    matches = list(re.finditer(r'[.!?](?:\s|$)', text))
    if not matches:
        return text
    return text[:matches[-1].end()].strip()


def _salvage_truncated_scribe_output(raw: str) -> dict:
    """Best-effort recovery when LFM hits max_tokens mid-generation.

    Confirmed reproducible at temperature=0.05 (identical output across
    retries) -- not sampling noise a retry can route around. Rather than
    raising and burning a retry that will fail identically, extract every
    field that closed as a valid JSON string, salvage the in-progress
    field at its last complete sentence, and mark anything that never
    started with an explicit placeholder for human review at the approval
    gate -- never a silent blank.
    """
    stripped = strip_code_fence(raw)
    complete_fields = dict(_STRING_FIELD_RE.findall(stripped))

    salvaged = {}
    for field in _FIELD_ORDER:
        if field in complete_fields:
            salvaged[field] = complete_fields[field]
            continue

        partial_match = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)$', stripped)
        if partial_match and partial_match.group(1).strip():
            trimmed = _last_complete_sentence(partial_match.group(1))
            salvaged[field] = trimmed or (
                "TRUNCATED: generation exceeded token budget before completing this field"
            )
        else:
            salvaged[field] = (
                "MISSING: generation exceeded token budget before this field started"
            )

    return salvaged

_MAX_COERCED_FIELD_CHARS = 300


def _coerce_adr_string_fields(parsed: dict) -> tuple[dict, list[str]]:
    """Defends against LFM returning a non-string value (dict/list) for a
    field ADROutput requires as str. Observed on a real incremental diff
    (spec_v2.json, 6 Aug): the prompt's diff summary contained dict-shaped
    text and the model mirrored that shape back into diff_summary as a
    nested object instead of paraphrasing it -- reproducible 3/3 times at
    temperature=0.05, exhausting scribe_step's DBOS retries and failing
    the whole pipeline with zero artifacts after ~670s of compute.
    _format_diff_detail() above should prevent the trigger going forward,
    but this is the same class of "model didn't follow the schema" as
    truncation/malformed JSON and deserves the same treatment: coerce
    once, don't burn 3 identical retries finding out it fails the same
    way each time. Compact JSON keeps the coerced value inspectable
    without silently blanking it; the leading text guarantees the value
    never starts with '[', so it can't trip the frontmatter list-parsing
    bug persistence.py now guards against, and it's a distinct visible
    flag for human review at the approval gate rather than a wall of raw
    JSON masquerading as a normal ADR sentence.
    """
    coerced: list[str] = []
    for field in _FIELD_ORDER:
        value = parsed.get(field)
        if value is not None and not isinstance(value, str):
            compact = json.dumps(value, separators=(",", ":"))
            if len(compact) > _MAX_COERCED_FIELD_CHARS:
                compact = compact[:_MAX_COERCED_FIELD_CHARS] + "...(truncated)"
            parsed[field] = (
                f"NON-STRING OUTPUT (coerced from {type(value).__name__}, "
                f"flag for human review): {compact}"
            )
            coerced.append(field)
    return parsed, coerced


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
        "temperature": 0.05,
        "max_tokens": SCRIBE_MAX_OUTPUT_TOKENS,
        "frequency_penalty": 0.4,
        "presence_penalty": 0.2,
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
    
    salvage_reason = None
    if choice.get("finish_reason") == "length":
        print(
            f"WARNING: Scribe hit max_tokens ({SCRIBE_MAX_OUTPUT_TOKENS}) -- "
            f"salvaging partial output instead of failing the step."
        )
        parsed = _salvage_truncated_scribe_output(raw)
        salvage_reason = "truncated"
    else:
        try:
            parsed = json.loads(strip_code_fence(raw))
        except json.JSONDecodeError as e:
            # Not the truncation case above -- finish_reason != "length" here,
            # meaning LFM completed generation but the JSON is structurally
            # broken (bad delimiter, stray comma, etc). Reuse the same
            # sentence-boundary salvage rather than raising and letting DBOS's
            # step-level retry burn a full LFM inference pass on output that
            # may fail identically at temperature=0.05. Kept as a distinct
            # log message/reason from the truncation case since the failure
            # mode -- and what it implies about compute_duration_s -- differs.
            print(
                f"WARNING: Scribe produced malformed JSON on a complete "
                f"generation (finish_reason={choice.get('finish_reason')!r}, "
                f"error={e}) -- salvaging instead of failing the step."
            )
            parsed = _salvage_truncated_scribe_output(raw)
            salvage_reason = "malformed_json"

    parsed, coerced_fields = _coerce_adr_string_fields(parsed)
    if coerced_fields:
        print(
            f"WARNING: Scribe returned non-string value(s) for "
            f"{coerced_fields} -- coerced to a flagged string instead of "
            f"failing the step."
        )
        salvage_reason = salvage_reason or "non_string_field"

    if salvage_reason:
        # Not part of ADROutput's schema -- logged, not persisted, so it can't
        # trip strict validation below. Surfacing it here (rather than only in
        # the WARNING prints above) makes it greppable from a single line if
        # this ever needs to be correlated against compute_duration_s.
        print(f"INFO: Scribe output was salvaged (reason={salvage_reason}).")

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