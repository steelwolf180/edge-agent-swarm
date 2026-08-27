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

_DUPLICATE_FLAG_PREFIX = "POSSIBLE DUPLICATE -- FLAG FOR HUMAN REVIEW: "


def _already_flagged(text: str) -> bool:
    """True if `text` already carries one of this module's flag prefixes.
    Checked before every mutation below so a re-run over already-flagged
    output (or two guards matching the same item) never double-wraps it."""
    return isinstance(text, str) and text.startswith(("POSSIBLE ",))


def _flag_duplicate_list_items(parsed: dict) -> list[str]:
    """Detect AND flag exact-duplicate entries within spofs,
    missing_integrations, and gap descriptions -- mutates `parsed` in
    place, mirroring agents/scribe.py's inline-flag pattern, instead of
    only logging. The pre-fix version (print-only, no mutation) is the
    confirmed root cause of workflow a9b0d6df-...'s spofs repetition
    reaching the persisted review doc unflagged despite the guard
    correctly detecting it in the log -- see KICKOFF_CHECKLIST.md's
    review of that run. Every occurrence of a repeated entry is flagged,
    not just the first duplicate found (the pre-fix version also stopped
    at one warning per field via `break`), so a human reviewing the
    output sees exactly which entries are suspect, not just that
    *something* in the field repeats.
    """
    flagged_fields = []

    for field in ("spofs", "missing_integrations"):
        items = parsed.get(field) or []
        counts: dict[str, int] = {}
        for item in items:
            if isinstance(item, str) and item.strip():
                key = item.strip().lower()
                counts[key] = counts.get(key, 0) + 1
        if not any(c > 1 for c in counts.values()):
            continue
        new_items = []
        for item in items:
            key = item.strip().lower() if isinstance(item, str) else None
            if key and counts.get(key, 0) > 1 and not _already_flagged(item):
                new_items.append(f"{_DUPLICATE_FLAG_PREFIX}{item}")
            else:
                new_items.append(item)
        parsed[field] = new_items
        flagged_fields.append(field)

    gaps = parsed.get("gaps") or []
    gap_keys = [
        g.get("description", "").strip().lower() if isinstance(g, dict) else ""
        for g in gaps
    ]
    gap_counts: dict[str, int] = {}
    for k in gap_keys:
        if k:
            gap_counts[k] = gap_counts.get(k, 0) + 1
    if any(c > 1 for c in gap_counts.values()):
        for g, k in zip(gaps, gap_keys):
            if k and gap_counts.get(k, 0) > 1 and isinstance(g, dict):
                desc = g.get("description", "")
                if not _already_flagged(desc):
                    g["description"] = f"{_DUPLICATE_FLAG_PREFIX}{desc}"
        flagged_fields.append("gaps")

    return flagged_fields

_CROSS_FIELD_FLAG_PREFIX = "POSSIBLE CROSS-FIELD DUPLICATE -- FLAG FOR HUMAN REVIEW: "


