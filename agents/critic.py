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
import re
from difflib import SequenceMatcher

import httpx
from pydantic import ValidationError

from schemas.adr import ADROutput
from schemas.architect import ArchitectOutput, Component
from schemas.critic import CriticOutput

from dotenv import load_dotenv
load_dotenv()

LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL") + "/v1/chat/completions"
LFM_MODEL_NAME = os.environ.get("LFM_MODEL_NAME")  # must match the model name in models.ini
CRITIC_MAX_OUTPUT_TOKENS = int(os.environ.get("CRITIC_TOKEN_BUDGET"))
CRITIC_HTTP_TIMEOUT_S = int(os.environ.get("CRITIC_HTTP_TIMEOUT_S"))

CRITIC_SYSTEM_PROMPT = """You are the Critic agent in an architecture review pipeline.
You play devil's advocate against a C4 System Context diagram and the ADR that explains it.
You have no tools. Base your critique only on the diagram, components, ADR, and blackboard
context given to you. Surface gaps, single points of failure, and integrations implied by
the context but missing from the diagram. Do not default to agreement -- a diagram with zero
flagged gaps should be the rare case, not the norm. If nothing is genuinely wrong, say so with
an empty gaps list rather than inventing filler. Keep each gap description to one sentence.

The following is a WORKED FORMAT EXAMPLE ONLY, from an unrelated domain. Do not reuse
its content -- it exists only to show the expected depth and specificity of a real
critique. A genuine review of an unfamiliar diagram routinely finds issues like these:

EXAMPLE INPUT: A diagram shows a single "OrderService" handling both order writes
and read-heavy inventory lookups, no cache layer, and a "NotificationService" with
a Rel to an external SMS provider but no corresponding component in the components list.

EXAMPLE OUTPUT:
{"gaps": [{"description": "Inventory lookups and order writes share one service with no separation, risking read load starving write throughput.", "severity": "medium", "related_component": "OrderService"}],
 "spofs": ["OrderService is a single instance handling both critical paths with no stated redundancy."],
 "missing_integrations": ["NotificationService references an SMS provider via Rel but no external system is declared for it in the components list."]}

List at most 5 gaps, 3 SPOFs, and 3 missing integrations, ordered most
severe/important first. If more genuinely apply, name only the top ones —
breadth of coverage matters less than getting the most consequential
issues right. Keep each entry to one sentence, no exceptions.

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

def _detect_duplicate_list_items(parsed: dict) -> list[str]:
    """Flag any list field (spofs, missing_integrations, gap descriptions)
    containing an exact-duplicate entry. Boundary backstop for degenerate
    output (e.g. the same spof sentence repeated N times) that schema
    validation alone won't catch -- CriticOutput doesn't enforce uniqueness.
    """
    warnings = []
    for field in ("spofs", "missing_integrations"):
        items = parsed.get(field) or []
        seen = set()
        for item in items:
            key = item.strip().lower()
            if key in seen:
                warnings.append(
                    f"Critic output for '{field}' contains a duplicate entry: {item[:80]!r}"
                )
                break
            seen.add(key)
    # gaps is a list of dicts -- compare on 'description'
    gap_descs = [g.get("description", "").strip().lower() for g in (parsed.get("gaps") or [])]
    if len(gap_descs) != len(set(gap_descs)):
        warnings.append("Critic output for 'gaps' contains a duplicate description.")
    return warnings

def _detect_cross_field_duplication(parsed: dict) -> list[str]:
    """spofs and missing_integrations are semantically distinct categories --
    a SPOF is a redundancy/resilience risk, a missing integration is an
    undeclared dependency. Identical (or near-identical) entries across the
    two means the model treated them as the same list twice, not two kinds
    of analysis. Also checks gaps descriptions for the same underlying
    reworded-duplicate problem, since gaps is prose while spofs/missing_
    integrations are terse -- exact-match alone won't catch gaps overlap,
    so gaps is compared via substring containment against the other two
    instead of set intersection.

    Confirmed live on workflow 59e4e1b2-...: spofs and missing_integrations
    were identical entry-for-entry (3/3), gaps repeated the same three
    underlying findings as full sentences. _detect_duplicate_list_items()
    correctly found no *within-list* repetition on that run and stayed
    silent -- this guard covers the cross-field axis it doesn't.

    P2 closed on guard coverage, not model behavior (21 Aug 2026, same
    reasoning as P0's 19 Aug closure) -- exact cross-field overlap and
    verbatim gap-restatement are both caught before approval. NOT caught:
    a gap that paraphrases a spof/missing_integrations entry in different
    words rather than restating it verbatim -- reusing Scribe's
    SequenceMatcher-against-fixed-example pattern doesn't transfer here,
    since both sides of the comparison are dynamic, length-mismatched
    model output (a one-line entry vs. a full gap sentence), not a known
    string to match against. Logged as a known limitation (same class as
    Scribe's Rubber Stamp Risk, spec §7), not pursued further -- P2 is
    explicitly non-blocking for §9.
    """
    warnings = []
    spofs = [s.strip().lower() for s in (parsed.get("spofs") or [])]
    missing = [s.strip().lower() for s in (parsed.get("missing_integrations") or [])]

    spofs_set = set(spofs)
    missing_set = set(missing)
    exact_overlap = spofs_set & missing_set
    if exact_overlap:
        warnings.append(
            f"Critic 'spofs' and 'missing_integrations' share "
            f"{len(exact_overlap)} identical entr{'y' if len(exact_overlap) == 1 else 'ies'} "
            f"-- not distinct analysis categories on this run."
        )

    # gaps is prose, so check whether a gap description contains a spof or
    # missing_integrations entry as a substring rather than requiring an
    # exact match -- catches the reworded-into-a-sentence case.
    gap_descs = [g.get("description", "").strip().lower() for g in (parsed.get("gaps") or [])]
    reworded_hits = 0
    for gap in gap_descs:
        if any(s and s in gap for s in spofs_set) or any(m and m in gap for m in missing_set):
            reworded_hits += 1
    if reworded_hits:
        warnings.append(
            f"Critic 'gaps' contains {reworded_hits} description(s) that "
            f"restate a 'spofs' or 'missing_integrations' entry rather than "
            f"offering distinct analysis."
        )

    return warnings


# CRITIC_SYSTEM_PROMPT's WORKED FORMAT EXAMPLE (OrderService/NotificationService,
# an e-commerce domain unrelated to this pipeline) is meant to demonstrate output
# depth/specificity, not to be reused as content. Discovered 21 Aug 2026, while
# testing the P2 cross-field-duplication guard: on tests/smoke/test_critic.py's
# weak-Postgres fixture (which has nothing to do with orders, notifications, or
# SMS), 2 of 5 sampled runs at temperature=0.2 echoed the example's literal text
# into gaps/spofs/missing_integrations -- one badly enough to break JSON parsing.
# Same failure class as Scribe's P0 example-copying bug, just never previously
# exercised on Critic. Kept in sync manually with CRITIC_SYSTEM_PROMPT's example;
# if the example changes, update this set too.
_EXAMPLE_OUTPUT_STRINGS = {
    "Inventory lookups and order writes share one service with no separation, "
    "risking read load starving write throughput.",
    "OrderService is a single instance handling both critical paths with no "
    "stated redundancy.",
    "NotificationService references an SMS provider via Rel but no external "
    "system is declared for it in the components list.",
}

_COPY_SIMILARITY_THRESHOLD = 0.90

_ARTICLE_RE = re.compile(r"\b(a|an|the)\b")
_WHITESPACE_RE = re.compile(r"\s+")

# Proper nouns unique to the worked example's fictional e-commerce domain -- a
# real spec run through this pipeline (architecture review specs, per README)
# will not legitimately contain these, so this is a safe plain substring check,
# same zero-false-positive-risk class as Scribe's _DIFF_SYNTAX_TOKENS.
_EXAMPLE_DOMAIN_TOKENS = ("orderservice", "notificationservice", "sms provider")


def _normalize_for_comparison(text: str) -> str:
    """Lowercase, strip articles, and collapse whitespace before a fuzzy
    compare. Mirrors agents/scribe.py's helper of the same name."""
    text = _WHITESPACE_RE.sub(" ", text.strip().lower())
    text = _ARTICLE_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _detect_example_copying(parsed: dict) -> list[str]:
    """Flags any gaps/spofs/missing_integrations list entry that closely
    matches CRITIC_SYSTEM_PROMPT's worked example text -- exact OR
    near-verbatim (>=90% similar after normalizing case/whitespace/articles).
    Same pattern as agents/scribe.py's _detect_example_copying(), applied to
    list fields instead of single strings.
    """
    copied = []
    spofs = parsed.get("spofs") or []
    missing = parsed.get("missing_integrations") or []
    gap_descs = [g.get("description", "") for g in (parsed.get("gaps") or [])]

    for field, items in (("spofs", spofs), ("missing_integrations", missing), ("gaps", gap_descs)):
        for item in items:
            if not isinstance(item, str):
                continue
            normalized = _normalize_for_comparison(item)
            for example in _EXAMPLE_OUTPUT_STRINGS:
                ratio = SequenceMatcher(
                    None, normalized, _normalize_for_comparison(example)
                ).ratio()
                if ratio >= _COPY_SIMILARITY_THRESHOLD:
                    copied.append(field)
                    break
            else:
                continue
            break
    return copied


def _detect_example_domain_leak(parsed: dict) -> list[str]:
    """Flags any gaps/spofs/missing_integrations entry containing a proper
    noun unique to the worked example's fictional domain (OrderService,
    NotificationService, SMS provider) -- catches a domain leak that's been
    reworded enough to fall below the fuzzy-match threshold above. Same
    pattern as agents/scribe.py's _detect_example_domain_leak()."""
    leaked = []
    spofs = parsed.get("spofs") or []
    missing = parsed.get("missing_integrations") or []
    gap_descs = [g.get("description", "") for g in (parsed.get("gaps") or [])]

    for field, items in (("spofs", spofs), ("missing_integrations", missing), ("gaps", gap_descs)):
        for item in items:
            if not isinstance(item, str):
                continue
            lowered = item.lower()
            if any(token in lowered for token in _EXAMPLE_DOMAIN_TOKENS):
                leaked.append(field)
                break
    return leaked


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
    client = client or httpx.AsyncClient(timeout=CRITIC_HTTP_TIMEOUT_S)
    try:
        response = await client.post(LLAMA_SERVER_URL, json=payload)
        
        if response.status_code >= 400:
            raise ValueError(
                f"Critic: llama-server returned {response.status_code}. "
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
        validated = CriticOutput.model_validate(parsed)
    except ValidationError as e:
        raise ValueError(f"Critic: LFM output failed CriticOutput validation: {e}") from e

    dup_warnings = _detect_duplicate_list_items(parsed)
    for w in dup_warnings:
        print(f"WARNING: {w}")  # match whatever logging call scribe.py actually uses here

    cross_field_warnings = _detect_cross_field_duplication(parsed)
    for w in cross_field_warnings:
        print(f"WARNING: {w}")

    copied_fields = _detect_example_copying(parsed)
    if copied_fields:
        print(
            f"WARNING: Critic output for {copied_fields} closely matches "
            f"(exact or near-verbatim) a worked-example string from the system prompt"
        )

    leaked_fields = _detect_example_domain_leak(parsed)
    if leaked_fields:
        print(
            f"WARNING: Critic output for {leaked_fields} contains a worked-example "
            f"domain token (OrderService/NotificationService/SMS provider) unrelated "
            f"to this spec -- POSSIBLE EXAMPLE LEAK -- FLAG FOR HUMAN REVIEW"
        )

    return validated