"""
agents/architect.py

Architect agent (Gemma 4 E4B QAT via llama-server, router mode).
No tools — pure text generation. Reads spec + Researcher's pricing
context off the blackboard, plus recent prior ADRs scanned from
artifacts/adr/v*/adr_*.md, and emits ArchitectOutput.
"""

from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from pydantic import ValidationError
from schemas.adr import ADRRecord
from schemas.architect import ArchitectOutput, DiagramProvenance
from dotenv import load_dotenv

import json
import re
import httpx
import os

load_dotenv()


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {key} (check your .env file)"
        )
    return value


LLAMA_ROOT_SERVER_URL = _require_env("LLAMA_SERVER_URL")
BASE_URL = f"{LLAMA_ROOT_SERVER_URL}/v1/chat/completions"

MODEL_NAME = _require_env("GEMMA_MODEL_NAME")
MAX_TOKENS = int(_require_env("ARCHITECT_TOKEN_BUDGET"))

ARTIFACTS_ROOT = Path("artifacts")
ADR_GLOB_PATTERN = "adr/v*/adr_*.md"
ADR_CONTEXT_LIMIT = 3
ADR_DECISION_TRUNCATE = 200

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)
_SECTION_RE = re.compile(r"##\s*(Context|Decision|Consequences)\s*\n(.*?)(?=\n##|\Z)", re.DOTALL | re.IGNORECASE)

ARCHITECT_HTTP_TIMEOUT_S = float(_require_env("ARCHITECT_HTTP_TIMEOUT_S"))

def _parse_frontmatter(block: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip("'\"") for v in value[1:-1].split(",") if v.strip()]
            fields[key] = items
        else:
            fields[key] = value.strip("'\"")
    return fields


def _parse_adr_markdown(path: Path) -> ADRRecord:
    text = path.read_text()
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError(f"{path} is missing a --- frontmatter block")
    frontmatter_block, body = match.groups()
    fields = _parse_frontmatter(frontmatter_block)

    sections = {m.group(1).lower(): m.group(2).strip() for m in _SECTION_RE.finditer(body)}
    for required in ("context", "decision", "consequences"):
        if required not in sections:
            raise ValueError(f"{path} is missing a '## {required.title()}' section")

    return ADRRecord(
        adr_id=fields["adr_id"],
        spec_version=int(fields["spec_version"]),
        status=fields.get("status", "accepted"),
        supersedes=fields.get("supersedes", []),
        context=sections["context"],
        decision=sections["decision"],
        consequences=sections["consequences"],
        diff_summary=fields.get("diff_summary", ""),
        affected_diagrams=fields.get("affected_diagrams", []),
        created=fields["created"],
    )


def _load_recent_adrs(artifacts_root: Path = ARTIFACTS_ROOT, limit: int = ADR_CONTEXT_LIMIT) -> list[ADRRecord]:
    if not artifacts_root.exists():
        return []

    records: list[ADRRecord] = []
    for path in sorted(artifacts_root.glob(ADR_GLOB_PATTERN)):
        try:
            records.append(_parse_adr_markdown(path))
        except (ValueError, ValidationError, KeyError) as e:
            print(f"WARNING: skipping malformed ADR file {path}: {e}")
            continue

    superseded_ids = {sid for r in records for sid in r.supersedes}
    live_records = [
        r for r in records
        if r.status == "accepted" and r.adr_id not in superseded_ids
    ]

    live_records.sort(key=lambda r: r.spec_version, reverse=True)
    return live_records[:limit]