def _flag_cross_field_duplication(parsed: dict) -> list[str]:
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
    underlying findings as full sentences. _flag_duplicate_list_items()
    correctly found no *within-list* repetition on that run and stayed
    silent -- this guard covers the cross-field axis it doesn't.

    As of 21 Aug 2026 (critic-guard-wiring fix), this mutates `parsed` in
    place instead of only returning warning strings -- the pre-fix version
    detected correctly but never touched the output, which is the same
    class of bug (detection without action) confirmed separately on
    _flag_duplicate_list_items() via workflow a9b0d6df-.... Every guard in
    this file now follows the same inline-flag contract Scribe's guards
    already use.

    Still NOT caught: a gap that paraphrases a spof/missing_integrations
    entry in different words rather than restating it verbatim -- reusing
    Scribe's SequenceMatcher-against-fixed-example pattern doesn't
    transfer here, since both sides of the comparison are dynamic,
    length-mismatched model output (a one-line entry vs. a full gap
    sentence), not a known string to match against. Logged as a known
    limitation (same class as Scribe's Rubber Stamp Risk, spec §7), not
    pursued further -- non-blocking for §9.
    """
    flagged_fields = []
    spofs = parsed.get("spofs") or []
    missing = parsed.get("missing_integrations") or []

    spofs_keys = {s.strip().lower() for s in spofs if isinstance(s, str)}
    missing_keys = {m.strip().lower() for m in missing if isinstance(m, str)}
    exact_overlap = spofs_keys & missing_keys

    if exact_overlap:
        parsed["spofs"] = [
            f"{_CROSS_FIELD_FLAG_PREFIX}{item}"
            if isinstance(item, str) and item.strip().lower() in exact_overlap and not _already_flagged(item)
            else item
            for item in spofs
        ]
        parsed["missing_integrations"] = [
            f"{_CROSS_FIELD_FLAG_PREFIX}{item}"
            if isinstance(item, str) and item.strip().lower() in exact_overlap and not _already_flagged(item)
            else item
            for item in missing
        ]
        flagged_fields.extend(["spofs", "missing_integrations"])

    # gaps is prose, so check whether a gap description contains a spof or
    # missing_integrations entry as a substring rather than requiring an
    # exact match -- catches the reworded-into-a-sentence case.
    gaps = parsed.get("gaps") or []
    gaps_changed = False
    spofs_to_flag: set[str] = set()
    missing_to_flag: set[str] = set()
    for g in gaps:
        if not isinstance(g, dict):
            continue
        desc = g.get("description", "")
        low = desc.strip().lower()
        if _already_flagged(desc):
            continue
        matched_spofs = {s for s in spofs_keys if s and s in low}
        matched_missing = {m for m in missing_keys if m and m in low}
        if matched_spofs or matched_missing:
            g["description"] = f"{_CROSS_FIELD_FLAG_PREFIX}{desc}"
            gaps_changed = True
            spofs_to_flag |= matched_spofs
            missing_to_flag |= matched_missing
    if gaps_changed:
        flagged_fields.append("gaps")

    # Guard-asymmetry fix (21 Aug 2026): a gap matching a spofs/missing_
    # integrations entry via substring means that entry is duplicated too,
    # not just the gap. Pre-fix, only the gap side got flagged -- a
    # reviewer scanning missing_integrations or spofs in isolation would
    # see the duplicated entry with no marker at all. Confirmed live on
    # workflow 873af0ae-...: missing_integrations[1] ("No explicit
    # handling of potential API rate limits...") is byte-identical to
    # gaps[1]'s description, which WAS flagged CROSS-FIELD DUPLICATE --
    # missing_integrations[1] itself was not.
    if spofs_to_flag:
        parsed["spofs"] = [
            f"{_CROSS_FIELD_FLAG_PREFIX}{item}"
            if isinstance(item, str) and item.strip().lower() in spofs_to_flag and not _already_flagged(item)
            else item
            for item in parsed.get("spofs") or []
        ]
        if "spofs" not in flagged_fields:
            flagged_fields.append("spofs")
    if missing_to_flag:
        parsed["missing_integrations"] = [
            f"{_CROSS_FIELD_FLAG_PREFIX}{item}"
            if isinstance(item, str) and item.strip().lower() in missing_to_flag and not _already_flagged(item)
            else item
            for item in parsed.get("missing_integrations") or []
        ]
        if "missing_integrations" not in flagged_fields:
            flagged_fields.append("missing_integrations")

    return flagged_fields


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


_EXAMPLE_COPY_FLAG_PREFIX = "POSSIBLE EXAMPLE COPY -- FLAG FOR HUMAN REVIEW: "
_EXAMPLE_DOMAIN_LEAK_FLAG_PREFIX = "POSSIBLE EXAMPLE COPY (fictional-domain term) -- FLAG FOR HUMAN REVIEW: "


def _flag_list_and_gap_items(parsed: dict, matches, prefix: str) -> list[str]:
    """Shared mutation helper for the two guards below: given a `matches(text)
    -> bool` predicate, rebuild spofs/missing_integrations (immutable str
    entries -- must reassign the list) and mutate gaps' description dicts
    in place (mutable, no reassignment needed), prefixing every entry that
    matches and isn't already flagged. Returns the field names touched."""
    flagged_fields = []

    for field in ("spofs", "missing_integrations"):
        items = parsed.get(field) or []
        if not items:
            continue
        new_items = []
        changed = False
        for item in items:
            if isinstance(item, str) and not _already_flagged(item) and matches(item):
                new_items.append(f"{prefix}{item}")
                changed = True
            else:
                new_items.append(item)
        parsed[field] = new_items
        if changed:
            flagged_fields.append(field)

    gaps = parsed.get("gaps") or []
    gaps_changed = False
    for g in gaps:
        if not isinstance(g, dict):
            continue
        desc = g.get("description", "")
        if isinstance(desc, str) and not _already_flagged(desc) and matches(desc):
            g["description"] = f"{prefix}{desc}"
            gaps_changed = True
    if gaps_changed:
        flagged_fields.append("gaps")

    return flagged_fields


def _flag_example_copying(parsed: dict) -> list[str]:
    """Flags AND mutates any gaps/spofs/missing_integrations list entry
    that closely matches CRITIC_SYSTEM_PROMPT's worked example text --
    exact OR near-verbatim (>=90% similar after normalizing
    case/whitespace/articles). Same pattern as agents/scribe.py's
    _detect_example_copying(), applied to list fields instead of single
    strings. As of the critic-guard-wiring fix, mutates in place via
    _flag_list_and_gap_items() rather than only returning field names for
    a caller to log -- the pre-fix version detected but never flagged the
    persisted output, same gap confirmed on the duplicate-item guard."""
    def matches(text: str) -> bool:
        normalized = _normalize_for_comparison(text)
        return any(
            SequenceMatcher(None, normalized, _normalize_for_comparison(example)).ratio()
            >= _COPY_SIMILARITY_THRESHOLD
            for example in _EXAMPLE_OUTPUT_STRINGS
        )

    return _flag_list_and_gap_items(parsed, matches, _EXAMPLE_COPY_FLAG_PREFIX)


def _flag_example_domain_leak(parsed: dict) -> list[str]:
    """Flags AND mutates any gaps/spofs/missing_integrations entry
    containing a proper noun unique to the worked example's fictional
    domain (OrderService, NotificationService, SMS provider) -- catches a
    domain leak that's been reworded enough to fall below the fuzzy-match
    threshold above. Same pattern as agents/scribe.py's
    _detect_example_domain_leak(). Mutates in place as of the
    critic-guard-wiring fix -- see _flag_example_copying()'s note."""
    def matches(text: str) -> bool:
        lowered = text.lower()
        return any(token in lowered for token in _EXAMPLE_DOMAIN_TOKENS)

    return _flag_list_and_gap_items(parsed, matches, _EXAMPLE_DOMAIN_LEAK_FLAG_PREFIX)

_NEAR_DUP_GAP_FLAG_PREFIX = "POSSIBLE NEAR-DUPLICATE GAP -- FLAG FOR HUMAN REVIEW: "
_GAP_NEAR_DUP_THRESHOLD = 0.75  # gap sentences are same-shape/length, unlike
# the cross-field case P2 ruled fuzzy-matching out for -- start here, tune
# empirically against confirmed runs rather than guessing higher.

def _flag_near_duplicate_gaps(parsed: dict) -> list[str]:
    """Within-gaps near-duplicate check -- catches reworded restatements
    of the same finding that _flag_duplicate_list_items()'s exact-match
    check can't see. Confirmed live: workflow a5847e95-..., three gap
    descriptions all describing the same missing Vector Store <-> LLM API
    integration, worded three different ways, none byte-identical.
    """
    gaps = parsed.get("gaps") or []
    descs = [
        (i, g.get("description", "")) for i, g in enumerate(gaps)
        if isinstance(g, dict) and isinstance(g.get("description"), str)
    ]
    flagged_idx = set()
    for a in range(len(descs)):
        i, text_a = descs[a]
        if _already_flagged(text_a):
            continue
        for b in range(a + 1, len(descs)):
            j, text_b = descs[b]
            if _already_flagged(text_b):
                continue
            ratio = SequenceMatcher(
                None, text_a.strip().lower(), text_b.strip().lower()
            ).ratio()
            if ratio >= _GAP_NEAR_DUP_THRESHOLD:
                flagged_idx.add(i)
                flagged_idx.add(j)

    for i in flagged_idx:
        desc = gaps[i].get("description", "")
        if not _already_flagged(desc):
            gaps[i]["description"] = f"{_NEAR_DUP_GAP_FLAG_PREFIX}{desc}"

    return ["gaps"] if flagged_idx else []

# --- Diagram relationship echo (P5, 21 Aug 2026) -----------------------
#
# Confirmed live on workflow 873af0ae-...: all three `spofs` entries were
# scrambled restatements of a single Rel() edge from the Architect's own
# diagram (Rel(customer, rag_system, "Sends support queries via Support
# Chat Widget")) -- e.g. "Support Chat Widget receives support queries
# from RAG System" -- not genuine redundancy/resilience analysis. None of
# the guards above caught it: not a within-field duplicate (three distinct
# strings), not a cross-field duplicate (no overlap with gaps/missing_
# integrations), and not a copy of CRITIC_SYSTEM_PROMPT's fixed worked
# example (the leaked content is from the diagram, not the prompt).
#
# Unlike the P2 gap-vs-spof paraphrase case (ruled out in
# _flag_cross_field_duplication's docstring -- no fixed anchor, both sides
# are dynamic model output), this failure DOES have a fixed anchor
# available: architect_output.context_diagram is already part of this
# run's input, known before Critic ever runs. So the same
# "known-string-to-compare-against" pattern Scribe uses for its
# diff-syntax/example-domain guards applies here too, just anchored on the
# diagram instead of a hardcoded example.
#
# SequenceMatcher (used for _flag_example_copying, above) is the wrong
# tool for this specific failure shape: hand-checking workflow
# 873af0ae-...'s entries against their source Rel label scores ~0.7 on
# character-level similarity even for the closest-matching entry, well
# under the 0.90 threshold used elsewhere in this file -- word-order
# scrambling and reworded verbs ("sends" -> "receives") defeat a
# character-alignment comparison. Bag-of-words token overlap (order-
# independent, coarse trailing-'s' stem) is used instead.
#
# Second confirming run (workflow 9f651906-..., 21 Aug 2026, different
# spec-derived diagram from 873af0ae-...): 15 of 18 spofs entries and 1 of
# 4 missing_integrations correctly flagged, including entries that
# reassigned the wrong component to a Rel's action (e.g. "Zendesk pulls
# documents from Confluence API" -- the real edge is
# Rel(ingestion_service, confluence, "Pulls documents from Confluence
# API")) -- confirms the token-overlap approach generalizes past exact
# component attribution, not just exact wording. BUT 3 entries slipped
# through unflagged on that run:
#   "Zendesk manages tickets and help center"
#   "Confluence is source of truth for documentation"
#   "Git repository is source of truth for versioned technical docs"
# These restate a System_Ext(...) declaration's own description string
# (its third argument), not a Rel() edge -- e.g. System_Ext(confluence,
# "Confluence", "Source of truth for product documentation"). The
# original anchor set (_extract_rel_texts()) only parsed Rel() lines, so
# it had nothing to compare these against. _extract_declaration_texts()
# below closes that gap by pulling System/System_Ext/Person declaration
# names + descriptions into the same anchor set Rel() labels already feed.

_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
    "via", "from", "by", "at", "is", "are", "this", "that", "as", "it",
}

