"""
schemas/adr.py

On-disk shape for a persisted ADR record. Scribe (not yet built) will be
the writer; Architect is the first reader, pulling prior decisions in
before generating a new diagram so it doesn't contradict settled choices.

File convention: artifacts/v<n>/adr_<NNNN>.md — markdown with a small
YAML-style frontmatter block for structured fields, and ## headers for
the prose body (Context / Decision / Consequences). This matches spec
§5 (file system stores "ADR markdown files") and §9 (artifacts/v1/*.md
committed to GitLab, rendered inline) rather than a separate JSON store.
ADRs live inside the same versioned folder as the diagram they informed,
not in a parallel artifacts/adr/ directory.

Example file:

    ---
    adr_id: adr_0001
    spec_version: 1
    status: accepted
    diff_summary: Added orchestration section
    affected_diagrams: [context]
    created_at: 2026-07-01T10:00:00Z
    ---

    # ADR 0001: Use DBOS over Temporal

    ## Context
    Needed a durable workflow engine for local, resumable pipeline execution.

    ## Decision
    Use DBOS over Temporal for a lighter local footprint.

    ## Consequences
    Ties orchestration to Postgres; no separate Temporal cluster required.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ADRRecord(BaseModel):
    """Matches spec §5 ADROutput fields, plus identity/versioning for on-disk storage.

    Lifecycle split, resolved: the proposed -> accepted/rejected decision
    lives entirely in Postgres (pipeline_runs / revision_cycles / artifacts
    tables, spec §4), which already exists to track mutable workflow state.
    A file under artifacts/v<n>/ is only ever written once, at the moment
    a decision is accepted — matching spec §5's documented flow exactly
    (rejection writes revision_notes to the blackboard, never a file).

    Consequence: status is "accepted" at write time, every time. Architect
    never sees "proposed" or "rejected" — not because it filters them out,
    but because those states are architecturally incapable of reaching a
    file. Architect stays a pure filesystem reader with no DB dependency.

    "superseded" is the one lifecycle event that happens after a file
    already exists on disk, and it's handled without touching the DB or
    the old file at all: a later accepted ADR names the old adr_id in its
    own `supersedes` list. The loader (agents/architect.py) treats that
    forward reference as authoritative, so Architect's read stays entirely
    file-based even for this case.

    status is kept as the full four-value Literal for documentation
    parity with the Nygard/MADR standard, and as cheap defensive filtering
    against a stray file someone drops into the folder by hand — but in
    the designed pipeline, only "accepted" and (via inference) "superseded"
    are ever actually reachable here.
    """

    adr_id: str = Field(..., description="e.g. 'adr_0001', stable filename-derived id")
    spec_version: int = Field(..., description="Spec version this ADR was generated against")
    status: Literal["proposed", "accepted", "rejected", "superseded"] = Field(
        default="accepted",
        description="Always 'accepted' at write time — files are only written post-decision. "
        "See class docstring for the DB/file lifecycle split.",
    )
    supersedes: list[str] = Field(
        default_factory=list,
        description="adr_id values this ADR revisits and replaces. Set on the NEW "
        "record, not the old one — a forward reference, so superseding a decision "
        "never requires editing a historical file. The loader treats any adr_id "
        "named here as superseded even if its own status field was never updated.",
    )
    context: str
    decision: str
    consequences: str
    diff_summary: str
    affected_diagrams: list[Literal["context", "container"]]
    created_at: datetime
