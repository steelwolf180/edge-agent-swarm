"""ADR schemas for the Scribe agent and the on-disk artifact store.

Two distinct schemas, deliberately not one:

ADROutput -- transient. What Scribe's LFM call produces and what
run_scribe() validates against. MVP-locked to affected_diagrams=["context"]
(KICKOFF_CHECKLIST.md §6). No identity or lifecycle fields -- those are
system-assigned, not something the LLM should ever populate.

ADRRecord -- persisted. On-disk shape for artifacts/v<n>/adr_<NNNN>.md
(markdown with YAML-style frontmatter for structured fields, ## headers
for the Context/Decision/Consequences prose body -- spec §5 file system
store, §9 paper trail). Architect is the first reader, pulling prior
accepted decisions in before generating a new diagram.

Lifecycle split: proposed -> accepted/rejected lives entirely in Postgres
(pipeline_runs / revision_cycles / artifacts, spec §4). A file under
artifacts/v<n>/ is only ever written once, at the moment a decision is
accepted -- matching spec §5 exactly (rejection writes revision_notes to
the blackboard, never a file). Consequence: status is "accepted" at write
time, every time. Architect never sees "proposed" or "rejected", not
because it filters them out, but because those states are architecturally
incapable of reaching a file.

"superseded" is the one lifecycle event that happens after a file already
exists on disk. It's handled without touching the DB or the old file: a
later accepted ADR names the old adr_id in its own `supersedes` list. The
loader (agents/architect.py, not yet built) treats that forward reference
as authoritative, so Architect's read stays entirely file-based even here.

Example file:
    ---
    adr_id: adr_0001
    spec_version: 1
    status: accepted
    diff_summary: Added orchestration section
    affected_diagrams: [context]
    created: 2026-07-01T10:00:00Z
    ---
    # ADR 0001: Use DBOS over Temporal
    ## Context
    Needed a durable workflow engine for local, resumable pipeline execution.
    ## Decision
    Use DBOS over Temporal for a lighter local footprint.
    ## Consequences
    Ties orchestration to Postgres; no separate Temporal cluster required.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ADROutput(BaseModel):
    """Scribe's direct LFM output. Validated in run_scribe() before anything
    touches the filesystem or DB. Never written to disk as-is -- build_adr_record()
    wraps it with identity/lifecycle fields at the moment of DB approval (§7)."""

    context: str = Field(..., description="Why this decision needed to be made")
    decision: str = Field(..., description="What was decided")
    consequences: str = Field(..., description="Trade-offs and downstream effects")
    diff_summary: str = Field(
        ..., description="Human-readable summary of the spec diff that triggered this ADR"
    )
    affected_diagrams: list[Literal["context"]] = Field(
        default_factory=lambda: ["context"],
        description=(
            "C4 diagram levels affected. MVP-locked to 'context' (L1 System Context). "
            "'container' is not a valid value until L2 lands in v2."
        ),
    )


class ADRRecord(BaseModel):
    """On-disk shape, matches ADROutput fields plus identity/versioning.

    status is kept as the full four-value Literal for documentation parity
    with the Nygard/MADR standard, and as cheap defensive filtering against a
    stray file someone drops into the folder by hand -- but in the designed
    pipeline, only "accepted" and (via inference through `supersedes`)
    "superseded" are ever actually reachable here. See module docstring for
    the full DB/file lifecycle split.
    """

    adr_id: str = Field(..., description="e.g. 'adr_0001', stable filename-derived id")
    spec_version: int = Field(..., description="Spec version this ADR was generated against")
    status: Literal["proposed", "accepted", "rejected", "superseded"] = Field(
        default="accepted",
        description="Always 'accepted' at write time -- files are only written post-decision. "
        "See class docstring for the DB/file lifecycle split.",
    )
    supersedes: list[str] = Field(
        default_factory=list,
        description="adr_id values this ADR revisits and replaces. Set on the NEW "
        "record, not the old one -- a forward reference, so superseding a decision "
        "never requires editing a historical file. The loader treats any adr_id "
        "named here as superseded even if its own status field was never updated.",
    )
    context: str
    decision: str
    consequences: str
    diff_summary: str
    affected_diagrams: list[Literal["context", "container"]]
    created: datetime


def build_adr_record(
    output: ADROutput,
    *,
    adr_id: str,
    spec_version: int,
    created: datetime,
    supersedes: list[str] | None = None,
) -> ADRRecord:
    """Bridge Scribe's transient ADROutput into a persisted ADRRecord.

    Called once, at the point of human approval (§7 pipeline wiring) --
    never by Scribe itself. status is not a parameter: it's always
    "accepted" here, because this function only runs when a file is about
    to be written, and files are only written on approval (see module
    docstring). affected_diagrams widens from ADROutput's MVP-locked
    Literal["context"] to ADRRecord's Literal["context", "container"] --
    a safe widening since every ADROutput value is already a valid
    ADRRecord value; the reverse would not be true.
    """
    return ADRRecord(
        adr_id=adr_id,
        spec_version=spec_version,
        status="accepted",
        supersedes=supersedes or [],
        context=output.context,
        decision=output.decision,
        consequences=output.consequences,
        diff_summary=output.diff_summary,
        affected_diagrams=list(output.affected_diagrams),
        created=created,
    )