_REL_RE = re.compile(r'Rel\w*\(\s*(\w+)\s*,\s*(\w+)\s*,\s*"([^"]*)"')

# Matches System(id, "Name", "Description"), System_Ext(id, "Name",
# "Description"), and Person(id, "Name", "Description") -- the three C4
# declaration constructs the spec's MVP scope (L1 System Context) uses.
# System_Boundary and other constructs are out of scope until L2 (v2).
_DECL_RE = re.compile(r'(?:System(?:_Ext)?|Person)\(\s*\w+\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\)')

# Vocabulary that indicates genuine resilience/redundancy analysis rather
# than a restated diagram edge. An entry that scores high on Rel-label
# token overlap AND contains none of these is very likely paraphrasing a
# Rel() line rather than analyzing it -- a real SPOF/gap finding about the
# same component pair would typically reach for language like this
# instead of (or alongside) the Rel's own connector vocabulary. Kept
# deliberately short and unambiguous (same zero-false-positive-risk
# design as _EXAMPLE_DOMAIN_TOKENS) rather than exhaustive.
_RESILIENCE_VOCAB = (
    "single", "redundan", "failover", "outage", "unavailable", "bottleneck",
    "backup", "downtime", "point of failure", "no fallback",
)

# Confirmed empirically against workflow 873af0ae-...'s output (21 Aug
# 2026): all three spofs entries score 0.50-1.0 against their source Rel
# text; that run's genuine gap descriptions ("No explicit separation of
# concerns...", "No explicit documentation of the data flow...") score
# 0.20-0.29 -- a wide, comfortable margin. 0.50 (not the initially-tried
# 0.55, which missed the weakest of the three confirmed echo entries) is
# the threshold, still well above the highest legitimate-content score
# observed so far.
_REL_OVERLAP_THRESHOLD = 0.50
_DIAGRAM_ECHO_FLAG_PREFIX = "POSSIBLE DIAGRAM RELATIONSHIP ECHO -- FLAG FOR HUMAN REVIEW: "


def _significant_tokens(text: str) -> set[str]:
    """Lowercased words of length >=4, stopwords dropped, trailing 's'
    stripped for a cheap singular/plural match (system/systems). Coarse
    on purpose -- the failure mode being caught is word salvage from a
    Rel() line into scrambled/reordered prose, not a clean verbatim copy,
    so phrase order is deliberately ignored."""
    words = re.findall(r"[a-z]+", text.lower())
    out = set()
    for w in words:
        if len(w) < 4 or w in _STOPWORDS:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def _extract_rel_texts(context_diagram: str, components: list[Component]) -> list[str]:
    """One combined text blob per Rel() edge in the Mermaid source: the
    source/target component display names (falling back to the raw id if
    a component isn't in the components list, e.g. an undeclared id) plus
    the Rel's own label text."""
    id_to_name = {c.id: c.name for c in components}
    texts = []
    for src, dst, label in _REL_RE.findall(context_diagram or ""):
        src_name = id_to_name.get(src, src)
        dst_name = id_to_name.get(dst, dst)
        texts.append(f"{src_name} {dst_name} {label}")
    return texts


def _extract_declaration_texts(context_diagram: str) -> list[str]:
    """One combined text blob per System/System_Ext/Person declaration in
    the Mermaid source: the declared display name plus its description
    string (the C4 declaration's third argument). Confirmed necessary on
    workflow 9f651906-...: _extract_rel_texts() alone missed 3 of 18
    spofs entries because the model echoed a declaration's own
    description text (e.g. System_Ext(confluence, "Confluence", "Source
    of truth for product documentation")) rather than a Rel() edge label
    -- same failure class, different diagram construct, so it needs its
    own extractor rather than a tweak to the Rel-only one."""
    return [f"{name} {description}" for name, description in _DECL_RE.findall(context_diagram or "")]


