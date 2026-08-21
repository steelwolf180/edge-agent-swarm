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

# --- Scribe system prompt, built conditionally on whether this run's diff is
# genuinely empty. Split out of one monolithic string (16 Aug 2026 fix) after
# the bracket-placeholder fix (15 Aug 2026) closed Examples 1/3 as copy
# targets but redirected the same underlying behavior onto Example 2 --
# confirmed workflow (§8.1e, third run that evening): the model reproduced
# Example 2's zero-diff boilerplate wholesale against a real 11-hunk creation
# diff. Example 2's output can't be bracket-slotted like 1/3 -- it has to stay
# a literal, memorizable string, because that literal string is the CORRECT
# answer on a genuine zero-diff run (CRITICAL NO-DIFF RULE requires an exact
# match). But that's exactly what makes it a copy target: after 1/3 lost
# their concrete text, Example 2 -- duplicated a second time in the CRITICAL
# NO-DIFF RULE block itself -- became the only finished, quotable, low-effort
# text left anywhere in the prompt, and the model reached for it under
# grounding pressure on a dense diff, same mechanism as before, new target.
#
# Fix follows the pattern that actually worked for the §8.1b diff-syntax leak:
# remove the copyable text from context at the source rather than adding a
# further instruction not to use it (already tried and already failed once
# for a different leak in this same file). run_scribe() knows diff_summary
# before it builds the payload, so the CRITICAL NO-DIFF RULE block and
# Example 2 are only included in the system prompt on an actual zero-diff
# run. On a real-diff run, that text simply isn't in context to copy.
#
# NOT YET CONFIRMED against a real run -- this is the untested half of the
# §8.1e P0 root-cause hypothesis, not a landed fix. Needs two runs to close:
# one real-diff run (confirms Example 2 leak stops) and one genuine zero-diff
# run (confirms the NO-DIFF branch still produces correct boilerplate, so the
# legitimate case isn't broken while fixing the illegitimate one).

_SCRIBE_PROMPT_HEADER = """You are the Scribe agent in an architecture review pipeline.
You write Architecture Decision Records (ADRs) in Context / Decision / Consequences form.
You have no tools. Base your ADR only on the spec diff and blackboard context given to you.

CRITICAL GROUNDING RULE: Your "decision", "consequences", and "diff_summary" fields must
describe ONLY the specific change shown in the "Spec diff" section below -- and ONLY the
"Spec diff" section. Every noun phrase in "decision" must be traceable to a specific line
in the "Spec diff" section you were given -- if you cannot point to the diff line that
justifies a word or phrase, do not write it. The "Blackboard context" section is background
for understanding the system, not a source of content for these three fields: never pull a
service name, dependency, or detail into "decision", "consequences", or "diff_summary" just
because it appears in "Blackboard context" -- if it isn't in the diff, it isn't a change, and
it doesn't belong in these fields, no matter how relevant-sounding it is. Do not invent,
assume, or generalize to a larger architectural decision. Do not discuss data centralization,
service consolidation, tenant isolation, or any other topic unless that exact topic appears
verbatim in the diff text you were given.

DIFF FORMAT IS NOT CONTENT: each line in the "Spec diff" section starts with a short verb
(Added / Removed / Changed) followed by a spec field path and a colon, e.g. "Added
project_overview: purpose, target_users, deployment_environment". This is structural
formatting for you to read, not a sentence to copy. Two specific things are always wrong:
(1) writing the field path itself into your output -- a path like
"functional_requirements.integration_points[2]" or "project_overview" is a schema location,
not a business-domain phrase, and never belongs in "decision", "consequences", or
"diff_summary"; and (2) echoing the diff line's own shape back, e.g. "Added project_overview:
purpose, target_users, deployment_environment" restated almost verbatim as the decision. If a
diff line reads "Added project_overview: purpose, target_users, deployment_environment", the
change is that the project's purpose, target users, and deployment environment were defined --
write that in plain English ("Define the project's purpose, target users, and deployment
environment."), never a reworded copy of the diff line itself.

A third specific trap, and the easiest one to fall into: some diff bullets (for example
functional_requirements.key_user_flows) are themselves written as full narrative sentences,
often chained together with "->" arrows describing a user's step-by-step flow. Because these
bullets already read like connected prose instead of a terse field list, they are the single
most tempting thing anywhere in the diff to copy directly into "decision" -- but a bullet like
this is exactly as much diff content, not decision content, as any other line, and the same
rule applies without exception: find the one load-bearing point across all such bullets, state
it in your own words, in ONE sentence, and never carry over the "->" chain, the bullet's own
sentence boundaries, or more than one bullet's worth of detail."""

# Only included when this run's diff is genuinely empty (see _build_scribe_system_prompt).
_SCRIBE_ZERO_DIFF_RULE = """

CRITICAL NO-DIFF RULE: If the "Spec diff" section reads exactly "No field-level changes
detected.", there is nothing to decide and you MUST NOT invent a decision to fill the field.
In that exact case, respond with these exact field values and nothing else:
- "decision": "No meaningful decision to record: no spec changes were detected in this run."
- "consequences": "None. No architectural change occurred, so no consequence follows."
- "diff_summary": "No field-level changes detected."
Do not substitute your own wording for these three fields when the diff is empty, even if the
blackboard context describes an interesting system. Blackboard context explains the existing
system; it is not itself a change and must never be the source of a "decision" on a zero-diff run."""

