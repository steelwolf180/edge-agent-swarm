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

from difflib import SequenceMatcher

from deepdiff import DeepDiff
from pydantic import ValidationError

from schemas.adr import ADROutput
from schemas.spec import ArchitectureSpec

from dotenv import load_dotenv
load_dotenv()

LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL") + "/v1/chat/completions"
LFM_MODEL_NAME = os.environ.get("LFM_MODEL_NAME")  # must match the model name in models.ini
SCRIBE_MAX_OUTPUT_TOKENS = int(os.environ.get("SCRIBE_TOKEN_BUDGET"))
SCRIBE_HTTP_TIMEOUT_S = int(os.environ.get("SCRIBE_HTTP_TIMEOUT_S"))

SCRIBE_SYSTEM_PROMPT = """You are the Scribe agent in an architecture review pipeline.
You write Architecture Decision Records (ADRs) in Context / Decision / Consequences form.
You have no tools. Base your ADR only on the spec diff and blackboard context given to you.

CRITICAL GROUNDING RULE: Your "decision", "consequences", and "diff_summary" fields must
describe ONLY the specific change shown in the "Spec diff" section below. Every noun phrase
in "decision" must be traceable to a specific line in the "Spec diff" or "Blackboard context"
sections you were given -- if you cannot point to the line that justifies a word or phrase,
do not write it. Do not invent, assume, or generalize to a larger architectural decision. Do
not discuss data centralization, service consolidation, tenant isolation, or any other topic
unless that exact topic appears verbatim in the diff text you were given.

CRITICAL NO-DIFF RULE: If the "Spec diff" section reads exactly "No field-level changes
detected.", there is nothing to decide and you MUST NOT invent a decision to fill the field.
In that exact case, respond with these exact field values and nothing else:
- "decision": "No meaningful decision to record: no spec changes were detected in this run."
- "consequences": "None. No architectural change occurred, so no consequence follows."
- "diff_summary": "No field-level changes detected."
Do not substitute your own wording for these three fields when the diff is empty, even if the
blackboard context describes an interesting system. Blackboard context explains the existing
system; it is not itself a change and must never be the source of a "decision" on a zero-diff run.

FORMATTING RULES (all fields):
- Never copy or quote the "Spec diff" or "Blackboard context" text verbatim into any field.
  Paraphrase concisely, in your own words, in 1-2 sentences per field.
- "context" must always be exactly the short fixed label: "L1 System Context". Do not put
  any other content in this field.
- "decision" and "consequences" must each be ONE sentence, no more than 25 words, specific
  to the one change in the diff -- not a summary of the entire architecture or blackboard context.

Worked examples:

IMPORTANT ABOUT THESE EXAMPLES: they illustrate FORMAT ONLY, and are deliberately about a
fictional glacier-monitoring sensor network -- a domain that will NEVER appear in a real
architecture spec you are given. This is intentional: if any example's specific wording shows
up in your actual output, it will be obviously, unmistakably wrong, because real specs in this
pipeline are business systems (chat support, e-commerce, RAG, etc.), never glacier sensors.
Copying an example's entity names, service names, or sentence content into your real output is
exactly as unacceptable as inventing content from nothing -- treat any overlap as a sign you
stopped reading the real "Spec diff" section and started reading this one instead.

Example 1 -- one small, real diff entry present:
Spec diff:
- dictionary_item_added: functional_requirements.integration_points[2] -> added 'satellite uplink for glacier-sensor telemetry'
Correct output: {"context": "L1 System Context", "decision": "Add a satellite uplink integration point for glacier-sensor telemetry.", "consequences": "The system gains a new outbound dependency on satellite uplink availability.", "diff_summary": "Added a satellite uplink integration point.", "affected_diagrams": ["context"]}
(Every word traces to the diff line above. If your real diff does not mention satellites or
glaciers, your output must not either -- this is a placeholder, not a hint.)

Example 2 -- no diff (spec_version 1, or a re-submitted spec identical to the prior version):
Spec diff:
No field-level changes detected.
Correct output: {"context": "L1 System Context", "decision": "No meaningful decision to record: no spec changes were detected in this run.", "consequences": "None. No architectural change occurred, so no consequence follows.", "diff_summary": "No field-level changes detected.", "affected_diagrams": ["context"]}
(Even though the blackboard context below may describe a rich system with a RAG pipeline, retrieval
service, etc., none of that is a *change*, so none of it appears in "decision". This is correct
even though it looks like a "boring" answer -- a boring correct answer beats an invented one.)

Example 3 -- large creation diff (spec_version 1, no prior spec at all -- baseline is empty, so
EVERY field in the current spec shows up as a change; this is the most common real-world case
you will see and the one most likely to tempt you toward a generic-sounding decision instead of
one grounded in what's actually listed):
Spec diff:
- dictionary_item_added: project_overview -> added (purpose, target_users, deployment_environment)
- dictionary_item_added: functional_requirements.core_features -> added ['ice-thickness measurement', 'crevasse-drift alerting']
- dictionary_item_added: functional_requirements.integration_points -> added ['Iridium satellite network', 'field base station radio']
- dictionary_item_added: non_functional_requirements -> added (performance, scalability, availability, security, observability)
- dictionary_item_added: technical_constraints -> added (language_framework, existing_systems, budget_infra_limits, team_skillset)
- dictionary_item_added: data_architecture -> added (data_sources, storage_requirements, data_flow, retention_compliance)
Correct output: {"context": "L1 System Context", "decision": "Establish the initial architecture: a glacier sensor network integrating with the Iridium satellite network and a field base station radio.", "consequences": "All subsequent changes will be diffed against this baseline spec version.", "diff_summary": "Initial spec creation: core features, integrations (Iridium satellite network, field base station radio), and full requirement set established.", "affected_diagrams": ["context"]}
(Pick out only what the real diff lines actually list -- here, the Iridium network and base
station radio, and the two core features -- rather than reaching for a plausible-sounding but
unlisted detail. Six top-level sections were added, but the decision only names what's specific
and load-bearing, not every field. Your real diff will list real business-system services, not
satellites or glaciers -- name those instead, and only those.)

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


def _non_empty_field_names(section: dict) -> list[str]:
    """Names (not values) of fields in a section dict that actually hold
    content -- mirrors Example 3's 'project_overview -> added (purpose,
    target_users, deployment_environment)' bullet, which only makes sense
    to emit for fields that were actually populated."""
    return [k for k, v in section.items() if v not in (None, "", [], {})]


def summarize_creation_diff(
    current_spec: ArchitectureSpec, max_items: int = 8
) -> tuple[str, int]:
    """Build the diff summary for a *creation* diff (prior_spec=None,
    spec_version 1) directly from current_spec, bypassing
    compute_spec_diff()/DeepDiff entirely for this one case.

    Root cause (confirmed by direct DeepDiff testing, not inference):
    DeepDiff({}, populated_dict) only emits per-key 'dictionary_item_added'
    entries when a single top-level key is added. The moment 2+ top-level
    keys are added against a *completely empty* dict baseline, DeepDiff
    collapses the whole comparison into one 'values_changed: root ->
    changed from {} to {...huge dict...}' entry instead -- a quirk of an
    empty starting dict specifically, not a bug in how this diff is
    invoked. Every real creation run (prior_spec=None) hits this, since
    compute_spec_diff()'s baseline is always {} in that case.

    This is the leading hypothesis for the persistent fabrication/example-
    copying failure (KICKOFF_CHECKLIST §8.1): SCRIBE_SYSTEM_PROMPT's
    Example 3 trains the model to expect clean, per-section
    'dictionary_item_added: <section> -> added (...)' bullets on a
    creation diff, but the model was actually being shown one giant
    root-level dict-repr blob -- a shape it had never been primed on -- so
    it fell back to pattern-matching the nearest thing it *had* seen
    worked-example output for for: Example 3 itself, verbatim or near-
    verbatim. Constructing the bullets directly from current_spec, in the
    same shape Example 3 uses, removes that shape mismatch at its source
    rather than only detecting the copy after the fact.

    Returns (summary_text, hunk_count). hunk_count is unbounded by
    max_items (same contract as hunk_count_from_diff()) since it feeds
    Judge's adrs_per_diff metric, not the prompt.
    """
    dump = current_spec.model_dump()
    bullets: list[str] = []

    for key, value in dump.items():
        if value in (None, "", [], {}):
            continue

        if isinstance(value, list):
            bullets.append(f"dictionary_item_added: {key} -> added {value!r}")
            continue

        if isinstance(value, dict):
            # Non-empty nested lists get their own bullet (mirrors Example
            # 3's functional_requirements.core_features /
            # .integration_points bullets); remaining non-empty scalar
            # subfields are named together in one bullet (mirrors Example
            # 3's 'project_overview -> added (purpose, ...)' bullet).
            list_subfields = {
                k: v for k, v in value.items() if isinstance(v, list) and v
            }
            for subkey, sublist in list_subfields.items():
                bullets.append(
                    f"dictionary_item_added: {key}.{subkey} -> added {sublist!r}"
                )

            remaining = {k: v for k, v in value.items() if k not in list_subfields}
            named_fields = _non_empty_field_names(remaining)
            if named_fields:
                bullets.append(
                    f"dictionary_item_added: {key} -> added ({', '.join(named_fields)})"
                )
            continue

        # Bare top-level scalar -- not expected in the current schema shape,
        # but handled explicitly rather than silently dropped.
        bullets.append(f"dictionary_item_added: {key} -> added {value!r}")

    hunk_count = len(bullets)
    summary = (
        "\n".join(bullets[:max_items]) if bullets else "No field-level changes detected."
    )
    return summary, hunk_count

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

# Exact strings from SCRIBE_SYSTEM_PROMPT's worked-example "Correct output" values.
# Confirmed twice (13 Aug: Stripe/payment example; 14 Aug: Zendesk/Confluence example)
# that LFM reproduces a worked example's output verbatim instead of grounding in the
# real diff, despite an explicit in-prompt instruction not to -- prompt-only fixes
# have now failed identically twice, so this is a boundary-level backstop rather
# than a third attempt at asking more firmly. Kept in sync manually with the
# examples in SCRIBE_SYSTEM_PROMPT; if the examples change, update this set too.
_EXAMPLE_OUTPUT_STRINGS = {
    "Add a satellite uplink integration point for glacier-sensor telemetry.",
    "The system gains a new outbound dependency on satellite uplink availability.",
    "Added a satellite uplink integration point.",
    "No meaningful decision to record: no spec changes were detected in this run.",
    "None. No architectural change occurred, so no consequence follows.",
    "No field-level changes detected.",  # legitimate for a genuine zero-diff run -- see note below
    "Establish the initial architecture: a glacier sensor network integrating with the Iridium satellite network and a field base station radio.",
    "All subsequent changes will be diffed against this baseline spec version.",
    "Initial spec creation: core features, integrations (Iridium satellite network, field base station radio), and full requirement set established.",
    # Prior example generations, kept in case any cached/older prompt is still in
    # play somewhere -- costs nothing to keep checking for these too.
    "Add a Stripe webhook integration point for payment confirmation.",
    "The system gains an inbound external dependency on Stripe's webhook delivery.",
    "Added a Stripe webhook integration point.",
    "Establish the initial architecture: a chat-based support system integrating with Zendesk and Confluence.",
    "Initial spec creation: core features, integrations (Zendesk, Confluence), and full requirement set established.",
}


_COPY_SIMILARITY_THRESHOLD = 0.90

_ARTICLE_RE = re.compile(r"\b(a|an|the)\b")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_for_comparison(text: str) -> str:
    """Lowercase, strip articles, and collapse whitespace before a fuzzy
    compare.

    Confirmed on workflow 726ed8f9 (14 Aug 2026): the model reproduced
    Example 3's "decision" string with only three words dropped ("the",
    "the", "a" -- articles), which is 96% similar by SequenceMatcher on the
    raw strings but NOT an exact match, so it silently passed the old
    exact-match check while consequences/diff_summary (copied verbatim)
    got caught. Article-stripping means this specific evasion collapses to
    an exact normalized match rather than merely a high ratio, but the
    ratio check below is kept as the general backstop for other kinds of
    near-verbatim drift (a swapped word, a dropped clause, etc.).
    """
    text = _WHITESPACE_RE.sub(" ", text.strip().lower())
    text = _ARTICLE_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _detect_example_copying(parsed: dict, diff_summary: str) -> list[str]:
    """Flags any of decision/consequences/diff_summary that closely match a
    worked-example output string -- exact OR near-verbatim (>=90% similar
    after normalizing case/whitespace/articles).

    Widened from exact-match (13-14 Aug fix) after confirming the model can
    evade a strict string check by dropping a handful of articles while
    reproducing an example's content and sentence structure otherwise
    unchanged. This is still a narrow backstop, not general paraphrase
    detection -- it only catches output that is *structurally* the same
    sentence as a worked example, which is what "copied the example instead
    of grounding in the diff" looks like. It will not catch genuinely novel
    fabricated content that merely echoes the example's *topic* (glaciers,
    satellites) in different words; that class of failure still depends on
    the prompt's grounding rules holding.

    "No field-level changes detected." is legitimate when the real diff
    actually was empty (Example 2's case) -- only flagged here if
    diff_summary itself doesn't match, i.e. the real diff was non-empty but
    the model still emitted the empty-diff text.
    """
    copied = []
    for field in ("decision", "consequences", "diff_summary"):
        value = parsed.get(field)
        if not isinstance(value, str):
            continue
        stripped = value.strip()

        if stripped == "No field-level changes detected.":
            if diff_summary.strip() != "No field-level changes detected.":
                copied.append(field)
            continue

        normalized = _normalize_for_comparison(stripped)
        for example in _EXAMPLE_OUTPUT_STRINGS:
            if example == "No field-level changes detected.":
                continue  # handled above; legitimate on a real zero-diff run
            ratio = SequenceMatcher(
                None, normalized, _normalize_for_comparison(example)
            ).ratio()
            if ratio >= _COPY_SIMILARITY_THRESHOLD:
                copied.append(field)
                break
    return copied


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
    # Route creation diffs (prior_spec=None) through summarize_creation_diff()
    # instead of compute_spec_diff()/DeepDiff -- see that function's
    # docstring for why: DeepDiff({}, populated_dict) collapses to one
    # root-level values_changed blob rather than the clean per-section
    # bullets SCRIBE_SYSTEM_PROMPT's Example 3 primes the model to expect,
    # which is the leading hypothesis for the persistent fabrication/
    # example-copying behavior on every cloud_rag.json creation run
    # (§8.1). Incremental diffs (prior_spec present) are unaffected --
    # DeepDiff already produces clean per-field entries once the baseline
    # has real content -- so that path is untouched.
    if prior_spec is None:
        diff_summary, hunk_count = summarize_creation_diff(current_spec)
    else:
        diff = compute_spec_diff(prior_spec, current_spec)
        diff_summary = summarize_diff(diff)
        hunk_count = hunk_count_from_diff(diff)

    # Auditability, not behavior: the only record of what Scribe was actually
    # shown used to be its own restated diff_summary field in the output --
    # which is exactly the thing in question when fabrication is suspected.
    # Logging the real diff text here means a future "is this grounded?"
    # question can be answered by reading the run log instead of re-deriving
    # prior_spec from memory or guessing at the invocation, as happened for
    # workflow 6b8a7752 (13 Aug 2026) where "no --prior-spec was passed" had
    # to be confirmed after the fact via the human who ran it.
    print(
        f"INFO: Scribe diff fed to prompt (prior_spec={'present' if prior_spec else 'None (creation diff)'}, "
        f"hunk_count={hunk_count}):\n{diff_summary}"
    )

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
    client = client or httpx.AsyncClient(timeout=SCRIBE_HTTP_TIMEOUT_S)
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

    copied_fields = _detect_example_copying(parsed, diff_summary)
    if copied_fields:
        print(
            f"WARNING: Scribe output for {copied_fields} closely matches "
            f"(exact or near-verbatim) a worked-example string from the "
            f"system prompt -- the model appears to have copied the example "
            f"instead of grounding in the real diff. Flagging inline rather "
            f"than retrying, since temperature=0.05 makes an identical "
            f"retry likely to reproduce the same copy."
        )
        for field in copied_fields:
            parsed[field] = f"POSSIBLE EXAMPLE COPY -- FLAG FOR HUMAN REVIEW: {parsed[field]}"
        salvage_reason = salvage_reason or "example_copied"

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