def _flag_diagram_relationship_echo(
    parsed: dict,
    context_diagram: str,
    components: list[Component],
) -> list[str]:
    """Flags AND mutates spofs/missing_integrations/gaps entries that
    substantially reuse the words of one of the diagram's own Rel() edges,
    and (spofs only -- see below) System/System_Ext/Person declaration
    descriptions. See the module-level note above this function for the
    confirming runs and why token overlap (not SequenceMatcher) is used
    here.

    Two anchor sets, deliberately different scope:

    - Rel() edges are checked against ALL THREE fields (gaps, spofs,
      missing_integrations), same as the original 873af0ae-... fix --
      confirmed no false positives on real run data.
    - Declaration descriptions are checked against SPOFS ONLY. Tried
      checking all three fields first and confirmed (against
      9f651906-...'s real missing_integrations list) that this produces
      false positives there: "Zendesk API (help center articles + ticket
      context)" scores 0.67 against System_Ext(zendesk, "Zendesk",
      "Manages customer support tickets and help center")'s tokens, but
      it's a genuinely correct missing_integrations entry, not an echo --
      a correct description of what an integration does will always
      legitimately share vocabulary with the diagram's own correct
      description of that same component. spofs doesn't have this
      problem: legitimate SPOF content is about redundancy/resilience
      risk, not restating what a component is/does, so high overlap with
      a plain declaration description is a reliable echo signal there in
      a way it isn't for missing_integrations or gaps.

    Requires the entry to contain NONE of _RESILIENCE_VOCAB in both
    checks, so a genuine SPOF finding that happens to mention the same
    component (or reuse some of its description's words) isn't misflagged
    just for sharing vocabulary -- SPOFs are often legitimately *about* a
    component that also appears in Rel edges or has its own declaration.
    """
    flagged_fields: list[str] = []

    rel_texts = _extract_rel_texts(context_diagram, components)
    rel_token_sets = [s for s in (_significant_tokens(t) for t in rel_texts) if len(s) >= 3]

    def matches_rel(text: str) -> bool:
        low = text.lower()
        if any(v in low for v in _RESILIENCE_VOCAB):
            return False
        entry_tokens = _significant_tokens(text)
        if len(entry_tokens) < 3:
            return False
        return any(
            len(entry_tokens & tokens) / len(entry_tokens) >= _REL_OVERLAP_THRESHOLD
            for tokens in rel_token_sets
        )

    if rel_token_sets:
        flagged_fields.extend(_flag_list_and_gap_items(parsed, matches_rel, _DIAGRAM_ECHO_FLAG_PREFIX))

    decl_texts = _extract_declaration_texts(context_diagram)
    decl_token_sets = [s for s in (_significant_tokens(t) for t in decl_texts) if len(s) >= 3]

    def matches_decl(text: str) -> bool:
        low = text.lower()
        if any(v in low for v in _RESILIENCE_VOCAB):
            return False
        entry_tokens = _significant_tokens(text)
        if len(entry_tokens) < 3:
            return False
        return any(
            len(entry_tokens & tokens) / len(entry_tokens) >= _REL_OVERLAP_THRESHOLD
            for tokens in decl_token_sets
        )

    if decl_token_sets:
        spofs = parsed.get("spofs") or []
        new_spofs = []
        changed = False
        for item in spofs:
            if isinstance(item, str) and not _already_flagged(item) and matches_decl(item):
                new_spofs.append(f"{_DIAGRAM_ECHO_FLAG_PREFIX}{item}")
                changed = True
            else:
                new_spofs.append(item)
        if changed:
            parsed["spofs"] = new_spofs
            if "spofs" not in flagged_fields:
                flagged_fields.append("spofs")

    return flagged_fields


# Vocabulary that indicates a genuine gap statement -- an entry that
# actually asserts something is missing/undocumented/unhandled, rather
# than describing an integration that exists. Deliberately short and
# unambiguous (same zero-false-positive-risk design as _RESILIENCE_VOCAB
# and _EXAMPLE_DOMAIN_TOKENS): a correct missing_integrations entry will
# almost always reach for language like this to say *what's absent*, and
# a pure restatement of an existing Rel()/spec integration essentially
# never will, since restating what something does doesn't require saying
# it's missing.
#
# This exists as a separate mechanism from _flag_diagram_relationship_echo
# on purpose. Extending that guard's overlap-with-source approach to check
# missing_integrations against spec/diff text was considered and rejected:
# a genuinely correct missing_integrations entry has to name the same
# integration the spec/diff names, so it will always share heavy token
# overlap with that source text -- this is the identical false-positive
# class already confirmed on 9f651906-... for the declaration-anchor
# check (a correct "Zendesk API..." entry scored 0.67 against Zendesk's
# own declaration text). Overlap-with-source can't distinguish "restates
# the source" from "correctly analyzes the same named thing." Checking
# for the *absence* of gap-indicating vocabulary sidesteps that problem
# entirely, since it doesn't compare against source text at all.
_GAP_LANGUAGE_VOCAB = (
    "missing", "not shown", "not represented", "not depicted", "not modeled",
    "no explicit", "lacks", "lacking", "undocumented", "absent", "unclear",
    "not defined", "not specified", "gap",
)
_MISSING_INTEGRATION_RESTATEMENT_FLAG_PREFIX = (
    "POSSIBLE RESTATEMENT, NOT A GAP -- FLAG FOR HUMAN REVIEW: "
)


def _flag_missing_integrations_without_gap_language(parsed: dict) -> list[str]:
    """Flags AND mutates missing_integrations entries that contain none of
    _GAP_LANGUAGE_VOCAB -- i.e. entries that describe an integration
    (what it is / what it does) without ever asserting that it's absent,
    undocumented, or unhandled, which is what this field is actually
    supposed to surface per CRITIC_SYSTEM_PROMPT.

    Confirmed motivating case (workflow ae941ea4-..., 26 Aug 2026): all
    three missing_integrations entries this run were pure restatements
    ("RAG System interacts with X to do Y"), not gap statements. Two of
    three happened to also score high enough on Rel()-token-overlap to
    be caught by _flag_diagram_relationship_echo(); the third ("Git-
    hosted docs repo... to pull versioned documentation") echoed the
    Scribe diff's own integration_points bullet instead of a Rel() edge,
    so it fell outside that guard's anchor set and went through
    unflagged even though it has the identical underlying problem: no
    gap asserted, not genuine missing_integrations analysis. This guard
    targets the shared root symptom (no gap language) directly rather
    than patching diagram_relationship_echo's anchor set to also cover
    diff text, which would reintroduce the declaration-anchor false-
    positive class documented above.

    missing_integrations only, not spofs or gaps: spofs already has
    _RESILIENCE_VOCAB doing the equivalent job for its own domain, and
    gaps' correct shape (per CRITIC_SYSTEM_PROMPT) doesn't require this
    specific vocabulary the way a "what's missing" field does.
    """
    flagged_fields: list[str] = []
    items = parsed.get("missing_integrations") or []
    if not items:
        return flagged_fields

    new_items = []
    changed = False
    for item in items:
        if isinstance(item, str) and not _already_flagged(item):
            low = item.lower()
            if not any(v in low for v in _GAP_LANGUAGE_VOCAB):
                new_items.append(f"{_MISSING_INTEGRATION_RESTATEMENT_FLAG_PREFIX}{item}")
                changed = True
                continue
        new_items.append(item)
    if changed:
        parsed["missing_integrations"] = new_items
        flagged_fields.append("missing_integrations")

    return flagged_fields