_SCRIBE_FORMATTING_RULES = """

FORMATTING RULES (all fields):
- Never copy or quote the "Spec diff" or "Blackboard context" text verbatim into any field.
  Paraphrase concisely, in your own words, in 1-2 sentences per field.
- "context" must always be exactly the short fixed label: "L1 System Context". Do not put
  any other content in this field.
- "decision" and "consequences" must each be ONE sentence, no more than 25 words, specific
  to the one change in the diff -- not a summary of the entire architecture or blackboard context.

Worked examples:

IMPORTANT ABOUT THESE EXAMPLES: every bracketed [SLOT] below describes what belongs there -- it
is not literal text, and if your real output still contains a square bracket you have failed to
fill in a slot. These examples contain no invented service names, company names, or business
content in any domain, on purpose. Earlier versions of this prompt used a fictional
glacier-monitoring domain instead, on the theory that any leak of it into real output would be
obviously wrong -- but the model reproduced that fictional domain's exact wording into real
output anyway (confirmed workflow 3303ce31-...), after two earlier real-domain example versions
(Stripe/payment, then Zendesk/Confluence) leaked the same way before that. Concrete example
content -- fictional or real, on-topic or not -- is something this model has repeatedly copied
instead of grounding in the real diff, so these examples now show structure only, with nothing
concrete left to copy. Every noun in your real output must come from the real "Spec diff"
section given to you below, because there is nothing else it could legitimately come from."""

_SCRIBE_EXAMPLE_1 = """

Example 1 -- one small, real diff entry present:
Spec diff:
- Added functional_requirements.integration_points[2]: [your real diff line will name one real integration here]
Correct output shape: {"context": "L1 System Context", "decision": "[one sentence, <=25 words, naming only the specific item your real diff line above added]", "consequences": "[one sentence: the concrete new dependency or effect that follows from that addition]", "diff_summary": "[short paraphrase of your real diff line above, not its field-path syntax]", "affected_diagrams": ["context"]}
(Every word in your real answer traces to your real diff line, whatever it says -- not to this
placeholder text.)"""

# Only included when this run's diff is genuinely empty (see _build_scribe_system_prompt).
# This is the one remaining example with finished, literal, quotable prose -- see the
# module-level note above for why that makes it a copy target on non-empty-diff runs,
# and why it's withheld from the prompt entirely rather than merely discouraged.
_SCRIBE_EXAMPLE_2 = """

Example 2 -- no diff (spec_version 1, or a re-submitted spec identical to the prior version):
Spec diff:
No field-level changes detected.
Correct output: {"context": "L1 System Context", "decision": "No meaningful decision to record: no spec changes were detected in this run.", "consequences": "None. No architectural change occurred, so no consequence follows.", "diff_summary": "No field-level changes detected.", "affected_diagrams": ["context"]}
(This exact answer is correct even if the blackboard context below describes a rich, detailed
system -- none of that is a *change*, so none of it belongs in "decision". A boring correct
answer beats an invented one.)"""

_SCRIBE_EXAMPLE_3 = """

Example 3 -- large creation diff (spec_version 1, no prior spec at all -- baseline is empty, so
EVERY field in the current spec shows up as a change; this is the most common real-world case
you will see and the one most likely to tempt you toward a generic-sounding decision instead of
one grounded in what's actually listed):
Spec diff:
- Added project_overview: purpose, target_users, deployment_environment
- Added functional_requirements.core_features: [your real diff lists the actual core features here]
- Added functional_requirements.integration_points: [your real diff lists the actual integrations here]
- Added non_functional_requirements: performance, scalability, availability, security, observability
- Added technical_constraints: language_framework, existing_systems, budget_infra_limits, team_skillset
- Added data_architecture: data_sources, storage_requirements, data_flow, retention_compliance
Correct output shape: {"context": "L1 System Context", "decision": "[one sentence naming only the specific core features and integration points your real diff bullets above actually list -- not every section name, just what's specific and load-bearing]", "consequences": "All subsequent changes will be diffed against this baseline spec version.", "diff_summary": "[short paraphrase: core features established, the named integrations, and the full requirement set added]", "affected_diagrams": ["context"]}
(Six top-level sections were added here, but "decision" only names what's specific and
load-bearing from YOUR real diff -- the actual core features and integrations you were given,
never a placeholder and never wording from a different run. Everything inside a bracket above
must be replaced with real content from your own diff, and nothing inside a bracket should ever
survive into your output verbatim.)"""

_SCRIBE_PROMPT_FOOTER = """

Respond with a single JSON object matching this schema, and nothing else:
{"context": str, "decision": str, "consequences": str, "diff_summary": str, "affected_diagrams": ["context"]}
For this MVP, affected_diagrams must always be exactly ["context"] -- L1 System Context is the
only diagram level in scope. Do not emit "container" even if the diff suggests a container-level change."""


def _build_scribe_system_prompt(zero_diff: bool) -> str:
    """Compose SCRIBE_SYSTEM_PROMPT, including the CRITICAL NO-DIFF RULE and
    Example 2 only when this run's diff is genuinely empty. See the module-
    level note above _SCRIBE_PROMPT_HEADER for why: that block's finished,
    literal text is a confirmed copy target on real-diff runs, so it's
    withheld from context entirely rather than merely discouraged in-prompt
    when it doesn't apply.
    """
    parts = [_SCRIBE_PROMPT_HEADER]
    if zero_diff:
        parts.append(_SCRIBE_ZERO_DIFF_RULE)
    parts.append(_SCRIBE_FORMATTING_RULES)
    parts.append(_SCRIBE_EXAMPLE_1)
    if zero_diff:
        parts.append(_SCRIBE_EXAMPLE_2)
    parts.append(_SCRIBE_EXAMPLE_3)
    parts.append(_SCRIBE_PROMPT_FOOTER)
    return "".join(parts)


# Backward-compat alias for any test or call site that still imports the
# static name directly -- equivalent to the zero_diff=True (full) prompt.
# run_scribe() itself always calls _build_scribe_system_prompt() fresh, per
# request, so it never uses this stale alias.
SCRIBE_SYSTEM_PROMPT = _build_scribe_system_prompt(zero_diff=True)


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


