"""
pipeline/send_approval.py — sends a human review decision to a running
architecture_review_workflow, unblocking its DBOS.recv() gate (§7).

Usage:
    python pipeline/send_approval.py <workflow_id>                    # approve
    python pipeline/send_approval.py <workflow_id> --reject "notes"   # reject

This is a separate process from the running workflow, so it can't call
DBOS.send() directly (that's only for the same app instance) — it uses
DBOSClient, which connects to the same system database and can send()
into any workflow_id, per the DBOS docs ("you can also call send from
outside your DBOS application with the DBOS Client").

Topic/message contract — must match architecture_review_workflow's
DBOS.recv_async(topic=REVIEW_TOPIC) call in run.py:
    topic:   "review_decision"
    message: {"approved": bool, "notes": str | None}

--reject requires non-empty notes, per spec §2 Key User Flows:
"reject with required comments".

NOTE: DBOS.recv_async / client.send with a topic kwarg are inferred from
the async-workflow and "call from outside the app" patterns in the DBOS
docs, not copy-pasted from a confirmed working example — worth a quick
smoke test (send to a dummy recv_async() workflow) before trusting this
against a real pipeline run.
"""

from __future__ import annotations

import argparse
import os

from dbos import DBOSClient
from dotenv import load_dotenv

load_dotenv()

REVIEW_TOPIC = "review_decision"


def send_decision(
    workflow_id: str,
    approved: bool,
    notes: str | None,
    supersedes: list[str] | None = None,
) -> None:
    system_database_url = os.environ.get("DBOS_SYSTEM_DATABASE_URL")
    if not system_database_url:
        raise ValueError(
            "DBOS_SYSTEM_DATABASE_URL is not set in .env — send_approval.py "
            "must connect to the same system database as pipeline/run.py, "
            "same no-silent-fallback pattern as the rest of the project."
        )

    client = DBOSClient(system_database_url=system_database_url)
    message = {"approved": approved, "notes": notes, "supersedes": supersedes or []}
    client.send(workflow_id, message, topic=REVIEW_TOPIC)

    verb = "Approved" if approved else "Rejected"
    suffix = f" notes={notes!r}" if notes else ""
    suffix += f" supersedes={supersedes!r}" if supersedes else ""
    print(f"{verb} workflow_id={workflow_id}{suffix}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send a human review decision to a running pipeline workflow."
    )
    parser.add_argument("workflow_id", help="workflow_id printed by pipeline/run.py")
    parser.add_argument(
        "--reject",
        metavar="NOTES",
        default=None,
        help="Reject with required revision notes. Omit this flag to approve.",
    )
    parser.add_argument(
        "--supersedes",
        metavar="ADR_IDS",
        default=None,
        help="Comma-separated adr_ids this approval supersedes, e.g. adr_0003,adr_0004. "
             "Approve only — ignored if combined with --reject.",
    )
    args = parser.parse_args()

    if args.reject is not None:
        notes = args.reject.strip()
        if not notes:
            parser.error(
                "--reject requires non-empty notes (spec §2: rejection "
                "comments are required, not optional like approval)."
            )
        send_decision(args.workflow_id, approved=False, notes=notes)
    else:
        supersedes = (
            [s.strip() for s in args.supersedes.split(",") if s.strip()]
            if args.supersedes else None
        )
        send_decision(args.workflow_id, approved=True, notes=None, supersedes=supersedes)


if __name__ == "__main__":
    main()