# --- Fabricated component reference (26 Aug 2026) ----------------------
#
# Confirmed live on workflow 3ae9e752-...: gaps/spofs/missing_integrations
# all referenced a "notification_service" component that does not exist
# anywhere -- not in the spec, not in architect_output.components, not in
# the diagram. Distinct failure class from every guard above: those all
# check for *copying* or *duplication* of real content; this checks for
# outright invention of an entity that was never real to begin with. The
# existing cross-field duplication guard correctly flagged the spofs/
# missing_integrations copies of it as duplicates of the gaps entry, but
# that's incidental -- it flagged them for being duplicates of each other,
# not for referencing something fabricated. The gaps entry itself, being
# the only occurrence, went through both duplicate guards unflagged.
#
# Flag-only, same as every other guard's first landing here (P0, P2, P5
# all started detection-only before any behavior-level fix was
# attempted) -- not bundling a "fix the model" attempt with this guard's
# first landing.
_ENTITY_STOPWORDS = {"the", "and", "or", "for", "with", "via", "of", "to", "a", "an"}
_SNAKE_CASE_ENTITY_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_TITLE_CASE_ENTITY_RE = re.compile(
    r"\b(?:[A-Z][a-zA-Z0-9]*\s){0,3}"
    r"(?:Service|API|Store|Handler|Console|Widget|Sidebar|System|Database|DB)\b"
)
_FABRICATED_COMPONENT_ENTITY_OVERLAP_THRESHOLD = 0.5
_FABRICATED_COMPONENT_FLAG_PREFIX_TEMPLATE = (
    "POSSIBLE FABRICATED COMPONENT ({entities} not present in diagram) "
    "-- FLAG FOR HUMAN REVIEW: "
)


def _normalize_entity(text: str) -> str:
    """snake_case or Title Case -> space-joined lowercase tokens, stopwords
    dropped. 'Notification Service' and 'notification_service' both
    normalize to 'notification service', matching real component id/name
    regardless of which casing style the model reaches for."""
    text = text.replace("_", " ")
    tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return " ".join(t for t in tokens if t not in _ENTITY_STOPWORDS)


def _extract_entity_mentions(text: str) -> set[str]:
    mentions = set()
    for m in _SNAKE_CASE_ENTITY_RE.findall(text):
        mentions.add(_normalize_entity(m))
    for m in _TITLE_CASE_ENTITY_RE.findall(text):
        norm = _normalize_entity(m)
        if norm:
            mentions.add(norm)
    return {m for m in mentions if m}


def _flag_fabricated_component_references(parsed: dict, components: list[Component]) -> list[str]:
    """Flags AND mutates gaps/spofs/missing_integrations entries that
    reference a component-like entity absent from architect_output.
    components -- confirmed motivating case above (workflow 3ae9e752-...,
    a hallucinated 'notification_service'/'Notification Service').

    Bag-of-words token overlap against real component id/name, same
    0.50-threshold pattern as _flag_diagram_relationship_echo's Rel-anchor
    check, so 'notification service' still correctly matches a real id
    like 'vector_store' if the model reworded it, while not matching a
    genuinely absent entity at all.
    """
    real_entities = set()
    for c in components:
        real_entities.add(_normalize_entity(c.id))
        real_entities.add(_normalize_entity(c.name))
    real_entities.discard("")
    real_token_sets = [set(r.split()) for r in real_entities if r]

    def matches(text: str) -> bool:
        mentioned = _extract_entity_mentions(text)
        fabricated = set()
        for m in mentioned:
            if m in real_entities:
                continue
            m_tokens = set(m.split())
            if not any(
                len(m_tokens & r_tokens) / max(len(m_tokens), 1) >= _FABRICATED_COMPONENT_ENTITY_OVERLAP_THRESHOLD
                for r_tokens in real_token_sets
            ):
                fabricated.add(m)
        # Stash on the closure via a mutable default arg trick is fragile;
        # instead re-derive fabricated set at flag-time below.
        return bool(fabricated)

    # _flag_list_and_gap_items only takes a boolean matches() predicate and
    # a single fixed prefix, but this guard's prefix needs to name which
    # entity was fabricated per-entry -- so flag manually here rather than
    # reusing that helper, unlike every other guard in this file.
    flagged_fields: list[str] = []

    def fabricated_entities_for(text: str) -> set[str]:
        mentioned = _extract_entity_mentions(text)
        fabricated = set()
        for m in mentioned:
            if m in real_entities:
                continue
            m_tokens = set(m.split())
            if not any(
                len(m_tokens & r_tokens) / max(len(m_tokens), 1) >= _FABRICATED_COMPONENT_ENTITY_OVERLAP_THRESHOLD
                for r_tokens in real_token_sets
            ):
                fabricated.add(m)
        return fabricated

    for field in ("spofs", "missing_integrations"):
        items = parsed.get(field) or []
        if not items:
            continue
        new_items = []
        changed = False
        for item in items:
            if isinstance(item, str) and not _already_flagged(item):
                fabricated = fabricated_entities_for(item)
                if fabricated:
                    prefix = _FABRICATED_COMPONENT_FLAG_PREFIX_TEMPLATE.format(
                        entities=", ".join(sorted(fabricated))
                    )
                    new_items.append(f"{prefix}{item}")
                    changed = True
                    continue
            new_items.append(item)
        parsed[field] = new_items
        if changed:
            flagged_fields.append(field)

    gaps = parsed.get("gaps") or []
    gaps_changed = False
    for g in gaps:
        if not isinstance(g, dict):
            continue
        desc = g.get("description", "")
        if isinstance(desc, str) and not _already_flagged(desc):
            fabricated = fabricated_entities_for(desc)
            if fabricated:
                prefix = _FABRICATED_COMPONENT_FLAG_PREFIX_TEMPLATE.format(
                    entities=", ".join(sorted(fabricated))
                )
                g["description"] = f"{prefix}{desc}"
                gaps_changed = True
    if gaps_changed:
        flagged_fields.append("gaps")

    return flagged_fields


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


def _find_matching_bracket(text: str, open_idx: int, open_ch: str, close_ch: str) -> int | None:
    """Given text[open_idx] == open_ch, return the index of its matching
    close_ch, skipping over bracket/brace characters inside double-quoted
    strings (respecting backslash escapes). Returns None if unbalanced.

    Generic over the bracket pair so it covers both '[...]' (spofs /
    missing_integrations, flat lists) and '{...}' (individual gap objects)
    with one implementation -- unlike agents/scribe.py's version of this
    helper, which is '[...]'-only since every field Scribe salvages is a
    flat string, never a list of objects."""
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
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


def _extract_field_bracket(stripped: str, field: str) -> str | None:
    """Return the '[...]' substring for `field` (e.g. the value of
    '"spofs": [...]'), or None if the field key isn't present in the raw
    output at all -- distinct from the field being present but empty
    ('"spofs": []', which returns '[]')."""
    match = re.search(rf'"{field}"\s*:\s*\[', stripped)
    if not match:
        return None
    open_idx = match.end() - 1
    close_idx = _find_matching_bracket(stripped, open_idx, "[", "]")
    return stripped[open_idx:close_idx + 1] if close_idx is not None else stripped[open_idx:]