# DeepDiff's raw category names (e.g. "dictionary_item_added") were, until this
# fix, interpolated directly into the prompt as the leading word of every diff
# bullet -- and SCRIBE_SYSTEM_PROMPT's own worked examples showed that exact
# shape as "normal" input. That combination is the confirmed root cause of the
# §8.1b diff-syntax leak (workflow 7570a555-...): the model wasn't fabricating
# or refusing an instruction, it was pattern-matching a template it had
# genuinely been shown twice (once in the real diff, once in the example).
# _detect_diff_syntax_leak() catches the symptom after the fact; this mapping
# removes the token from the input entirely so there's nothing shaped like
# itself for the model to echo. Every DeepDiff tree-view category currently
# reachable from compute_spec_diff() (ignore_order=True) must have an entry
# here -- an unmapped category falls back to "Changed", which is safe (never a
# banned token) but less precise, so add new categories here if DeepDiff ever
# surfaces one this mapping doesn't cover, rather than letting it fall back
# silently forever.
_CHANGE_TYPE_VERBS = {
    "dictionary_item_added": "Added",
    "dictionary_item_removed": "Removed",
    "iterable_item_added": "Added to",
    "iterable_item_removed": "Removed from",
    "values_changed": "Changed",
    "type_changes": "Changed the type of",
}

_DEEPDIFF_PATH_RE = re.compile(r"\['([^']+)'\]|\[(\d+)\]")

def _clean_diff_path(path: str) -> str:
    """Convert DeepDiff's raw tree-view path repr (root['a']['b'][2]) into
    the dotted/bracketed field-path style already used safely in
    summarize_creation_diff() and shown as normal in SCRIBE_SYSTEM_PROMPT's
    own examples (e.g. 'functional_requirements.integration_points[2]').
    DeepDiff's 'root[...]' syntax was never in scope for the 8.1a/8.1b
    token-leak fixes -- those only translated the category verb, not the
    path string itself -- and summarize_diff() is the only path-summarizing
    function that still passes a DeepDiff-native string through unmodified.
    """
    parts = []
    for key, idx in _DEEPDIFF_PATH_RE.findall(path):
        if key:
            parts.append(f".{key}" if parts else key)
        else:
            parts.append(f"[{idx}]")
    return "".join(parts) or path

def _describe_change(change_type: str, path: str, detail) -> str:
    """Render one DeepDiff change entry as a plain-English sentence fragment,
    with no DeepDiff category token and no '->' arrow -- both named as banned
    output in SCRIBE_SYSTEM_PROMPT's DIFF FORMAT IS NOT CONTENT rule, and both
    now absent from the input too, so the rule has nothing left to guard
    against on this path. _format_diff_detail() still does the value-shape
    work (no raw dict reprs) it was written for on 6 Aug; this only replaces
    the leading label and connector."""
    verb = _CHANGE_TYPE_VERBS.get(change_type, "Changed")
    return f"{verb} {_clean_diff_path(path)}: {_format_diff_detail(detail)}"


def summarize_diff(diff: DeepDiff, max_items: int = 8) -> str:
    """Compress a DeepDiff into a short bullet list for the prompt.

    Truncated to max_items to stay inside the ~800 token Scribe budget (spec §4,
    Context Window Budget: Scribe ~800 tokens).
    """
    lines: list[str] = []
    for change_type, items in diff.to_dict().items():
        for path, detail in list(items.items())[:max_items]:
            lines.append(f"- {_describe_change(change_type, path, detail)}")
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
    hunk_count = 0  # counted independently of len(bullets) -- see
    # key_user_flows below, which is a real change (increments this) but
    # is never turned into a bullet the LLM sees.

    for key, value in dump.items():
        if value in (None, "", [], {}):
            continue

        if isinstance(value, list):
            bullets.append(f"Added {key}: {value!r}")
            hunk_count += 1
            continue

        if isinstance(value, dict):
            # Non-empty nested lists get their own bullet (mirrors Example
            # 3's functional_requirements.core_features /
            # .integration_points bullets); remaining non-empty scalar
            # subfields are named together in one bullet (mirrors Example
            # 3's 'project_overview: purpose, ...' bullet). Same prose shape
            # as _describe_change() above -- no DeepDiff category token, no
            # '->' arrow -- kept as a hand-written mirror rather than routed
            # through _describe_change() itself since this path never has a
            # real DeepDiff change_type to look up (everything here is a
            # creation-diff addition by construction).
            list_subfields = {
                k: v for k, v in value.items() if isinstance(v, list) and v
            }
            for subkey, sublist in list_subfields.items():
                if subkey == "key_user_flows":
                    hunk_count += 1
                    continue
                bullets.append(f"Added {key}.{subkey}: {sublist!r}")
                hunk_count += 1

            remaining = {k: v for k, v in value.items() if k not in list_subfields}
            named_fields = _non_empty_field_names(remaining)
            if named_fields:
                bullets.append(f"Added {key}: {', '.join(named_fields)}")
                hunk_count += 1
            continue

        # Bare top-level scalar -- not expected in the current schema shape,
        # but handled explicitly rather than silently dropped.
        bullets.append(f"Added {key}: {value!r}")
        hunk_count += 1

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


def _find_matching_bracket(text: str, open_idx: int) -> int | None:
    """Given text[open_idx] == '[', return the index of its matching ']',
    skipping over bracket characters that appear inside double-quoted
    strings (respecting backslash escapes). Returns None if unbalanced
    (e.g. the array itself never closes before the string ends)."""
    depth = 0
    in_string = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


_MAX_LIST_COERCE_CHARS = 300  # matches _MAX_COERCED_FIELD_CHARS's convention


