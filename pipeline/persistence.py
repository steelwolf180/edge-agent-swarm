"""
pipeline/persistence.py — approval-time persistence for the Agent Swarm
pipeline (§7). Two responsibilities:

1. ADR file writes: bridges Scribe's ADROutput into a written ADRRecord
   under artifacts/v<n>/adr_<NNNN>.md, matching the frontmatter shape
   agents/architect.py already parses (schemas/adr.py, architect.py's
   _parse_adr_markdown / _FRONTMATTER_RE / _SECTION_RE).
2. Postgres writes: spec_versions / pipeline_runs / artifacts /
   revision_cycles, per schemas/001_app_tables.sql + 002_add_adr_id.sql.
   Same database as DBOS itself (DBOS_SYSTEM_DATABASE_URL).

Design decisions locked in per checklist §7:
  - adr_id: sequential ("adr_0001", "adr_0002", ...), not UUID — matches
    the worked example in schemas/adr.py's module docstring and the
    ADR_GLOB_PATTERN "v*/adr_*.md" filename convention architect.py
    already reads by.
  - supersedes: human-specified at approval time (via --supersedes on
    send_approval.py), not inferred from spec diff.
  - artifacts.adr_id: added via 002_add_adr_id.sql so the Postgres row
    links back to the markdown file (user decision, 24 Jul).

All DB functions open/close their own connection per call rather than
sharing one across a step — steps are meant to be small, retryable units,
and this avoids holding a connection open across DBOS checkpointing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json as _json
import os
import re

import psycopg
from dotenv import load_dotenv

load_dotenv()

from schemas.adr import ADROutput, ADRRecord, build_adr_record
from schemas.architect import ArchitectOutput
from schemas.judge import JudgeOutput

ARTIFACTS_ROOT = Path("artifacts")
ADR_ID_RE = re.compile(r"adr_(\d+)\.md$")


# ---------------------------------------------------------------------------
# ADR file writes
# ---------------------------------------------------------------------------

def _next_adr_id(artifacts_root: Path = ARTIFACTS_ROOT) -> str:
    """Scans artifacts/v*/adr_*.md filenames (the filename IS the adr_id
    by convention) for the highest existing sequence number. Global across
    spec versions, not per-version: adr_id must be a stable identifier
    that `supersedes` can reference regardless of which artifacts/v<n>/
    folder it lives in."""
    if not artifacts_root.exists():
        return "adr_0001"

    max_n = 0
    for path in artifacts_root.glob("v*/adr_*.md"):
        match = ADR_ID_RE.search(path.name)
        if match:
            max_n = max(max_n, int(match.group(1)))

    return f"adr_{max_n + 1:04d}"


def _format_frontmatter_list(items: list[str]) -> str:
    """Matches architect.py's _parse_frontmatter bracket-list handling:
    `[a, b, c]`. No quotes needed — that parser strips them either way."""
    return "[" + ", ".join(items) + "]"


def serialize_adr_markdown(record: ADRRecord) -> str:
    """Inverse of architect.py's _parse_adr_markdown. Field order and
    header casing ('## Context' etc.) must match _SECTION_RE /
    _FRONTMATTER_RE exactly — a mismatch here doesn't raise loudly on the
    read side, it just gets warn-and-skipped by _load_recent_adrs(),
    silently dropping a decision from PRIOR_DECISIONS."""
    frontmatter = "\n".join([
        f"adr_id: {record.adr_id}",
        f"spec_version: {record.spec_version}",
        f"status: {record.status}",
        f"supersedes: {_format_frontmatter_list(record.supersedes)}",
        f"diff_summary: {record.diff_summary}",
        f"affected_diagrams: {_format_frontmatter_list(record.affected_diagrams)}",
        f"created: {record.created.isoformat()}",
    ])
    body = "\n".join([
        "## Context",
        record.context.strip(),
        "## Decision",
        record.decision.strip(),
        "## Consequences",
        record.consequences.strip(),
    ])
    return f"---\n{frontmatter}\n---\n{body}\n"


def write_adr_file(record: ADRRecord, artifacts_root: Path = ARTIFACTS_ROOT) -> Path:
    """Writes artifacts/v<spec_version>/adr_<NNNN>.md."""
    version_dir = artifacts_root / f"v{record.spec_version}"
    version_dir.mkdir(parents=True, exist_ok=True)

    path = version_dir / f"{record.adr_id}.md"
    if path.exists():
        raise FileExistsError(
            f"{path} already exists — refusing to overwrite an approved ADR. "
            f"adr_id collision suggests _next_adr_id() raced or was called twice."
        )
    path.write_text(serialize_adr_markdown(record))
    return path


def persist_adr(
    adr_output: ADROutput,
    spec_version: int,
    supersedes: list[str] | None = None,
    artifacts_root: Path = ARTIFACTS_ROOT,
) -> ADRRecord:
    """Full approval-time bridge: assign adr_id, build the ADRRecord,
    write the file. Call from a @DBOS.step() in run.py — filesystem writes
    are non-deterministic, same rule as everything else in this project."""
    adr_id = _next_adr_id(artifacts_root)
    record = build_adr_record(
        adr_output,
        adr_id=adr_id,
        spec_version=spec_version,
        created=datetime.now(timezone.utc),
        supersedes=supersedes,
    )
    write_adr_file(record, artifacts_root)
    return record


# ---------------------------------------------------------------------------
# Postgres writes
# ---------------------------------------------------------------------------

def _db_url() -> str:
    url = os.environ.get("DBOS_SYSTEM_DATABASE_URL")
    if not url:
        raise ValueError("DBOS_SYSTEM_DATABASE_URL not set — same DB as DBOS system tables.")
    return url


def ensure_spec_version_row(spec_version: int, spec_json: dict) -> int:
    """Returns spec_versions.id (the SERIAL PK pipeline_runs.spec_version_id
    references) for this version, inserting if it doesn't exist yet.
    spec_versions.version has no UNIQUE constraint in 001_app_tables.sql,
    so this checks-then-inserts rather than relying on ON CONFLICT."""
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM spec_versions WHERE version = %s ORDER BY id DESC LIMIT 1",
                (spec_version,),
            )
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                "INSERT INTO spec_versions (version, spec_json) VALUES (%s, %s) RETURNING id",
                (spec_version, _json.dumps(spec_json)),
            )
            spec_version_id = cur.fetchone()[0]
        conn.commit()
        return spec_version_id


def ensure_pipeline_run_row(workflow_id: str, spec_version_id: int) -> None:
    """workflow_id is PRIMARY KEY — ON CONFLICT DO NOTHING makes this safe
    to call again on DBOS step retry/recovery without erroring."""
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO pipeline_runs (workflow_id, spec_version_id, status)
                   VALUES (%s, %s, 'pending')
                   ON CONFLICT (workflow_id) DO NOTHING""",
                (workflow_id, spec_version_id),
            )
        conn.commit()


def update_pipeline_run_status(workflow_id: str, status: str) -> None:
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pipeline_runs SET status = %s, modified = now() WHERE workflow_id = %s",
                (status, workflow_id),
            )
        conn.commit()


def insert_artifact_row(
    workflow_id: str,
    architect_output: ArchitectOutput,
    adr_record: ADRRecord,
    judge_output: JudgeOutput,
) -> int:
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO artifacts
                   (workflow_id, context_diagram, diagram_source, adr_id,
                    adr_context, adr_decision, adr_consequences, judge_scores)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    workflow_id,
                    architect_output.context_diagram,
                    architect_output.diagram_source.model_dump_json(),
                    adr_record.adr_id,
                    adr_record.context,
                    adr_record.decision,
                    adr_record.consequences,
                    _json.dumps(judge_output.scores),
                ),
            )
            artifact_id = cur.fetchone()[0]
        conn.commit()
        return artifact_id


def insert_revision_cycle_row(workflow_id: str, revision_notes: str) -> int:
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO revision_cycles (workflow_id, revision_notes) VALUES (%s, %s) RETURNING id",
                (workflow_id, revision_notes),
            )
            revision_id = cur.fetchone()[0]
        conn.commit()
        return revision_id