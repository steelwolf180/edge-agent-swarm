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


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {key} (check your .env file)")
    return value


def _fmt_json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _render_researcher(output: dict) -> str:
    lines = ["## Researcher", ""]
    lines.append(f"**Services identified:** {', '.join(output.get('services_identified', []))}")
    lines.append("")
    lines.append("**Pricing:**")
    lines.append("")
    lines.append("| Service | Provider | Monthly USD | Notes |")
    lines.append("|---|---|---|---|")
    for p in output.get("pricing", []):
        lines.append(f"| {p.get('service')} | {p.get('provider')} | {p.get('monthly_cost_usd')} | {p.get('notes', '')} |")
    lines.append("")
    lines.append("**Summary:**")
    lines.append("")
    lines.append(output.get("summary", "").strip())
    lines.append("")
    return "\n".join(lines)


def _render_architect(output: dict) -> str:
    lines = ["## Architect", ""]
    provenance = output.get("diagram_source", {})
    adrs = provenance.get("informed_by_adrs", [])
    lines.append(f"**Model:** {provenance.get('model', '?')}  ")
    lines.append(f"**Informed by prior ADRs:** {', '.join(adrs) if adrs else 'none'}")
    lines.append("")
    lines.append("**Docs:**")
    lines.append("")
    lines.append(output.get("docs", "").strip())
    lines.append("")
    lines.append("**Diagram source (Mermaid C4Context):**")
    lines.append("")
    lines.append("```mermaid")
    lines.append(output.get("context_diagram", "").strip())
    lines.append("```")
    lines.append("")
    components = output.get("components", [])
    if components:
        lines.append(f"**Components ({len(components)}):**")
        lines.append("")
        lines.append("| id | name | type | redundant |")
        lines.append("|---|---|---|---|")
        for c in components:
            lines.append(f"| {c.get('id')} | {c.get('name')} | {c.get('type')} | {c.get('redundant')} |")
        lines.append("")
    return "\n".join(lines)


def _render_scribe(output: dict) -> str:
    lines = ["## Scribe (ADR draft)", ""]
    lines.append(f"**Context:** {output.get('context', '')}")
    lines.append("")
    lines.append(f"**Decision:** {output.get('decision', '')}")
    lines.append("")
    lines.append(f"**Consequences:** {output.get('consequences', '')}")
    lines.append("")
    lines.append(f"**Diff summary:** {output.get('diff_summary', '')}")
    lines.append("")
    lines.append(f"**Affected diagrams:** {', '.join(output.get('affected_diagrams', []))}")
    lines.append("")
    return "\n".join(lines)


def _render_critic(output: dict) -> str:
    lines = ["## Critic (devil's advocate)", ""]
    for field in ("gaps", "spofs", "missing_integrations"):
        items = output.get(field, [])
        lines.append(f"**{field}** ({len(items)}):")
        if items:
            for item in items:
                lines.append(f"- {item}")
        else:
            lines.append("- *(none reported)*")
        lines.append("")
    return "\n".join(lines)


def _render_judge(output: dict) -> str:
    lines = ["## Judge", ""]
    lines.append(f"**Recommendation:** `{output.get('recommendation', '?')}`  ")
    flagged = output.get("flagged_for_review", [])
    lines.append(f"**Flagged for review:** {', '.join(flagged) if flagged else 'none'}")
    lines.append("")
    lines.append("| Metric | Value | Target | Flag threshold | Direction | Flagged | Reason |")
    lines.append("|---|---|---|---|---|---|---|")
    for name, score in output.get("scores", {}).items():
        lines.append(
            f"| {name} | {score.get('value')} | {score.get('target')} | "
            f"{score.get('flag_threshold')} | {score.get('direction')} | "
            f"{score.get('flagged')} | {score.get('flag_reason') or ''} |"
        )
    lines.append("")
    lines.append(f"**Cost estimate:** ${output.get('cost_estimate', '?')}")
    lines.append("")
    return "\n".join(lines)


_RENDERERS = {
    "researcher_step": _render_researcher,
    "architect_step": _render_architect,
    "scribe_step": _render_scribe,
    "critic_step": _render_critic,
    "judge_step": _render_judge,
}


def render_markdown(workflow_id: str, steps: list[dict]) -> str:
    lines = [f"# Review — workflow `{workflow_id}`", ""]
    for step in steps:
        name = step["function_name"]
        output = step.get("output")
        if name in _RENDERERS and output:
            lines.append(_RENDERERS[name](output))
        elif name == "thermal_guard_step" and output:
            lines.append(f"*[thermal guard: {output.get('temp_c')}°C, waited {output.get('waited_s')}s]*")
            lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Approve: `python pipeline/send_approval.py " + workflow_id + "`  ")
    lines.append('Reject:  `python pipeline/send_approval.py ' + workflow_id + ' --reject "notes"`')
    lines.append("")
    return "\n".join(lines)


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