def _salvage_list_valued_field(stripped: str, field: str) -> str | None:
    """Best-effort recovery when a field that must be a single string
    instead came back as a JSON array -- confirmed workflow b67bea1a-...
    (19 Aug 2026): 'decision' returned as an array whose elements were
    near-verbatim copies of individual core_features entries straight from
    the diff bullet, instead of one <=25-word sentence. That also broke
    JSON parsing outright (an unescaped quote inside one of the copied diff
    entries), and _STRING_FIELD_RE -- which only matches `"field": "string"`
    -- never matches this shape at all, so without this the field fell
    straight to the unrecoverable MISSING placeholder. A real coverage gap
    in the extractor, not a judgment call about content quality.

    This deliberately does NOT try to reconstruct a clean one-sentence
    field from the list -- that would be writing new content on the
    model's behalf, not salvaging. It extracts what it can, caps it, and
    prefixes an explicit flag so a human reviewer sees immediately this is
    a coercion artifact of a schema-violating list, not a real (if verbose)
    answer -- same spirit as _coerce_adr_string_fields()'s stringify-and-
    flag backstop for the case where json.loads() succeeds but a field's
    *type* is still wrong.

    Whether the underlying behavior (dumping diff content into a list
    instead of synthesizing one sentence) is a new failure mode or a
    known-class recurrence on a new field is a separate, still-open
    question -- see KICKOFF_CHECKLIST.md. This function only prevents a
    total data loss on that failure, it doesn't diagnose or fix why the
    model produced a list in the first place.

    Returns None if the field isn't list-shaped at all, or if nothing
    usable could be pulled out of it -- caller falls back to the existing
    missing/truncated placeholders in that case.
    """
    match = re.search(rf'"{field}"\s*:\s*\[', stripped)
    if not match:
        return None

    open_idx = match.end() - 1  # index of the '['
    close_idx = _find_matching_bracket(stripped, open_idx)
    bracket_text = stripped[open_idx:close_idx + 1] if close_idx is not None else stripped[open_idx:]

    items: list[str] = []
    try:
        parsed_list = json.loads(bracket_text)
        if isinstance(parsed_list, list):
            items = [str(item) for item in parsed_list]
    except (json.JSONDecodeError, ValueError):
        # The array itself is malformed too (e.g. an unescaped quote inside
        # one entry, which is what broke parsing in the first place) --
        # fall back to grabbing whatever quoted substrings are extractable,
        # rather than giving up entirely.
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', bracket_text)

    joined = "; ".join(item.strip() for item in items if item.strip())
    if not joined:
        return None

    truncated = joined[:_MAX_LIST_COERCE_CHARS]
    if len(joined) > _MAX_LIST_COERCE_CHARS:
        truncated = truncated.rstrip() + "..."

    return (
        "LIST-COERCED (flag for review -- model returned a list instead of "
        "a string; this is a truncated join of that list's contents, not a "
        f"synthesized answer): {truncated}"
    )


def _salvage_truncated_scribe_output(raw: str, cause: str = "truncated") -> dict:
    """Best-effort recovery when LFM hits max_tokens mid-generation, or
    produces structurally malformed JSON on a *complete* generation.

    Confirmed reproducible at temperature=0.05 (identical output across
    retries) -- not sampling noise a retry can route around. Rather than
    raising and burning a retry that will fail identically, extract every
    field that closed as a valid JSON string, salvage the in-progress
    field at its last complete sentence, and mark anything that never
    started with an explicit placeholder for human review at the approval
    gate -- never a silent blank.

    `cause` controls only the wording of the placeholder text for
    unrecovered fields, not the recovery logic itself. Originally this
    function was truncation-only and every placeholder said "generation
    exceeded token budget" -- accurate for that case, but wrong and
    misleading when reused for the malformed_json case (workflow
    2db3d647-...), where finish_reason='stop' means the token budget was
    never the problem. Callers pass the real cause instead of this
    function assuming truncation.
    """
    if cause == "malformed_json":
        truncated_msg = "TRUNCATED: field was cut short by malformed JSON output, not by the token budget"
        missing_msg = "MISSING: field could not be recovered from malformed JSON output"
    else:
        truncated_msg = "TRUNCATED: generation exceeded token budget before completing this field"
        missing_msg = "MISSING: generation exceeded token budget before this field started"

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
            salvaged[field] = trimmed or truncated_msg
            continue

        list_coerced = _salvage_list_valued_field(stripped, field)
        if list_coerced:
            salvaged[field] = list_coerced
            continue

        salvaged[field] = missing_msg

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

# DeepDiff's own internal category/bookkeeping tokens. These can never
# legitimately appear in natural-English ADR prose, so (unlike example-
# copying, which needs a similarity threshold) a plain substring check is
# fully reliable here with zero false-positive risk -- same backstop class
# as _coerce_adr_string_fields(), not a judgment call.
#
# Added after the SCRIBE_SYSTEM_PROMPT rule prohibiting this (13 Aug fix)
# was confirmed to have no effect on a same-spec re-run (workflow
# 7570a555-...): "decision" and "diff_summary" came back byte-identical
# to the pre-fix output, still containing "dictionary_item_added" verbatim,
# at temperature=0.05 (deterministic, not sampling noise a retry would
# route around). One clean failure of a prompt-only fix on a fully
# unambiguous pattern is enough to justify a boundary backstop immediately,
# rather than burning further ~5-6 min pipeline runs testing prompt wording
# iterations -- same reasoning already applied to the example-copying guard.
_DIFF_SYNTAX_TOKENS = (
    "dictionary_item_added",
    "dictionary_item_removed",
    "iterable_item_added",
    "iterable_item_removed",
    "values_changed",
    "type_changes",
)

# Note (§8.1b source fix): summarize_diff()/summarize_creation_diff() no longer
# put any of these tokens in front of the model at all -- _describe_change()
# and its creation-diff equivalent translate every DeepDiff category into a
# plain verb (Added/Removed/Changed) before it reaches the prompt. This check
# should therefore never fire again in normal operation; it stays as a
# zero-cost backstop in case a future DeepDiff category is added upstream
# without a corresponding entry in _CHANGE_TYPE_VERBS (which falls back to the
# safe, non-token "Changed" rather than surfacing a raw category name).


def _detect_diff_syntax_leak(parsed: dict) -> list[str]:
    """Flags any of decision/consequences/diff_summary that leak DeepDiff's
    internal category tokens verbatim, instead of paraphrasing the change
    in plain English. See _DIFF_SYNTAX_TOKENS for why this is a safe plain
    substring check rather than a fuzzy-match judgment call."""
    leaked = []
    for field in ("decision", "consequences", "diff_summary"):
        value = parsed.get(field)
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        if any(token in lowered for token in _DIFF_SYNTAX_TOKENS):
            leaked.append(field)
    return leaked