SYSTEM_PROMPT = """You are the Architect agent in a C4 architecture pipeline.
Given a project spec, cloud pricing context, and a summary of prior
architecture decisions, produce a C4 System Context (L1) diagram as
Mermaid `C4Context` source, plus supporting docs and a structured
component list.

If PRIOR_DECISIONS lists settled decisions, do not silently contradict
them — the diagram should stay consistent with prior ADRs unless the
spec itself has changed in a way that requires revisiting one.

Respond with EXACTLY three sections, in this order, using these literal
markers on their own line. Do not add any text before ---DIAGRAM--- or
after the closing ---END---. Do not include any provenance, timestamps,
or metadata — those are handled outside the model.

The following is a WORKED FORMAT EXAMPLE ONLY. Do not reuse its entity
names, domain, or relationships — the actual diagram must be built
entirely from the SPEC provided by the user below. The example exists
only to show required structure and completeness.

---DIAGRAM---
C4Context
    title Library Book Reservation System Context
    Person(patron, "Library Patron", "Searches and reserves books")
    System(catalog, "Reservation System", "Manages holds and due dates")
    System_Ext(sms, "SMS Gateway", "Sends pickup notifications")
    Rel(patron, catalog, "Places holds")
    Rel(catalog, sms, "Sends notification on availability")
---DOCS---
This diagram shows the Library Reservation System's context. Patrons place holds through the system, which notifies them via SMS when a reserved book becomes available.
---COMPONENTS---
[{"id": "patron", "name": "Library Patron", "type": "person", "description": "Searches and reserves books", "technology": null, "redundant": false}, {"id": "catalog", "name": "Reservation System", "type": "internal_system", "description": "Manages holds and due dates", "technology": null, "redundant": false}, {"id": "sms", "name": "SMS Gateway", "type": "external_system", "description": "Sends pickup notifications", "technology": null, "redundant": false}]
---END---

Always produce all three sections for the actual spec given below — never
stop after ---DIAGRAM---. Each Person, System, System_Ext, Rel, and
boundary statement MUST be on its own line. Never place two statements
on the same line.

Before writing any Rel(...) line, the elements it connects must already
be declared on their own Person(...), System(...), or System_Ext(...)
line earlier in the diagram. After drafting the diagram, cross-check it
against your own COMPONENTS list: every id that appears in COMPONENTS
must also have a corresponding declaration line in the diagram, and vice
versa. On a diagram with many components, it is easy to introduce an
entity in one section and forget to declare it in another — check for
this specifically before finalizing your output.

REL ENDPOINT SYNTAX RULE: every Rel(...) call takes exactly two element
ids as its first two arguments, and each one MUST be a bare identifier —
letters, digits, and underscores only, no spaces, no quotes — that
exactly matches an id already declared on an earlier Person/System/
System_Ext line. Never put a quoted string in a Rel(...) endpoint
position, and never write a multi-word phrase there either. If you find
yourself wanting to reference something like "the sidebar" or "the admin
console" inside a Rel(...) call and it has no Person/System/System_Ext
declaration of its own, that is a signal you have invented an entity —
either declare it properly on its own line first, or drop the reference
entirely. Remember this is an L1 System Context diagram: internal UI
panels, dashboards, and sub-screens of a system you already declared
(e.g. an "admin console" that is part of a system you called
"support_chat") are L2 Container-level detail and MUST NOT appear as
separate Rel(...) endpoints at all — describe that interaction, if
relevant, as part of the existing System's relationship, not as a new
element.

WRONG (do not do this — invented, undeclared, malformed endpoints):
    Rel(agent, "Agent Sidebar", "Uses assisted-answer interface")
    Rel(Agent Sidebar, rag_system, "Submits queries to")
Both lines reference "Agent Sidebar", which was never declared with its
own Person/System/System_Ext line, once as a quoted string and once as
an unquoted multi-word phrase — neither is a valid Rel(...) endpoint.
CORRECT alternatives: either omit this sub-flow (it is L2-level, out of
scope for L1), or express it via the already-declared System, e.g.:
    Rel(agent, support_chat, "Uses assisted-answer sidebar within")

In the COMPONENTS list, the "type" field must be exactly one of these three
literal strings: "person", "internal_system", "external_system". Never use
"system", "actor", "external", or any other variant — these are the only
three valid values, and using anything else will fail validation.
"""


def _format_adr_context(adrs: list[ADRRecord]) -> str:
    if not adrs:
        return "No prior ADRs on file. This is the first recorded decision set."
    lines = ["Prior architecture decisions (most recent first):"]
    for adr in adrs:
        decision = adr.decision.strip()[:ADR_DECISION_TRUNCATE]
        lines.append(f"- [{adr.adr_id}, spec v{adr.spec_version}] {decision}")
    return "\n".join(lines)


def build_user_prompt(
    spec: dict[str, Any],
    pricing_context: dict[str, Any],
    prior_decisions_text: str,
) -> str:
    return (
        f"SPEC:\n{json.dumps(spec, indent=2)}\n\n"
        f"PRICING_CONTEXT:\n{json.dumps(pricing_context, indent=2)}\n\n"
        f"PRIOR_DECISIONS:\n{prior_decisions_text}\n\n"
        "Produce the C4 System Context (L1) diagram now."
    )


def _extract_section(raw: str, start_marker: str, end_marker: str) -> str:
    pattern = re.escape(start_marker) + r"(.*?)" + re.escape(end_marker)
    match = re.search(pattern, raw, re.DOTALL)
    if not match:
        raise ValueError(f"Could not find section between {start_marker!r} and {end_marker!r}")
    return match.group(1).strip()