def _salvage_string_list_field(stripped: str, field: str) -> list[str]:
    """Best-effort recovery for a list-of-strings field (spofs,
    missing_integrations) when the overall JSON document is malformed.

    Same fallback strategy as agents/scribe.py's _salvage_list_valued_
    field(): json.loads() the field's own bracket span first (the rest of
    the document can be broken while this one field's array is still
    well-formed on its own); if that also fails, regex-extract whatever
    quoted substrings are present rather than giving up entirely. Unlike
    Scribe, this is Critic's PRIMARY recovery path for these two fields,
    not a fallback for a wrong-shaped scalar -- spofs/missing_integrations
    are supposed to be lists already.

    Returns [] (not a placeholder string) if nothing usable is found. An
    empty list is a legitimate CriticOutput value here -- the system
    prompt explicitly allows "no SPOFs found" -- so standing in an empty
    list for unrecoverable content isn't dishonest the way Scribe's
    MISSING-placeholder problem would be for a required non-empty string
    field.
    """
    bracket_text = _extract_field_bracket(stripped, field)
    if bracket_text is None:
        return []
    try:
        parsed_list = json.loads(bracket_text)
        if isinstance(parsed_list, list):
            return [str(item).strip() for item in parsed_list if str(item).strip()]
    except (json.JSONDecodeError, ValueError):
        pass
    return [s.strip() for s in re.findall(r'"((?:[^"\\]|\\.)*)"', bracket_text) if s.strip()]


_GAP_SEVERITIES = {"low", "medium", "high"}
_GAP_SALVAGE_PREFIX = "SALVAGED (recovered from malformed JSON) -- FLAG FOR HUMAN REVIEW: "


def _coerce_gap(obj: dict) -> dict:
    """Normalize one recovered gap object to CriticOutput's Gap shape.
    severity defaults to 'medium' if missing or not one of the three
    allowed values; related_component defaults to None if missing.
    Prefixed the same way every other guard/salvage path in this file
    flags recovered content, so a human reviewer can tell salvaged output
    apart from a clean generation at a glance."""
    severity = obj.get("severity")
    if severity not in _GAP_SEVERITIES:
        severity = "medium"
    return {
        "description": f"{_GAP_SALVAGE_PREFIX}{obj['description']}",
        "severity": severity,
        "related_component": obj.get("related_component"),
    }


def _salvage_gap_objects(stripped: str) -> tuple[list[dict], int]:
    """Best-effort recovery for 'gaps' -- a list of {description, severity,
    related_component} objects, not a flat string list, so neither
    agents/scribe.py's nor Critic's own _salvage_string_list_field()
    applies directly.

    Confirmed failure shape (tests/smoke/test_critic.py, 21 Aug 2026): a
    missing delimiter BETWEEN two gap objects broke json.loads() on the
    whole array even though every individual object was well-formed on
    its own -- 'Expecting , delimiter' at the boundary between two
    otherwise-valid dicts.

    Strategy: locate the 'gaps' array span, try json.loads() on the whole
    thing first (cheap, and correct whenever the array itself is fine).
    If that fails, scan the span for balanced '{...}' object spans
    (respecting quoted strings, reusing the same bracket-matching logic
    as the list fields) regardless of whether a comma correctly separates
    them, and json.loads() each span independently. A span that parses
    and has a non-empty 'description' is kept; a span that still doesn't
    parse is DROPPED, not fabricated -- there is no honest way to invent
    gap content the model never successfully produced. CriticOutput's
    gaps list tolerates ending up shorter than the model likely intended,
    or even empty, without that being a false claim -- "fewer gaps
    recovered" is not the same statement as "no gaps exist". The caller
    logs how many objects were dropped so this isn't silent.

    Returns (gaps, dropped_count).
    """
    bracket_text = _extract_field_bracket(stripped, "gaps")
    if bracket_text is None:
        return [], 0

    try:
        parsed_list = json.loads(bracket_text)
        if isinstance(parsed_list, list):
            gaps = [
                _coerce_gap(item)
                for item in parsed_list
                if isinstance(item, dict) and item.get("description")
            ]
            return gaps, 0
    except (json.JSONDecodeError, ValueError):
        pass

    gaps: list[dict] = []
    dropped = 0
    i = 0
    n = len(bracket_text)
    while i < n:
        if bracket_text[i] == "{":
            close_idx = _find_matching_bracket(bracket_text, i, "{", "}")
            if close_idx is None:
                # Unterminated final object -- nothing more to scan after this.
                dropped += 1
                break
            candidate = bracket_text[i:close_idx + 1]
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict) and obj.get("description"):
                    gaps.append(_coerce_gap(obj))
                else:
                    dropped += 1
            except (json.JSONDecodeError, ValueError):
                dropped += 1
            i = close_idx + 1
        else:
            i += 1

    return gaps, dropped


def _salvage_malformed_critic_output(raw: str) -> dict:
    """Best-effort recovery when LFM produces structurally malformed JSON
    on a *complete* Critic generation (finish_reason != 'length' --
    truncation is ruled out by run_critic() before this is ever called).

    Same motivating failure class as agents/scribe.py's
    _salvage_truncated_scribe_output(cause='malformed_json'), ported to
    Critic's different schema shape rather than reused directly: Scribe's
    fields are all flat strings, Critic's 'gaps' is a list of nested
    objects, so the recovery strategy has to differ even though the
    underlying philosophy (extract what parsed, flag what's uncertain,
    never fabricate) is the same.

    Before this existed, any malformed-JSON generation raised a bare
    ValueError and cost a full DBOS retry (or, worse, exhausted all 3
    attempts and failed the whole step) even when most of the content --
    frequently all of spofs and missing_integrations, and most of gaps --
    was perfectly recoverable. This salvages each field independently so
    a single delimiter typo doesn't discard an otherwise-good critique.
    """
    stripped = strip_code_fence(raw)
    spofs = _salvage_string_list_field(stripped, "spofs")
    missing_integrations = _salvage_string_list_field(stripped, "missing_integrations")
    gaps, dropped = _salvage_gap_objects(stripped)

    if dropped:
        print(
            f"WARNING: Critic malformed-JSON salvage recovered {len(gaps)} "
            f"gap object(s) but could not parse {dropped} more -- those "
            f"entries are dropped, not fabricated, since there's no honest "
            f"way to reconstruct content the model didn't successfully "
            f"produce. Recovered gaps are flagged inline (SALVAGED)."
        )

    return {"gaps": gaps, "spofs": spofs, "missing_integrations": missing_integrations}