# Introduced alongside the switch to bracketed [SLOT] worked examples (15 Aug
# 2026 fix, replacing the third leaked concrete-domain example in a row --
# see SCRIBE_SYSTEM_PROMPT's "IMPORTANT ABOUT THESE EXAMPLES" note). That
# change trades "the model has a concrete sentence it can copy" for a new,
# narrower failure mode: the model echoes a literal "[...]" placeholder
# instead of filling it in. A real ADR sentence never legitimately contains
# a square bracket, so -- same class as _DIFF_SYNTAX_TOKENS -- this is a
# zero-false-positive substring check, not a judgment call. Not yet
# confirmed against a real run; this is a preemptive backstop for the new
# prompt shape, not a response to an observed failure.
def _detect_bracket_leak(parsed: dict) -> list[str]:
    """Flags any of decision/consequences/diff_summary that still contain a
    literal '[' or ']' -- i.e. the model echoed a worked-example placeholder
    slot instead of replacing it with real content from the diff."""
    leaked = []
    for field in ("decision", "consequences", "diff_summary"):
        value = parsed.get(field)
        if not isinstance(value, str):
            continue
        if "[" in value or "]" in value:
            leaked.append(field)
    return leaked


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


# Proper nouns unique to SCRIBE_SYSTEM_PROMPT's fictional worked-example domain
# (glacier monitoring). Per the prompt's own IMPORTANT ABOUT THESE EXAMPLES note,
# real specs in this pipeline are business systems and will NEVER legitimately
# contain these words -- so, like _DIFF_SYNTAX_TOKENS, this is a safe plain
# substring check with zero false-positive risk, not a judgment call.
#
# Added after workflow 3303ce31-... (§8.1b re-run, 15 Aug 2026) surfaced a
# template-fill variant of example-copying that evades _detect_example_copying()'s
# 0.90 similarity threshold: "decision" kept Example 3's exact sentence shape and
# the literal phrase "glacier sensor network" but swapped in real entities (AWS
# S3, AWS RDS, Confluence) for the rest of the sentence. Enough words changed to
# drop the full-string ratio below 0.90 -- consequences/diff_summary (copied with
# fewer substitutions) were correctly flagged, decision was not -- even though a
# half-real, half-fictional sentence is, if anything, more likely to be rubber-
# stamped by a reviewer than an obvious full copy would be.
_EXAMPLE_DOMAIN_TOKENS = (
    "glacier",
    "iridium",
    "crevasse",
    "ice-thickness",
    "sensor network",
    "base station radio",
    "satellite uplink",
)


def _detect_example_domain_leak(parsed: dict) -> list[str]:
    """Flags any of decision/consequences/diff_summary containing a proper
    noun unique to the worked examples' fictional domain. Complements
    _detect_example_copying(): that check catches full-sentence structural
    copies via fuzzy similarity; this catches a template-fill hybrid where
    the sentence shape and surrounding entities are real but a fictional-
    domain word rode along from the example anyway."""
    leaked = []
    for field in ("decision", "consequences", "diff_summary"):
        value = parsed.get(field)
        if not isinstance(value, str):
            continue
        lowered = value.lower()
        if any(token in lowered for token in _EXAMPLE_DOMAIN_TOKENS):
            leaked.append(field)
    return leaked


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


# Common English function words plus a few high-frequency, content-free words
# that show up in almost every ADR sentence regardless of topic (e.g. "system",
# "new", "change") -- excluded so the overlap check below measures shared
# *topical* vocabulary, not shared grammar.
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "and", "or", "to", "in", "on", "with",
    "is", "are", "was", "were", "be", "been", "this", "that", "these",
    "those", "as", "by", "at", "it", "its", "into", "from", "will", "no",
    "not", "new", "system", "field", "fields", "record", "change", "changes",
    "one", "all", "any", "now", "each",
})

# The mechanical vocabulary of a diff bullet itself (see _describe_change()) --
# excluded so a field can't get "grounding" credit merely for restating the
# diff line's own scaffolding, which DIFF FORMAT IS NOT CONTENT already bans
# as content in its own right.
_SCHEMA_NOISE_WORDS = frozenset({"added", "removed", "changed", "spec", "diff"})

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")


def _content_tokens(text: str) -> set[str]:
    """Lowercase alphanumeric/hyphenated tokens with stopwords and diff-
    scaffolding words stripped -- a cheap, non-linguistic filter, just
    enough to separate 'shares real topical vocabulary' from 'shares
    nothing at all'. Not intended as a precision measure."""
    words = _WORD_RE.findall(text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS and w not in _SCHEMA_NOISE_WORDS}


# One shared content word is a deliberately low bar. This guard exists to
# catch zero-overlap fabrication (a decision about something the diff never
# mentions at all), not to police paraphrase quality -- a field that shares
# one real noun with the diff but invents unsupported detail around it will
# still pass. Tightening this threshold is the natural next iteration if
# that narrower failure mode shows up in practice; starting loose keeps
# false positives (a correct, well-paraphrased field getting flagged) rare
# while this guard is unproven against real runs.
_MIN_GROUNDING_OVERLAP = 1

# Prefixes already applied by an earlier guard in run_scribe -- a field
# carrying one of these has already been flagged for human review, so this
# guard skips it rather than stacking a second, redundant prefix on top.
_ALREADY_FLAGGED_PREFIXES = (
    "POSSIBLE EXAMPLE COPY",
    "POSSIBLE DIFF-SYNTAX LEAK",
    "POSSIBLE PLACEHOLDER LEAK",
    "POSSIBLE DIFF BULLET ECHO",
    "NON-STRING OUTPUT",
    "LIST-COERCED",
)