_STATEMENT_START_RE = re.compile(
    r"(?<!\n)\s+(?=(Person|System|Rel|System_Boundary|Enterprise_Boundary|SystemDb|SystemQueue)\()"
)

def _normalize_diagram_linebreaks(diagram: str) -> str:
    return _STATEMENT_START_RE.sub("\n    ", diagram)

_DECLARED_ID_RE = re.compile(
    r"\b(?:Person|System|System_Ext|SystemDb|SystemDb_Ext|SystemQueue|SystemQueue_Ext|Container|Boundary|System_Boundary|Enterprise_Boundary)\w*\(\s*([A-Za-z0-9_]+)"
)
# Kept for _repair_undeclared_ids below, which only ever attempts repair on
# clean-but-undeclared ids -- malformed refs (quoted/multi-word) are handled
# separately by _validate_diagram_ids, since there's no COMPONENTS entry a
# quoted string or space-containing token could ever mechanically match.
_REL_RE = re.compile(r"\bRel\w*\(\s*([A-Za-z0-9_]+)\s*,\s*([A-Za-z0-9_]+)")

# Matches a full Rel(...) call's parenthesized argument list, so it can be
# parsed properly rather than pattern-matched positionally. The old _REL_RE-
# based validation only matched Rel calls where both endpoints were already
# bare identifiers -- anything else (a quoted string used as an id, or an
# unquoted multi-word token) simply failed to match and was silently
# skipped, letting malformed endpoints reach mermaid.ink undetected.
# Confirmed 13 Aug 2026: Gemma emitted Rel(agent, "Agent Sidebar", ...) and
# Rel(Agent Sidebar, rag_system, ...) for an invented, never-declared
# sub-component -- both invisible to the old regex -- and mermaid.ink's
# renderer crashed on it ("Cannot read properties of undefined (reading 'x')")
# instead of surfacing a clear error at the source.
_REL_CALL_RE = re.compile(r"\bRel\w*\(([^)]*)\)")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _split_top_level_args(arg_str: str) -> list[str]:
    """Split a Rel(...) argument list on commas, respecting double-quoted
    labels (which may themselves contain commas). Simple quote-toggle
    scanner -- Mermaid C4 args don't use escaped quotes, so this doesn't
    need to handle backslash-escaping."""
    args: list[str] = []
    current: list[str] = []
    in_quotes = False
    for ch in arg_str:
        if ch == '"':
            in_quotes = not in_quotes
            current.append(ch)
        elif ch == "," and not in_quotes:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        args.append("".join(current).strip())
    return args


def _validate_diagram_ids(diagram: str) -> None:
    declared = set(_DECLARED_ID_RE.findall(diagram))
    undefined: set[str] = set()
    malformed: list[str] = []

    for call in _REL_CALL_RE.finditer(diagram):
        args = _split_top_level_args(call.group(1))
        if len(args) < 2:
            malformed.append(
                f"{call.group(0)!r}: expected at least 2 arguments (source, target), got {len(args)}"
            )
            continue
        src, dst = args[0], args[1]
        for token, role in (("source", src), ("target", dst)):
            if not _BARE_ID_RE.match(role):
                malformed.append(
                    f"{call.group(0)!r}: {token} {role!r} is not a bare identifier -- "
                    f"a Rel(...) endpoint must exactly match an id already declared on "
                    f"an earlier Person/System/System_Ext line, with no quotes and no "
                    f"spaces. A quoted string or multi-word phrase here is almost always "
                    f"an entity the model referenced but never declared."
                )
            elif role not in declared:
                undefined.add(role)

    # Malformed endpoints are reported first and separately from merely
    # undeclared ones -- they're a different failure mode (invalid syntax
    # vs. a missing declaration _repair_undeclared_ids might still fix) and
    # conflating them into one undifferentiated id list would obscure which
    # fix actually applies.
    if malformed:
        raise ValueError(
            "Architect: diagram has malformed Rel(...) endpoint(s), not caught by "
            "declaration repair because there's no valid id to repair:\n"
            + "\n".join(f"  - {m}" for m in malformed)
        )
    if undefined:
        raise ValueError(
            f"Architect: diagram has Rel(...) referencing undeclared element id(s): "
            f"{sorted(undefined)}. Declared ids: {sorted(declared)}"
        )

_COMPONENT_TYPE_TO_DIAGRAM_FN = {
    "person": "Person",
    "internal_system": "System",
    "external_system": "System_Ext",
}

def _synthesize_declaration(component: dict[str, Any]) -> str:
    fn = _COMPONENT_TYPE_TO_DIAGRAM_FN.get(component.get("type"), "System")
    name = component.get("name", component["id"])
    description = component.get("description", "")
    return f'{fn}({component["id"]}, "{name}", "{description}")'