# --- Bounded regeneration for duplicate-class flags (26 Aug 2026) ------
#
# Different mechanism from every guard above: not a new detector, a single
# bounded regeneration attempt for entries the duplicate-class guards
# (_flag_duplicate_list_items, _flag_near_duplicate_gaps,
# _flag_cross_field_duplication) already flagged. Explicitly NOT the same
# as the temp=0.05 blind-retry pattern already ruled out elsewhere in this
# pipeline (Scribe's decision/consequences copying) -- those retries
# reused the identical prompt and reliably reproduced the identical
# failure. This instead feeds the flagged text + every other entry
# currently in the output back in as corrective context ("here's what you
# wrote, here's what's already covered, write something distinct"), which
# is a materially different prompt, not a resample of the same one.
#
# Scoped to the three duplicate-class guards only -- NOT example-copy,
# domain-leak, diagram-echo, restatement-without-gap-language, or
# fabricated-component flags. Those are different failure shapes
# (copying, restating, inventing) where "write something different" isn't
# obviously the right corrective instruction the way it is for genuine
# duplication; extending regeneration to them is a separate, unvalidated
# decision, not bundled into this landing.
#
# Capped at one attempt per flagged entry, no loop. On any failure (still
# duplicate against ANY other entry in the output, model declines via
# NO_DISTINCT_FINDING, empty/degenerate response, or the call itself
# fails) it falls back to the existing FLAG-FOR-REVIEW text untouched --
# never leaves an entry in a worse or unverified state than before.

_DUPLICATE_CLASS_PREFIXES = (
    _DUPLICATE_FLAG_PREFIX,
    _NEAR_DUP_GAP_FLAG_PREFIX,
    _CROSS_FIELD_FLAG_PREFIX,
)
_REGEN_STILL_DUPLICATE_THRESHOLD = 0.75  # same threshold as _GAP_NEAR_DUP_THRESHOLD


def _strip_flag_prefix(text: str) -> str:
    """Removes this module's flag prefix + 'FLAG FOR HUMAN REVIEW: '
    marker, returning the model's original text underneath. Used before
    feeding a flagged entry into a regeneration prompt -- the model should
    see its own original wording, not review-facing prefix text."""
    if "FLAG FOR HUMAN REVIEW: " in text:
        return text.split("FLAG FOR HUMAN REVIEW: ", 1)[1]
    return text


def _all_entry_texts(parsed: dict) -> list[str]:
    """Every current gaps/spofs/missing_integrations text, flag prefixes
    and all -- used as the 'already covered' set so a regenerated entry
    can't reintroduce a duplicate of a DIFFERENT already-flagged entry."""
    texts = []
    for field in ("spofs", "missing_integrations"):
        texts.extend(t for t in (parsed.get(field) or []) if isinstance(t, str))
    for g in parsed.get("gaps") or []:
        if isinstance(g, dict) and isinstance(g.get("description"), str):
            texts.append(g["description"])
    return texts


def _still_duplicate(candidate: str, other_texts: list[str]) -> bool:
    """Re-checked against every other entry currently in the output, not
    just the same field -- a regenerated spofs entry that now duplicates a
    gaps entry is still a failure, just of the cross-field kind instead of
    the within-field kind it started as."""
    if not candidate or len(candidate.strip()) < 10:
        return True
    low = candidate.strip().lower()
    for other in other_texts:
        other_clean = _strip_flag_prefix(other).strip().lower()
        if not other_clean:
            continue
        if low == other_clean or low in other_clean or other_clean in low:
            return True
        if SequenceMatcher(None, low, other_clean).ratio() >= _REGEN_STILL_DUPLICATE_THRESHOLD:
            return True
    return False


def _build_regeneration_prompt(
    field_name: str, flagged_text: str, other_texts: list[str], architect_output: ArchitectOutput
) -> str:
    siblings_rendered = "\n".join(f"- {_strip_flag_prefix(t)}" for t in other_texts) or "(none)"
    return (
        f"You previously wrote this {field_name} entry, but it duplicates or "
        f"overlaps with other analysis already recorded for this diagram:\n\n"
        f'  "{flagged_text}"\n\n'
        f"Entries already covered (do NOT repeat any of these, in wording or meaning):\n"
        f"{siblings_rendered}\n\n"
        f"C4 diagram context:\n{architect_output.context_diagram}\n\n"
        f"Write ONE new, distinct {field_name} entry that identifies a genuinely "
        f"different issue not already covered above. If you cannot identify a "
        f"distinct issue, respond with exactly: NO_DISTINCT_FINDING\n\n"
        f"Respond with only the entry text, one sentence, no preamble, no numbering."
    )


async def _regenerate_duplicate_entry(
    field_name: str,
    flagged_text: str,
    other_texts: list[str],
    architect_output: ArchitectOutput,
) -> str | None:
    """One bounded regeneration attempt. Returns new text on success, or
    None if the caller should keep the existing FLAG-FOR-REVIEW text as-is.

    temperature=0.15, not the 0.2-0.3 range considered initially -- the
    corrective prompt content (flagged text + full sibling list) is
    expected to do most of the work toward distinctness, and this model
    degrades unpredictably at higher temperatures elsewhere in this
    pipeline. Bump only if a real run shows 0.15 still reproducing
    near-duplicates -- one variable at a time, not estimated upfront.
    """
    original_text = _strip_flag_prefix(flagged_text)
    prompt = _build_regeneration_prompt(field_name, original_text, other_texts, architect_output)
    payload = {
        "model": LFM_MODEL_NAME,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are the Critic agent in an architecture review pipeline, "
                    "asked to replace one flagged duplicate finding with a distinct one."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.15,
        "max_tokens": 200,
    }
    try:
        async with httpx.AsyncClient(timeout=CRITIC_HTTP_TIMEOUT_S) as client:
            response = await client.post(LLAMA_SERVER_URL, json=payload)
        if response.status_code >= 400:
            return None
        choice = response.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            return None
        candidate = choice["message"]["content"].strip().strip('"')
    except Exception:
        return None

    if not candidate or candidate.upper().startswith("NO_DISTINCT_FINDING"):
        return None
    if _still_duplicate(candidate, other_texts):
        return None
    return candidate