# SCRIBE_SYSTEM_PROMPT's own stated cap for decision/consequences ("ONE
# sentence, no more than 25 words"). _SENTENCE_WORD_LIMIT_MULTIPLE is a
# generous buffer above that -- this isn't trying to strictly enforce the
# prompt's exact word count (that's the model's job, and a correct sentence
# running a little long shouldn't get flagged), it's a backstop against a
# field that is obviously not "one sentence" by any reading at all.
_MAX_SENTENCE_WORDS = 25
_SENTENCE_WORD_LIMIT_MULTIPLE = 3

# A field whose length is this large a fraction of the raw diff_summary's
# own length is far beyond "a short paraphrase" (FORMATTING RULES) no
# matter what it says -- it means most of the diff's own text rode along
# into the field rather than being condensed. Skipped on trivially short
# diffs (_DIFF_DUMP_MIN_DIFF_CHARS) since even a fully-grounded field can
# legitimately be a large fraction of a short diff's length there.
_DIFF_DUMP_LENGTH_RATIO = 0.4
_DIFF_DUMP_MIN_DIFF_CHARS = 200


def _detect_diff_dump(parsed: dict, diff_summary: str) -> list[str]:
    """Flags a field that is either far longer than SCRIBE_SYSTEM_PROMPT's
    own stated 'ONE sentence, <=25 words' rule (decision/consequences
    only), or whose length is a large fraction of the raw diff_summary's
    own length (all three fields) -- both signals that the model
    reproduced large chunks of the diff wholesale instead of synthesizing
    a short paraphrase.

    Confirmed workflow 34876593-... (19 Aug 2026): 'decision' and
    'consequences' both came back as near-total copies of diff content
    (the model's own core_features list, largely verbatim). That
    particular run was caught anyway, but only because the copied text
    happened to contain literal brackets that tripped _detect_bracket_
    leak() -- an accident of what the diff itself looked like, not a
    targeted check. If the diff's core_features hadn't contained brackets,
    the same failure would have passed every existing guard clean,
    including _detect_ungrounded_content() (which would see heavy shared
    vocabulary, because the field *is* the diff). This guard targets a
    diff-sized field directly, on length and word count alone, so
    detection doesn't depend on an incidental character collision.

    Two independent signals rather than one, since either alone can
    misfire: a field could be long without copying the diff (verbose but
    original text -- word-count check would still catch it if truly
    excessive), or share a high fraction of a short diff's length without
    actually being a dump (little content to work with either way -- the
    diff-size floor guards against this). Not yet confirmed against a real
    run where it's the only guard that fires -- workflow 34876593-... was
    already caught by _detect_bracket_leak() before this check would run
    against it.
    """
    flagged = []
    diff_len = len(diff_summary.strip())

    for field in ("decision", "consequences"):
        value = parsed.get(field)
        if not isinstance(value, str) or value.startswith(_ALREADY_FLAGGED_PREFIXES):
            continue
        too_long_for_one_sentence = len(value.split()) > _MAX_SENTENCE_WORDS * _SENTENCE_WORD_LIMIT_MULTIPLE
        diff_sized = diff_len >= _DIFF_DUMP_MIN_DIFF_CHARS and len(value) > diff_len * _DIFF_DUMP_LENGTH_RATIO
        if too_long_for_one_sentence or diff_sized:
            flagged.append(field)

    value = parsed.get("diff_summary")
    if (
        isinstance(value, str)
        and not value.startswith(_ALREADY_FLAGGED_PREFIXES)
        and diff_len >= _DIFF_DUMP_MIN_DIFF_CHARS
        and len(value) > diff_len * _DIFF_DUMP_LENGTH_RATIO
    ):
        flagged.append("diff_summary")

    return flagged


# Threshold for "this field is basically one diff bullet, reworded only
# slightly (or not at all)". Same mechanism and comparison function as
# _detect_example_copying() (SequenceMatcher ratio on normalized text),
# just pointed at the real diff instead of the fixed worked-example
# strings -- kept slightly below the example-copy threshold (0.90) since
# a diff bullet is dynamic per-run content, not a fixed string to match
# exactly, and the confirmed failure case (below) was a 100% match anyway,
# so there's room for a small tolerance margin without weakening the
# guard against the failure it exists for.
_DIFF_ECHO_SIMILARITY_THRESHOLD = 0.85

# summarize_diff() (incremental path) prefixes each bullet with "- ";
# summarize_creation_diff() (creation path) does not. Stripped before
# comparison so the same guard covers both diff shapes without the
# leading marker itself dragging the ratio down on the incremental path.
_BULLET_PREFIX_RE = re.compile(r"^-\s*")


def _detect_diff_bullet_echo(parsed: dict, diff_summary: str) -> list[str]:
    """Flags decision/consequences/diff_summary when it's a near-verbatim
    copy of a SINGLE bullet from the real diff -- as opposed to
    _detect_diff_dump() (near-total copy of the WHOLE diff, by length/word
    count) or _detect_ungrounded_content() (near-ZERO overlap with the
    diff at all). This is the gap between those two: a short field that
    echoes one bullet almost word-for-word is neither long enough to trip
    the diff-dump length thresholds nor low-overlap enough to trip the
    ungrounded-content check -- it sails through both clean.

    Confirmed live (workflow a9b0d6df-..., 21 Aug 2026, cloud_rag.json
    creation diff, after the key_user_flows exclusion fix landed): with
    the copy-tempting key_user_flows bullet removed from the prompt
    entirely, the model reached for the next-easiest target instead --
    the shortest, simplest bullet in the diff:
        diff bullet:   "Added project_overview: purpose, target_users,
                         deployment_environment"
        decision:      "Added project_overview: purpose, target_users,
                         deployment_environment"          (identical)
        diff_summary:  "Added project_overview: purpose, target_users,
                         deployment_environment."          (near-identical)
    Neither field is long enough or diff-sized enough to trip
    _detect_diff_dump() (diff_summary's own overall length is large across
    all 11 hunks, but this one field is short), and both fields obviously
    share plenty of real vocabulary with the diff, so
    _detect_ungrounded_content() has nothing to flag either -- the
    opposite problem from what that guard targets. No existing guard
    covers this pattern.

    'consequences' is intentionally still checked here (unlike
    _detect_ungrounded_content(), which skips it) -- a field that's
    supposed to state a downstream implication has no legitimate reason
    to be a verbatim echo of what changed, whereas Example 3's
    correct-answer 'consequences' text (see _detect_ungrounded_content's
    docstring) is a generalization, not a diff echo, so it won't
    false-positive here either.
    """
    lines = [
        _BULLET_PREFIX_RE.sub("", line).strip()
        for line in diff_summary.splitlines()
        if line.strip()
    ]
    if not lines or diff_summary.strip() == "No field-level changes detected.":
        # Nothing to echo against, or this IS the legitimate zero-diff
        # case -- already covered by _detect_example_copying()'s own
        # zero-diff branch, not this guard's concern.
        return []

    normalized_lines = [_normalize_for_comparison(line) for line in lines]

    flagged = []
    for field in ("decision", "consequences", "diff_summary"):
        value = parsed.get(field)
        if not isinstance(value, str) or value.startswith(_ALREADY_FLAGGED_PREFIXES):
            continue
        normalized_value = _normalize_for_comparison(value)
        for normalized_line in normalized_lines:
            ratio = SequenceMatcher(None, normalized_value, normalized_line).ratio()
            if ratio >= _DIFF_ECHO_SIMILARITY_THRESHOLD:
                flagged.append(field)
                break

    return flagged