def _repair_undeclared_ids(diagram: str, components: list[dict[str, Any]]) -> str:
    """Best-effort mechanical repair for the declare-before-reference bug
    (recurring as of 6 Aug 2026 — 2nd confirmed failure of the 22 Jul
    prompt-only fix, see KICKOFF_CHECKLIST.md §8). If a Rel(...) references
    an id missing from the diagram but present in the model's own
    COMPONENTS list, synthesize the declaration line from that component's
    own name/type/description instead of failing the step. Ids with no
    COMPONENTS match are left alone — _validate_diagram_ids() still raises
    for those, since there's nothing to repair from."""
    declared = set(_DECLARED_ID_RE.findall(diagram))
    rels = _REL_RE.findall(diagram)
    referenced = {src for src, dst in rels} | {dst for src, dst in rels}
    undeclared = referenced - declared

    components_by_id = {c["id"]: c for c in components if "id" in c}
    repaired_lines = [
        _synthesize_declaration(components_by_id[cid])
        for cid in sorted(undeclared)
        if cid in components_by_id
    ]
    if not repaired_lines:
        return diagram

    lines = diagram.splitlines()
    insert_at = 1 if lines and lines[0].strip().lower().startswith("c4context") else 0
    if len(lines) > insert_at and lines[insert_at].strip().lower().startswith("title"):
        insert_at += 1
    return "\n".join(lines[:insert_at] + ["    " + l for l in repaired_lines] + lines[insert_at:])

def parse_model_sections(raw: str) -> dict[str, Any]:
    context_diagram = _normalize_diagram_linebreaks(_extract_section(raw, "---DIAGRAM---", "---DOCS---"))
    docs = _extract_section(raw, "---DOCS---", "---COMPONENTS---")
    components_raw = _extract_section(raw, "---COMPONENTS---", "---END---")

    try:
        components = json.loads(components_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Components section is not valid JSON: {e}\nRaw: {components_raw!r}") from e

    context_diagram = _repair_undeclared_ids(context_diagram, components)
    _validate_diagram_ids(context_diagram)

    return {
        "context_diagram": context_diagram,
        "docs": docs,
        "components": components,
    }


def call_architect(
    spec: dict[str, Any],
    pricing_context: dict[str, Any],
    base_url: str = BASE_URL,
    artifacts_root: Path = ARTIFACTS_ROOT,
    timeout: float = ARCHITECT_HTTP_TIMEOUT_S,
) -> ArchitectOutput:
    adrs = _load_recent_adrs(artifacts_root)
    prior_decisions_text = _format_adr_context(adrs)

    payload = {
        "model": MODEL_NAME,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(spec, pricing_context, prior_decisions_text)},
        ],
    }
    response = httpx.post(base_url, json=payload, timeout=timeout)
    response.raise_for_status()
    choice = response.json()["choices"][0]
    raw_text = choice["message"]["content"]

    if choice.get("finish_reason") == "length":
        raise ValueError(
            f"Architect: Gemma hit max_tokens ({MAX_TOKENS}) before finishing output. "
            f"Raise ARCHITECT_TOKEN_BUDGET or shorten the prompt. "
            f"Partial output: {raw_text[:300]}"
        )
    
    if os.environ.get("ARCHITECT_DEBUG_RAW") == "1":
        print(f"ARCHITECT finish_reason={choice.get('finish_reason')!r}")
        print(f"ARCHITECT RAW OUTPUT:\n{raw_text!r}")
    
    sections = parse_model_sections(raw_text)

    provenance = DiagramProvenance(
        model=MODEL_NAME,
        generated_at=datetime.now(timezone.utc),
        spec_version=spec.get("spec_version"),
        informed_by_adrs=[adr.adr_id for adr in adrs],
    )

    return ArchitectOutput(
        context_diagram=sections["context_diagram"],
        diagram_source=provenance,
        docs=sections["docs"],
        components=sections["components"],
    )


if __name__ == "__main__":
    stub_spec = {
        "spec_version": 1,
        "project_name": "Agent Swarm at the Edge",
        "purpose": "Local multi-agent pipeline for C4 + ADR generation",
        "components_hint": ["llama-server", "PostgreSQL", "DBOS pipeline", "mermaid.ink"],
    }
    stub_pricing_context = {
        "postgres_rds_equivalent_usd_month": 0,
        "note": "Fully local deployment, Infracost stub for MVP",
    }

    result = call_architect(stub_spec, stub_pricing_context)
    print(result.model_dump_json(indent=2))