async def _attempt_regeneration_pass(parsed: dict, architect_output: ArchitectOutput) -> list[str]:
    """Runs one bounded regeneration attempt for every entry currently
    flagged by the duplicate-class guards. Successful regenerations
    replace the flagged text with the clean new text (no flag prefix --
    it's a verified-distinct finding, not something still needing
    review). Failures leave the existing FLAG-FOR-REVIEW text untouched.
    Returns "field[index]" identifiers for every entry regenerated, for
    logging.
    """
    regenerated: list[str] = []

    for field in ("spofs", "missing_integrations"):
        items = parsed.get(field) or []
        for i, item in enumerate(items):
            if not isinstance(item, str) or not item.startswith(_DUPLICATE_CLASS_PREFIXES):
                continue
            other_texts = [t for t in _all_entry_texts(parsed) if t != item]
            new_text = await _regenerate_duplicate_entry(field, item, other_texts, architect_output)
            if new_text is not None:
                items[i] = new_text
                regenerated.append(f"{field}[{i}]")

    gaps = parsed.get("gaps") or []
    for i, g in enumerate(gaps):
        if not isinstance(g, dict):
            continue
        desc = g.get("description", "")
        if not isinstance(desc, str) or not desc.startswith(_DUPLICATE_CLASS_PREFIXES):
            continue
        other_texts = [t for t in _all_entry_texts(parsed) if t != desc]
        new_text = await _regenerate_duplicate_entry("gaps", desc, other_texts, architect_output)
        if new_text is not None:
            g["description"] = new_text
            regenerated.append(f"gaps[{i}]")

    return regenerated


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
        salvage_reason = None
    except json.JSONDecodeError:
        # Same treatment as agents/scribe.py's malformed_json branch:
        # print the raw output once for debugging (capped, not truncated
        # at the JSON error's char offset, since that offset is a hint,
        # not a guarantee the fault is exactly there), then attempt
        # field-by-field recovery instead of raising and burning a retry
        # that would likely reproduce the same delimiter/structural error
        # deterministically at low temperature.
        print(f"DEBUG: Critic raw output (malformed_json, first 2000 chars):\n{raw[:2000]}")
        parsed = _salvage_malformed_critic_output(raw)
        salvage_reason = "malformed_json"

    # Guard-wiring fix (21 Aug 2026): every guard below now mutates `parsed`
    # in place -- prefixing the specific flagged entries -- BEFORE
    # CriticOutput.model_validate() runs, and it's the validated *mutated*
    # dict that gets returned. The pre-fix version validated `parsed` into
    # `validated` first, then ran these same detectors afterward as
    # print-only warnings that never touched `validated` -- so a correctly
    # detected problem (confirmed live: workflow a9b0d6df-...'s duplicate
    # spofs) could still reach the persisted review doc completely
    # unflagged. Order mirrors scribe.py's guard sequence: most specific /
    # highest-confidence pattern first (exact duplicates), most general
    # last (fictional-domain leak), and every guard checks
    # _already_flagged() before writing so two guards matching the same
    # entry never double-wrap it.
    dup_fields = _flag_duplicate_list_items(parsed)
    if dup_fields:
        print(
            f"WARNING: Critic output for {dup_fields} contained exact-"
            f"duplicate entries -- flagged inline (POSSIBLE DUPLICATE) "
            f"rather than retried, same reasoning as Scribe's guards at "
            f"low temperature: a retry is likely to reproduce the same "
            f"degenerate output."
        )
    
    near_dup_gap_fields = _flag_near_duplicate_gaps(parsed)
    if near_dup_gap_fields:
        print(
            f"WARNING: Critic output for {near_dup_gap_fields} contains "
            f"reworded near-duplicate gap descriptions -- same underlying "
            f"finding stated multiple times, not distinct issues. Flagged "
            f"inline (POSSIBLE NEAR-DUPLICATE GAP)."
        )
    
    cross_field_fields = _flag_cross_field_duplication(parsed)
    if cross_field_fields:
        print(
            f"WARNING: Critic output for {cross_field_fields} overlaps "
            f"across spofs/missing_integrations/gaps -- these are meant to "
            f"be distinct analysis categories, not the same finding "
            f"restated. Flagged inline (POSSIBLE CROSS-FIELD DUPLICATE)."
        )

    copied_fields = _flag_example_copying(parsed)
    if copied_fields:
        print(
            f"WARNING: Critic output for {copied_fields} closely matches "
            f"(exact or near-verbatim) a worked-example string from the "
            f"system prompt. Flagged inline (POSSIBLE EXAMPLE COPY)."
        )

    leaked_fields = _flag_example_domain_leak(parsed)
    if leaked_fields:
        print(
            f"WARNING: Critic output for {leaked_fields} contains a "
            f"worked-example domain token (OrderService/NotificationService/"
            f"SMS provider) unrelated to this spec. Flagged inline "
            f"(POSSIBLE EXAMPLE COPY (fictional-domain term))."
        )

    echo_fields = _flag_diagram_relationship_echo(
        parsed, architect_output.context_diagram, architect_output.components
    )
    if echo_fields:
        print(
            f"WARNING: Critic output for {echo_fields} substantially "
            f"reuses the words of a Rel() edge from the Architect's own "
            f"diagram instead of producing genuine SPOF/gap analysis. "
            f"Flagged inline (POSSIBLE DIAGRAM RELATIONSHIP ECHO)."
        )

    restatement_fields = _flag_missing_integrations_without_gap_language(parsed)
    if restatement_fields:
        print(
            f"WARNING: Critic output for {restatement_fields} describes an "
            f"existing integration without ever asserting it's missing/"
            f"undocumented/unhandled -- restatement, not gap analysis. "
            f"Flagged inline (POSSIBLE RESTATEMENT, NOT A GAP)."
        )

    fabricated_fields = _flag_fabricated_component_references(parsed, architect_output.components)
    if fabricated_fields:
        print(
            f"WARNING: Critic output for {fabricated_fields} references a "
            f"component not present in architect_output.components -- "
            f"invented, not restated. Flagged inline "
            f"(POSSIBLE FABRICATED COMPONENT)."
        )

    if salvage_reason or dup_fields or near_dup_gap_fields or cross_field_fields or copied_fields or leaked_fields or echo_fields or restatement_fields or fabricated_fields:
        print(
            f"INFO: Critic output was salvaged (reason={salvage_reason}, "
            f"guard_flags={dup_fields or near_dup_gap_fields or cross_field_fields or copied_fields or leaked_fields or echo_fields or restatement_fields or fabricated_fields})."
        )

    # Bounded regeneration pass -- one attempt per duplicate-class flagged
    # entry (dup_fields / near_dup_gap_fields / cross_field_fields only,
    # not fabricated_fields or the copy/leak/echo/restatement guards; see
    # module note above _attempt_regeneration_pass). Runs after every
    # guard above so it sees the full, final flagged state of `parsed`.
    if dup_fields or near_dup_gap_fields or cross_field_fields:
        regenerated = await _attempt_regeneration_pass(parsed, architect_output)
        if regenerated:
            print(
                f"INFO: Critic regenerated {len(regenerated)} duplicate-"
                f"flagged entr{'y' if len(regenerated) == 1 else 'ies'} "
                f"after one bounded attempt each (temperature=0.15): "
                f"{regenerated}."
            )

    try:
        validated = CriticOutput.model_validate(parsed)
    except ValidationError as e:
        raise ValueError(f"Critic: LFM output failed CriticOutput validation: {e}") from e

    return validated