def _detect_ungrounded_content(parsed: dict, diff_summary: str) -> list[str]:
    """Flags a 'decision' field that shares ~no vocabulary with the real
    diff_summary it is supposed to describe.

    Scoped to 'decision' only, not 'consequences' -- every confirmed
    fabrication case in KICKOFF_CHECKLIST.md's §8.1 is on 'decision'
    specifically, and 'consequences' legitimately generalizes on a creation
    diff in a way that would false-positive here: Example 3's own correct
    answer ("All subsequent changes will be diffed against this baseline
    spec version.") shares zero content vocabulary with its diff bullets by
    design, since it's a statement about the mechanical fact of establishing
    a baseline, not about the specific entities added. Checking 'decision'
    only avoids penalizing that legitimate pattern.

    The three guards above -- _detect_example_copying, _detect_example_domain_leak,
    _detect_diff_syntax_leak -- are all *negative* checks: each looks for one
    specific known-bad signature (verbatim/near-verbatim example copy, a
    fictional-domain proper noun, a leaked DeepDiff token). None of them can
    catch a genuinely novel fabrication that matches none of those
    signatures -- which is exactly the original P0 finding (13 Aug, workflow
    7cb63d6b-...): 'decision' described tenant-scoped retrieval isolation,
    invented in the model's own words with no basis in spec/Researcher/
    Architect output, on a zero-diff run. That text doesn't copy a worked
    example, doesn't contain a glacier/Iridium token, and doesn't leak
    DeepDiff syntax -- it would sail through all three existing guards clean.

    This is a coarse positive check instead: does the field share *any* real
    content vocabulary with the diff bullets it claims to describe? A
    correctly grounded paraphrase of "Added a satellite uplink integration
    point" will always reuse some of its nouns ("satellite", "uplink",
    "integration") even fully reworded, because there is nothing else for a
    grounded sentence to be about. Near-zero overlap is a strong signal the
    content was invented rather than synthesized from the given diff.

    Deliberately loose: threshold of 1 shared word, stopwords/schema noise
    excluded, fields already flagged by an earlier guard skipped, and a
    genuine zero-diff run (diff_summary == "No field-level changes
    detected.") exempted entirely since there is nothing to ground against
    by design -- see SCRIBE_SYSTEM_PROMPT's CRITICAL NO-DIFF RULE. This is a
    coverage backstop for zero-overlap fabrication, not a paraphrase-fidelity
    check -- a field that fabricates unsupported detail around one real
    shared noun will still pass. Not yet confirmed against a real run; watch
    for false positives on legitimately terse or abstract but correct
    decisions before tightening the threshold.
    """
    if diff_summary.strip() == "No field-level changes detected.":
        return []

    diff_tokens = _content_tokens(diff_summary)
    if not diff_tokens:
        return []

    value = parsed.get("decision")
    if not isinstance(value, str) or value.startswith(_ALREADY_FLAGGED_PREFIXES):
        return []
    field_tokens = _content_tokens(value)
    if not field_tokens:
        return []
    if len(field_tokens & diff_tokens) < _MIN_GROUNDING_OVERLAP:
        return ["decision"]
    return []


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

    # §8.1e root-cause candidate (16 Aug 2026): select the system prompt variant
    # based on whether this run's diff is genuinely empty, so the CRITICAL
    # NO-DIFF RULE block and Example 2's literal boilerplate -- a confirmed
    # copy target on real-diff runs -- are only in context when they actually
    # apply. See _build_scribe_system_prompt()'s docstring and the note above
    # _SCRIBE_PROMPT_HEADER for the full reasoning. Same string check the
    # existing guards already use, so this stays one source of truth for
    # "is this run's diff empty."
    is_zero_diff = diff_summary.strip() == "No field-level changes detected."
    system_prompt = _build_scribe_system_prompt(zero_diff=is_zero_diff)
    print(
        f"INFO: Scribe system prompt variant selected: "
        f"{'zero-diff (includes NO-DIFF rule + Example 2)' if is_zero_diff else 'real-diff (Examples 1+3 only, no zero-diff boilerplate in context)'}"
    )

    payload = {
        "model": LFM_MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
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
            # Diagnostic only, added after workflow 2db3d647-... produced a
            # malformed-JSON salvage with no way to tell, after the fact,
            # whether the break was an unescaped quote inside a field value,
            # a duplicated field, or something else -- neither this branch
            # nor the truncation one above has ever logged the raw text that
            # caused it, so every malformed-JSON incident so far has had to
            # be diagnosed by guessing at the shape instead of reading it.
            # Capped, not truncated at the error position, since the JSON
            # decode error's char offset is a hint, not a guarantee the fault
            # is exactly there.
            print(f"DEBUG: Scribe raw output (malformed_json, first 2000 chars):\n{raw[:2000]}")
            parsed = _salvage_truncated_scribe_output(raw, cause="malformed_json")
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

    domain_leaked_fields = _detect_example_domain_leak(parsed)
    if domain_leaked_fields:
        print(
            f"WARNING: Scribe output for {domain_leaked_fields} contains a "
            f"proper noun unique to the worked examples' fictional domain "
            f"(e.g. 'glacier', 'Iridium') that can never legitimately appear "
            f"in a real spec -- the model appears to have reused the "
            f"example's template and entities alongside real spec content, "
            f"a form of example-copying that evaded the fuzzy full-sentence "
            f"similarity check above (confirmed on workflow 3303ce31-...: "
            f"'decision' kept 'glacier sensor network' verbatim while every "
            f"other noun was swapped for real spec entities). Flagging "
            f"inline rather than retrying, same reasoning as the other "
            f"example-copying guard."
        )
        for field in domain_leaked_fields:
            if not parsed[field].startswith("POSSIBLE EXAMPLE COPY") and not parsed[field].startswith("POSSIBLE DIFF-SYNTAX LEAK"):
                parsed[field] = f"POSSIBLE EXAMPLE COPY (fictional-domain term) -- FLAG FOR HUMAN REVIEW: {parsed[field]}"
        salvage_reason = salvage_reason or "example_domain_leaked"

    leaked_fields = _detect_diff_syntax_leak(parsed)
    if leaked_fields:
        print(
            f"WARNING: Scribe output for {leaked_fields} contains DeepDiff's "
            f"internal category syntax (e.g. 'dictionary_item_added') "
            f"verbatim instead of plain-English paraphrase, despite the "
            f"system prompt explicitly prohibiting this. Flagging inline "
            f"rather than retrying, since temperature=0.05 makes an "
            f"identical retry likely to reproduce the same leak (confirmed "
            f"on workflow 7570a555-...)."
        )
        for field in leaked_fields:
            parsed[field] = f"POSSIBLE DIFF-SYNTAX LEAK -- FLAG FOR HUMAN REVIEW: {parsed[field]}"
        salvage_reason = salvage_reason or "diff_syntax_leaked"

    bracket_leaked_fields = _detect_bracket_leak(parsed)
    if bracket_leaked_fields:
        print(
            f"WARNING: Scribe output for {bracket_leaked_fields} still "
            f"contains a literal '[' or ']' -- the model echoed a "
            f"worked-example placeholder slot from SCRIBE_SYSTEM_PROMPT "
            f"instead of replacing it with real content from the diff. "
            f"Flagging inline rather than retrying, same reasoning as the "
            f"other guards at temperature=0.05."
        )
        for field in bracket_leaked_fields:
            parsed[field] = f"POSSIBLE PLACEHOLDER LEAK -- FLAG FOR HUMAN REVIEW: {parsed[field]}"
        salvage_reason = salvage_reason or "placeholder_leaked"

    ungrounded_fields = _detect_ungrounded_content(parsed, diff_summary)
    if ungrounded_fields:
        print(
            f"WARNING: Scribe output for {ungrounded_fields} shares no "
            f"real content vocabulary with the actual diff it is supposed "
            f"to describe -- unlike the example-copy/domain-leak/diff-"
            f"syntax guards above, this doesn't match a known-bad pattern, "
            f"it simply fails to reference anything in the real diff at "
            f"all, which is what novel (non-copied) fabrication looks "
            f"like. Flagging inline rather than retrying, same reasoning "
            f"as the other guards at temperature=0.05."
        )
        for field in ungrounded_fields:
            parsed[field] = f"POSSIBLE FABRICATION (no shared content with diff) -- FLAG FOR HUMAN REVIEW: {parsed[field]}"
        salvage_reason = salvage_reason or "ungrounded_content"

    diff_dump_fields = _detect_diff_dump(parsed, diff_summary)
    if diff_dump_fields:
        print(
            f"WARNING: Scribe output for {diff_dump_fields} is far longer "
            f"than SCRIBE_SYSTEM_PROMPT's own 'ONE sentence, <=25 words' "
            f"rule allows, and/or a large fraction of the raw diff's own "
            f"length -- the model appears to have reproduced large chunks "
            f"of the diff wholesale instead of synthesizing a short "
            f"paraphrase (confirmed workflow 34876593-...: 'decision' and "
            f"'consequences' both came back as near-total copies of the "
            f"diff's core_features content). Flagging inline rather than "
            f"retrying, same reasoning as the other guards at "
            f"temperature=0.05."
        )
        for field in diff_dump_fields:
            parsed[field] = f"POSSIBLE DIFF DUMP (field reproduces large fraction of diff, not a short paraphrase) -- FLAG FOR HUMAN REVIEW: {parsed[field]}"
        salvage_reason = salvage_reason or "diff_dump"

    echo_fields = _detect_diff_bullet_echo(parsed, diff_summary)
    if echo_fields:
        print(
            f"WARNING: Scribe output for {echo_fields} is a near-verbatim "
            f"echo of a single real diff bullet -- short enough to dodge "
            f"_detect_diff_dump()'s length/word-count thresholds and "
            f"sharing too much vocabulary with the diff to trip "
            f"_detect_ungrounded_content(), but still not the paraphrase "
            f"FORMATTING RULES requires (confirmed on workflow "
            f"a9b0d6df-...: 'decision' and 'diff_summary' both echoed the "
            f"project_overview bullet almost character-for-character). "
            f"Flagging inline rather than retrying, same reasoning as the "
            f"other guards at temperature=0.05."
        )
        for field in echo_fields:
            parsed[field] = f"POSSIBLE DIFF BULLET ECHO (near-verbatim copy of one diff bullet, not a paraphrase) -- FLAG FOR HUMAN REVIEW: {parsed[field]}"
        salvage_reason = salvage_reason or "diff_bullet_echo"

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