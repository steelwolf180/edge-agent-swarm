"""
pipeline/run.py — DBOS workflow wiring the 5 validated agents (§6, all complete).

v2 — corrected against actual agents/*.py (uploaded 22 July 2026). The first
draft of this file guessed at run_architect()/run_judge() signatures from the
spec's tables alone; those don't match the real code.

REAL SIGNATURES THIS FILE WIRES:
    researcher.run_researcher(spec_text: str) -> ResearcherOutput            [sync]
    architect.call_architect(spec: dict, pricing_context: dict) -> ArchitectOutput
        (reads prior ADRs itself from artifacts/v*/adr_*.md — no prior_adr arg) [sync]
    scribe.run_scribe(prior_spec: ArchitectureSpec | None, current_spec: ArchitectureSpec,
                       blackboard_context: str) -> ADROutput                  [async]
    critic.run_critic(architect_output: ArchitectOutput, adr_output: ADROutput,
                       blackboard_context: str) -> CriticOutput               [async]
    judge.calculate_metrics(researcher, architect, critic, spec, adr, adr_count,
                             rubric=None) -> JudgeOutput                      [sync, pure]

STILL OUT OF SCOPE for this pass (separate checklist items, don't fold in here):
    - Real DBOS blackboard set_event/get_event (the two _summarize_for_* helpers
      below are string-building placeholders standing in for a real blackboard read)
    - spec_version merge into ArchitectureSpec.model_dump()
    - Thermal guard as its own @DBOS.step()
    - DBOS.recv()/send() human review gate
    - Approval persistence (adr_id, build_adr_record(), markdown serializer, supersedes)

OPEN DESIGN QUESTION surfaced by this wiring pass, not resolved here: the spec
flows into three different shapes — raw text (Researcher), dict (Architect),
typed ArchitectureSpec (Scribe/Judge). This file derives spec_text as a JSON
dump of the same dict Architect gets, which is a reasonable default but wasn't
what Researcher was smoke-tested against (a plain descriptive sentence) — worth
confirming Researcher's tool-call fallback regex (_extract_services_fallback)
still finds service names inside a JSON blob before trusting this end-to-end.

SERIALIZATION NOTE: step boundaries pass .model_dump(mode="json") dicts, not
raw Pydantic instances, and re-validate on the receiving side. Plain
.model_dump() leaves datetime fields (e.g. DiagramProvenance.generated_at) as
live datetime objects — DBOS's own checkpointing survives that fine (default
step serialization is pickle), but stdlib json.dumps() doesn't, which is what
broke the final result print in the first real run. mode="json" fixes it at
the source rather than patching the print call, since these same dicts will
eventually get written to Postgres/markdown in the approval-persistence step.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from dbos import DBOS, DBOSConfig
from dotenv import load_dotenv

load_dotenv()

from agents.architect import call_architect
from agents.critic import run_critic
from agents.judge import calculate_metrics
from agents.researcher import run_researcher
from agents.scribe import run_scribe

from schemas.adr import ADROutput
from schemas.architect import ArchitectOutput
from schemas.critic import CriticOutput
from schemas.judge import JudgeOutput
from schemas.researcher import ResearcherOutput
from schemas.spec import ArchitectureSpec


def _require_spec_version(spec: dict) -> int:
    """ArchitectureSpec (schemas/spec.py) deliberately has no spec_version
    field — it's caller-supplied metadata, not user-submitted content. That
    means it only survives if callers keep passing around the raw dict
    (as this workflow does) rather than rebuilding it from a validated
    ArchitectureSpec.model_dump(), which would silently drop it. Fail loudly
    here rather than let it surface later as a None in DiagramProvenance.
    """
    spec_version = spec.get("spec_version")
    if spec_version is None:
        raise ValueError(
            "spec dict is missing 'spec_version' — required at the top level "
            "alongside the ArchitectureSpec fields, since ArchitectureSpec "
            "itself doesn't carry it (see schemas/spec.py docstring)."
        )
    return spec_version


# ---------------------------------------------------------------------------
# Blackboard placeholders — replace when real set_event/get_event lands
# ---------------------------------------------------------------------------
# Scribe and Critic each want a different slice as "blackboard_context":
# Scribe's is documented as Researcher + Architect output summarized (it has
# no typed architect_output param of its own); Critic's is spec + pricing
# only, since Critic already receives architect_output as a typed argument.

def _summarize_for_scribe(researcher: ResearcherOutput, architect: ArchitectOutput) -> str:
    return (
        f"Researcher pricing summary: {researcher.summary}\n"
        f"Architect docs: {architect.docs}"
    )


def _summarize_for_critic(spec: dict, researcher: ResearcherOutput) -> str:
    integration_points = spec.get("functional_requirements", {}).get("integration_points", [])
    return (
        f"Required integration points: {integration_points}\n"
        f"Researcher pricing summary: {researcher.summary}"
    )


# ---------------------------------------------------------------------------
# Step wrappers — each returns/accepts plain dicts across the DBOS boundary
# ---------------------------------------------------------------------------

@DBOS.step()
def researcher_step(spec_text: str) -> dict:
    return run_researcher(spec_text).model_dump(mode="json")


@DBOS.step()
def architect_step(spec: dict, pricing_context: dict) -> dict:
    return call_architect(spec, pricing_context).model_dump(mode="json")


@DBOS.step()
async def scribe_step(
    prior_spec: dict | None,
    current_spec: dict,
    blackboard_context: str,
) -> dict:
    prior = ArchitectureSpec.model_validate(prior_spec) if prior_spec else None
    current = ArchitectureSpec.model_validate(current_spec)
    adr_output = await run_scribe(prior, current, blackboard_context)
    return adr_output.model_dump(mode="json")


@DBOS.step()
async def critic_step(
    architect_output: dict,
    adr_output: dict,
    blackboard_context: str,
) -> dict:
    architect = ArchitectOutput.model_validate(architect_output)
    adr = ADROutput.model_validate(adr_output)
    critic_output = await run_critic(architect, adr, blackboard_context)
    return critic_output.model_dump(mode="json")


@DBOS.step()
def judge_step(
    researcher_output: dict,
    architect_output: dict,
    critic_output: dict,
    spec: dict,
    adr_output: dict,
    adr_count: int,
) -> dict:
    result = calculate_metrics(
        researcher=ResearcherOutput.model_validate(researcher_output),
        architect=ArchitectOutput.model_validate(architect_output),
        critic=CriticOutput.model_validate(critic_output),
        spec=ArchitectureSpec.model_validate(spec),
        adr=ADROutput.model_validate(adr_output),
        adr_count=adr_count,
    )
    return result.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

@DBOS.workflow()
async def architecture_review_workflow(
    spec: dict,
    prior_spec: dict | None = None,
    adr_count: int = 1,
) -> dict:
    """
    Sequential 5-agent pipeline (spec §5 Data Flow). Model swap sequence
    (Gemma -> LFM -> Gemma) happens inside llama-server, driven by which
    agent function is called — nothing to orchestrate here.

    spec_text derivation (JSON dump of the same dict Architect receives) is
    a placeholder — see module docstring's "OPEN DESIGN QUESTION" note.
    """
    _require_spec_version(spec)
    spec_text = json.dumps(spec, indent=2)

    DBOS.logger.info("[1/5] Researcher starting (Gemma)...")
    researcher_output = researcher_step(spec_text)
    DBOS.logger.info("[1/5] Researcher done.")

    DBOS.logger.info("[2/5] Architect starting (Gemma)...")
    architect_output = architect_step(
        spec,
        researcher_output["pricing_context"],
    )
    DBOS.logger.info("[2/5] Architect done.")

    # --- model swap: Gemma -> LFM happens inside llama-server here ---
    DBOS.logger.info("Model swap: Gemma -> LFM")

    scribe_blackboard = _summarize_for_scribe(
        ResearcherOutput.model_validate(researcher_output),
        ArchitectOutput.model_validate(architect_output),
    )
    DBOS.logger.info("[3/5] Scribe starting (LFM)...")
    adr_output = await scribe_step(prior_spec, spec, scribe_blackboard)
    DBOS.logger.info("[3/5] Scribe done.")

    critic_blackboard = _summarize_for_critic(spec, ResearcherOutput.model_validate(researcher_output))
    DBOS.logger.info("[4/5] Critic starting (LFM)...")
    critic_output = await critic_step(architect_output, adr_output, critic_blackboard)
    DBOS.logger.info("[4/5] Critic done.")

    # --- model swap: LFM -> Gemma happens inside llama-server here ---
    DBOS.logger.info("Model swap: LFM -> Gemma")

    DBOS.logger.info("[5/5] Judge starting (calculator, no LLM call)...")
    judge_output = judge_step(
        researcher_output,
        architect_output,
        critic_output,
        spec,
        adr_output,
        adr_count,
    )
    DBOS.logger.info("[5/5] Judge done. Pipeline complete.")

    return {
        "researcher": researcher_output,
        "architect": architect_output,
        "adr": adr_output,
        "critic": critic_output,
        "judge": judge_output,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run(spec_path: str, prior_spec_path: str | None, adr_count: int) -> None:
    config: DBOSConfig = {
        "name": "edge-agent-swarm",
        "system_database_url": os.environ.get("DBOS_SYSTEM_DATABASE_URL"),
        # DBOS's admin server defaults to port 3001, which mermaid-ink already
        # owns on this stack (spec §4 Integration Points). Move it off that port
        # rather than let it silently fail to bind (as it just did).
        "admin_port": 3010,
    }
    DBOS(config=config)
    DBOS.launch()

    with open(spec_path) as f:
        spec = json.load(f)
    prior_spec = json.load(open(prior_spec_path)) if prior_spec_path else None

    handle = await DBOS.start_workflow_async(
        architecture_review_workflow, spec, prior_spec, adr_count
    )
    print(f"workflow_id: {handle.workflow_id}")

    result = await handle.get_result()
    print(json.dumps(result, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Agent Swarm at the Edge pipeline.")
    parser.add_argument("--spec", required=True, help="Path to spec JSON file")
    parser.add_argument("--prior-spec", default=None, help="Path to prior spec JSON file, if any")
    parser.add_argument("--adr-count", type=int, default=1, help="ADRs generated for this diff")
    args = parser.parse_args()
    asyncio.run(_run(args.spec, args.prior_spec, args.adr_count))


if __name__ == "__main__":
    main()
