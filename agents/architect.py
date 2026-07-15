"""
agents/architect.py

Architect agent (Gemma 4 E4B QAT via llama-server, router mode).
No tools — pure text generation. Reads spec + Researcher's pricing
context off the blackboard, plus recent prior ADRs scanned from
artifacts/v*/adr_*.md, and emits ArchitectOutput.

DBOS wiring (spec §7) isn't done yet, so this module takes a plain
blackboard dict rather than reading DBOS.get_event() directly. Once
§7 lands, the pipeline step wraps call_architect() and hands it the
real blackboard state; the ADR-folder read stays here regardless,
since it's a filesystem concern local to this agent, not blackboard
state.

The model only generates context_diagram (Mermaid), docs, and
components. diagram_source (provenance: model, timestamp, spec_version,
informed_by_adrs) is assembled by this code after the call — it is
never asked of the LLM.

Usage (standalone smoke test, no DBOS/pipeline needed):
    python agents/architect.py
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

MODEL_NAME = _require_env("GEMMA_MODEL_NAME")  # must match the alias in models.ini
MAX_TOKENS = int(_require_env("ARCHITECT_TOKEN_BUDGET"))  # per spec §6 context window budget for Architect

ARTIFACTS_ROOT = Path("artifacts")
ADR_GLOB_PATTERN = "v*/adr_*.md"  # scans every versioned run folder, not a flat artifacts/adr/ dir
ADR_CONTEXT_LIMIT = 3  # most recent ADRs to load — keep small, this eats into the 900 token budget
ADR_DECISION_TRUNCATE = 200  # chars per decision summary in the prompt

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)
_SECTION_RE = re.compile(r"##\s*(Context|Decision|Consequences)\s*\n(.*?)(?=\n##|\Z)", re.DOTALL | re.IGNORECASE)


def _parse_frontmatter(block: str) -> dict[str, Any]:
    """Minimal key: value parser — no PyYAML dependency for a handful of scalar fields."""
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
    """Parse one adr_*.md file (frontmatter + ## Context/Decision/Consequences body) into an ADRRecord."""
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
    """Load the most recent ADR records across all artifacts/v*/ folders, newest spec_version first.

    Returns an empty list if no versioned artifact folders exist yet (first
    run) or none contain valid ADR files. Malformed files are skipped with
    a warning, not fatal — a corrupt ADR shouldn't block diagram generation.
    """
    if not artifacts_root.exists():
        return []

    records: list[ADRRecord] = []
    for path in sorted(artifacts_root.glob(ADR_GLOB_PATTERN)):
        try:
            records.append(_parse_adr_markdown(path))
        except (ValueError, ValidationError, KeyError) as e:
            print(f"WARNING: skipping malformed ADR file {path}: {e}")
            continue

    # status is "accepted" for every real file — proposed/rejected live
    # only in Postgres and never reach disk (see ADRRecord docstring for
    # the resolved DB/file lifecycle split). This filter is now mostly
    # defensive: a stray non-accepted file dropped in by hand shouldn't
    # leak into Architect's context. supersedes is the real exclusion
    # signal, cross-referenced next.
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

---DIAGRAM---
C4Context
    title <short diagram title>
    Person(...)
    System(...)
    Rel(...)
---DOCS---
<2-4 sentences of plain prose explaining the diagram>
---COMPONENTS---
<JSON array of objects with keys: id, name, type (person|internal_system|external_system), description, technology (nullable), redundant (bool)>
---END---
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


def parse_model_sections(raw: str) -> dict[str, Any]:
    """Parse the marker-delimited completion into the model-generated fields only.

    Returns dict with keys: context_diagram, docs, components.
    Does NOT include diagram_source — that's provenance, added by the caller.
    Raises ValueError on malformed output so callers (and smoke tests) fail
    loudly instead of silently passing bad data downstream.
    """
    context_diagram = _extract_section(raw, "---DIAGRAM---", "---DOCS---")
    docs = _extract_section(raw, "---DOCS---", "---COMPONENTS---")
    components_raw = _extract_section(raw, "---COMPONENTS---", "---END---")

    try:
        components = json.loads(components_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Components section is not valid JSON: {e}\nRaw: {components_raw!r}") from e

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
    timeout: float = 120.0,
) -> ArchitectOutput:
    """Single completion call against llama-server. No tool calling, no --jinja.

    Loads recent ADRs from artifacts/v*/adr_*.md before prompting, and
    records which ones were used in the returned provenance.
    """
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
    raw_text = response.json()["choices"][0]["message"]["content"]
    
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
    # Stub blackboard for isolated testing — real values come from DBOS
    # once §7 wiring is done. Keep small: ~900 token budget for this agent.
    stub_spec = {
        "spec_version": 1,
        "project_name": "Agent Swarm at the Edge",
        "purpose": "Local multi-agent pipeline for C4 + ADR generation",
        "components_hint": ["llama-server", "PostgreSQL", "DBOS pipeline", "mermaid.ink"],
    }
    stub_pricing_context = {
        "postgres_rds_equivalent_usd_month": 0,  # self-hosted, no cloud cost
        "note": "Fully local deployment, Infracost stub for MVP",
    }

    result = call_architect(stub_spec, stub_pricing_context)
    print(result.model_dump_json(indent=2))
