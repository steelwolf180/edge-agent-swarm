"""
agents/architect.py

Architect agent (Gemma 4 E4B QAT via llama-server, router mode).
No tools — pure text generation. Reads spec + Researcher's pricing
context off the blackboard, plus recent prior ADRs scanned from
artifacts/v*/adr_*.md, and emits ArchitectOutput.
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
ADR_GLOB_PATTERN = "v*/adr_*.md"
ADR_CONTEXT_LIMIT = 3
ADR_DECISION_TRUNCATE = 200

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)
_SECTION_RE = re.compile(r"##\s*(Context|Decision|Consequences)\s*\n(.*?)(?=\n##|\Z)", re.DOTALL | re.IGNORECASE)


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
