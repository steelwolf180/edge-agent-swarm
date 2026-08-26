"""
pipeline/view_result.py — renders a workflow's step outputs as a
human-readable Markdown file, for review BEFORE calling send_approval.py.

The pipeline currently has no built-in way to surface Architect's diagram,
Scribe's ADR, Critic's gaps, and Judge's scores before the human decision
gate (run.py only prints the final result AFTER a decision is sent). This
script closes that gap from the outside by reading step outputs directly
via DBOS.list_workflow_steps(), the same approach used ad-hoc during
manual review, now written to a file instead of a truncated terminal dump.

Usage:
    python pipeline/view_result.py <workflow_id>
    python pipeline/view_result.py <workflow_id> --out-dir artifacts/review

Writes:
    <out-dir>/<workflow_id>.md    human-readable review doc
    <out-dir>/<workflow_id>.json  raw step outputs, untruncated

NOTE: this is a read-only review aid. It does not call DBOS.set_event() or
change run.py's blackboard behavior — see the open item to move this
content onto the blackboard properly so it surfaces inline during a normal
run, rather than requiring this script to be run separately.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from dbos import DBOS, DBOSConfig  # noqa: E402
from pipeline.review_render import render_markdown, fmt_json as _fmt_json

def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key} (check your .env file)")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a workflow's step outputs for human review.")
    parser.add_argument("workflow_id", help="workflow_id printed by pipeline/run.py")
    parser.add_argument("--out-dir", default="artifacts/review", help="Directory to write review files to")
    args = parser.parse_args()

    system_database_url = _require_env("DBOS_SYSTEM_DATABASE_URL")
    config: DBOSConfig = {
        "name": "edge-agent-swarm",
        "system_database_url": system_database_url,
        "run_admin_server": False,
    }
    DBOS(config=config)
    DBOS.launch()

    steps = DBOS.list_workflow_steps(args.workflow_id)
    if not steps:
        raise SystemExit(f"No steps found for workflow_id={args.workflow_id!r} — check the ID is correct.")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    md_path = out_dir / f"{args.workflow_id}.md"
    json_path = out_dir / f"{args.workflow_id}.json"

    md_path.write_text(render_markdown(args.workflow_id, steps))
    json_path.write_text(_fmt_json({s["function_name"]: s.get("output") for s in steps}))

    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()