"""
pipeline/review_render.py — shared Markdown rendering for human review docs.

Extracted from view_result.py so the same rendering logic runs two ways:
  1. Inline, by pipeline/run.py, right after Judge finishes and before
     DBOS.recv() blocks — the review doc already exists by the time the
     terminal prints "Awaiting human review".
  2. Standalone, via `python pipeline/view_result.py <workflow_id>`, to
     regenerate a doc after the fact (e.g. deleted file, different machine).
"""
from __future__ import annotations
import json

def fmt_json(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)

def render_researcher(output: dict) -> str:
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

def render_architect(output: dict) -> str:
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

def render_scribe(output: dict) -> str:
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

def render_critic(output: dict) -> str:
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

def render_judge(output: dict) -> str:
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

RENDERERS = {
    "researcher_step": render_researcher,
    "architect_step": render_architect,
    "scribe_step": render_scribe,
    "critic_step": render_critic,
    "judge_step": render_judge,
}

def render_markdown(workflow_id: str, steps: list[dict]) -> str:
    lines = [f"# Review — workflow `{workflow_id}`", ""]
    for step in steps:
        name = step["function_name"]
        output = step.get("output")
        if name in RENDERERS and output:
            lines.append(RENDERERS[name](output))
        elif name == "thermal_guard_step" and output:
            lines.append(f"*[thermal guard: {output.get('temp_c')}°C, waited {output.get('waited_s')}s]*")
            lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("Approve: `python pipeline/send_approval.py " + workflow_id + "`  ")
    lines.append('Reject:  `python pipeline/send_approval.py ' + workflow_id + ' --reject "notes"`')
    lines.append("")
    return "\n".join(lines)

def render_markdown_from_outputs(workflow_id: str, outputs: dict[str, dict]) -> str:
    """Same output as render_markdown(), but takes a {step_name: output}
    dict directly instead of DBOS.list_workflow_steps()'s step-record shape
    — for calling inline mid-workflow, before anything needs re-fetching."""
    steps = [{"function_name": name, "output": out} for name, out in outputs.items()]
    return render_markdown(workflow_id, steps)