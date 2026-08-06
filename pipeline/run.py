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
    - DBOS.recv()/send() human review gate
    - Approval persistence (adr_id, build_adr_record(), markdown serializer, supersedes)

THERMAL GUARD (§7) — now wired below as thermal_guard_step(), called after
each of the four LLM-calling agent steps (Researcher, Architect, Scribe,
Critic; Judge is calculator-only, nothing follows it). Config comes from
THERMAL_MAX_C / THERMAL_POLL_S / THERMAL_TIMEOUT_S / THERMAL_COOLDOWN_S in
.env, no silent fallback — same pattern as *_TOKEN_BUDGET. THERMAL_MAX_C is
set more conservatively (60C) than the spec's original 65C pending a
sensors-monitored run to confirm actual climb rate near the 22 July
hard-power-off incident (checklist §8) — retune once that data exists.
This reduces risk but does NOT prove safety against a suspected EC/firmware-
level thermal cutoff, since `sensors` reads ACPI/coretemp, not the EC.

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
from datetime import datetime, timezone

import argparse
import asyncio
import json
import os
import re
import subprocess
import time

from dbos import DBOS, DBOSConfig
from dotenv import load_dotenv

load_dotenv()

from agents.architect import call_architect
from agents.critic import run_critic
from agents.judge import calculate_metrics
from agents.researcher import run_researcher
from agents.scribe import run_scribe

from schemas.adr import ADROutput, ADRRecord
from pipeline.persistence import (
    persist_adr,
    ensure_spec_version_row,
    ensure_pipeline_run_row,
    update_pipeline_run_status,
    insert_artifact_row,
    insert_revision_cycle_row,
)
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
# Thermal guard (§7) — wired as its own @DBOS.step() rather than inline
# workflow logic, since reading sensor state and sleeping are both
# non-deterministic. Config follows the same no-silent-fallback pattern as
# RESEARCHER_TOKEN_BUDGET etc.: a missing/invalid var raises loudly instead
# of quietly defaulting to a wrong number.
#
# NOTE ON NUMBERS: THERMAL_MAX_C is set more conservatively (60C) than the
# spec's original 65C pending a sensors-monitored run to confirm actual
# climb rate — see 22 July hard-power-off incident logged in checklist §8.
# THERMAL_COOLDOWN_S is a fixed, unconditional rest applied after every
# agent step regardless of sensor reading, on the theory that an EC/
# firmware-level cutoff may not correlate cleanly with what thermal_zone0
# (and therefore `sensors`) reports — a purely reactive guard could miss it.
# ---------------------------------------------------------------------------

class ThermalGuardTimeoutError(RuntimeError):
    pass


def _require_env_float(name: str) -> float:
    raw = os.environ.get(name)
    if raw is None:
        raise ValueError(
            f"{name} is not set in .env — thermal guard config has no silent "
            f"fallback, same as the *_TOKEN_BUDGET vars. Set it explicitly."
        )
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name}='{raw}' is not a valid number") from e

def _require_env(name: str) -> str:
    raw = os.environ.get(name)
    if raw is None:
        raise ValueError(
            f"{name} is not set — no silent fallback. A missing "
            f"DBOS_SYSTEM_DATABASE_URL used to make DBOS silently fall back "
            f"to a throwaway SQLite file instead of Postgres; this raises "
            f"loudly instead. Set it explicitly (check .env is being loaded "
            f"from the right cwd)."
        )
    return raw

def _read_cpu_package_temp_c() -> float:
    """Parses `sensors -u` for a package-level temp. Falls back to the max
    of all coretemp *_input readings if no explicit package sensor is
    found (naming varies by board/kernel — e.g. 'Package id 0' vs 'Tctl').

    This reads thermal_zone0-adjacent ACPI/coretemp data — NOT the EC.
    It cannot see an EC-level hard cutoff coming; it only reduces the
    chance of approaching one. Treat a clean reading here as reassuring,
    not as proof the EC threshold is far away.
    """
    try:
        out = subprocess.run(
            ["sensors", "-u"], capture_output=True, text=True, timeout=5, check=True
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"Could not read sensors: {e}") from e

    package_match = re.search(r"Package id 0:\s*\n\s*temp\d+_input:\s*([\d.]+)", out)
    if package_match:
        return float(package_match.group(1))

    tctl_match = re.search(r"Tctl:\s*\n\s*temp\d+_input:\s*([\d.]+)", out)
    if tctl_match:
        return float(tctl_match.group(1))

    core_temps = [float(v) for v in re.findall(r"temp\d+_input:\s*([\d.]+)", out)]
    if core_temps:
        return max(core_temps)

    raise RuntimeError("No temp readings found in `sensors -u` output")


# ---------------------------------------------------------------------------
# DIAGRAM RENDER VALIDATION — added post adr_0002 incident (31 Jul 2026).
# Architect's Mermaid C4Context output can be syntactically invalid (e.g. a
# malformed System_boundary() call) with nothing catching it before human
# review. This mirrors the finish_reason == "length" pattern already used
# in agents/*.py: fail loud at the source step instead of letting a bad
# output reach DBOS.recv() unvalidated. Split into a plain function +
# @DBOS.step() wrapper for the same reason as run_thermal_guard(): testable
# with an injected http client, no DBOS launch required.
# ---------------------------------------------------------------------------

class DiagramRenderError(RuntimeError):
    pass


def render_mermaid_diagram(
    context_diagram: str,
    mermaid_ink_url: str | None = None,
    timeout_s: float = 10.0,
    http_get=None,
) -> dict:
    """POSTs (via GET, per mermaid.ink's own URL-safe-base64 endpoint
    convention — see KICKOFF_CHECKLIST.md §3) the diagram source and
    raises DiagramRenderError if the response isn't image bytes. A
    lexer/parse error comes back as a 200 with a text/plain body
    ('Lexical error on line N...'), not a 4xx/5xx, so status code alone
    doesn't catch this — content-type must be checked explicitly."""
    import base64
    import httpx

    url = mermaid_ink_url or _require_env("MERMAID_INK_URL")
    b64 = base64.urlsafe_b64encode(context_diagram.encode()).decode().rstrip("=")

    getter = http_get or httpx.get
    resp = getter(f"{url}/img/{b64}", timeout=timeout_s)

    content_type = resp.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise DiagramRenderError(
            f"Architect: Mermaid diagram failed to render via mermaid.ink "
            f"(content-type={content_type!r}, status={resp.status_code}). "
            f"Response: {resp.text[:300]!r}"
        )
    return {"ok": True, "content_type": content_type, "bytes": len(resp.content)}


@DBOS.step(retries_allowed=False)
def validate_diagram_renders_step(context_diagram: str) -> dict:
    return render_mermaid_diagram(context_diagram)


@DBOS.step()
def capture_timestamp_step() -> float:
    """Reading the clock is non-deterministic, so it gets its own step —
    same reasoning as thermal_guard_step below. Used to split the
    workflow's total duration into compute time (Researcher..Judge) vs.
    approval wait time (DBOS.recv()), since §8's 5-minute target is
    against compute time only. Returns a Unix epoch float rather than a
    datetime so downstream arithmetic doesn't need re-parsing."""
    return time.time()


@DBOS.step(retries_allowed=False)
def thermal_guard_step(label: str = "") -> dict:
    """Thin @DBOS.step() wrapper. All actual logic lives in run_thermal_guard()
    below so it can be pytest-tested directly, without DBOS launched and
    without real sleeps — same split as agents/*.py vs. the *_step()
    wrappers in this file."""
    return run_thermal_guard(
        label=label,
        max_c=_require_env_float("THERMAL_MAX_C"),
        poll_s=_require_env_float("THERMAL_POLL_S"),
        timeout_s=_require_env_float("THERMAL_TIMEOUT_S"),
        cooldown_s=_require_env_float("THERMAL_COOLDOWN_S"),
    )


def run_thermal_guard(
    label: str,
    max_c: float,
    poll_s: float,
    timeout_s: float,
    cooldown_s: float,
    temp_reader=_read_cpu_package_temp_c,
    sleep_fn=time.sleep,
) -> dict:
    """Polls temp_reader() until below max_c or timeout_s elapses, then
    sleeps cooldown_s unconditionally. temp_reader/sleep_fn are injectable
    so tests can simulate temperature sequences and skip real waiting."""
    elapsed = 0.0
    temp = temp_reader()
    while temp >= max_c:
        if elapsed >= timeout_s:
            raise ThermalGuardTimeoutError(
                f"[thermal_guard{f' ({label})' if label else ''}] still at "
                f"{temp}\u00b0C after {timeout_s}s wait (limit {max_c}\u00b0C). "
                f"Aborting rather than proceeding into an unknown thermal state."
            )
        DBOS.logger.warning(
            f"[thermal_guard{f' ({label})' if label else ''}] {temp}\u00b0C >= "
            f"{max_c}\u00b0C, waiting {poll_s}s..."
        )
        sleep_fn(poll_s)
        elapsed += poll_s
        temp = temp_reader()

    # Unconditional rest after every step, independent of the reading above —
    # see module note: a reactive-only guard may not catch an EC-level cutoff.
    sleep_fn(cooldown_s)
    return {"ok": True, "temp_c": temp, "waited_s": elapsed, "cooldown_s": cooldown_s}


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


@DBOS.step(retries_allowed=True, max_attempts=3)
def architect_step(spec: dict, pricing_context: dict) -> dict:
    return call_architect(spec, pricing_context).model_dump(mode="json")


@DBOS.step(retries_allowed=True, max_attempts=3)
async def scribe_step(
    prior_spec: dict | None,
    current_spec: dict,
    blackboard_context: str,
) -> dict:
    prior = ArchitectureSpec.model_validate(prior_spec) if prior_spec else None
    current = ArchitectureSpec.model_validate(current_spec)
    adr_output = await run_scribe(prior, current, blackboard_context)
    return adr_output.model_dump(mode="json")


@DBOS.step(retries_allowed=True, max_attempts=3)
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


@DBOS.step()
def db_bootstrap_step(spec: dict, spec_version: int) -> None:
    spec_version_id = ensure_spec_version_row(spec_version, spec)
    ensure_pipeline_run_row(DBOS.workflow_id, spec_version_id)


@DBOS.step()
def persist_adr_step(adr_output: dict, spec_version: int, supersedes: list[str] | None) -> dict:
    record = persist_adr(
        ADROutput.model_validate(adr_output),
        spec_version=spec_version,
        supersedes=supersedes,
    )
    return record.model_dump(mode="json")


@DBOS.step()
def persist_approval_step(
    workflow_id: str,
    architect_output: dict,
    adr_record: dict,
    judge_output: dict,
) -> int:
    artifact_id = insert_artifact_row(
        workflow_id,
        ArchitectOutput.model_validate(architect_output),
        ADRRecord.model_validate(adr_record),
        JudgeOutput.model_validate(judge_output),
    )
    update_pipeline_run_status(workflow_id, "approved")
    return artifact_id


@DBOS.step()
def persist_rejection_step(workflow_id: str, revision_notes: str) -> int:
    revision_id = insert_revision_cycle_row(workflow_id, revision_notes)
    update_pipeline_run_status(workflow_id, "rejected")
    return revision_id

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
    spec_version = _require_spec_version(spec)
    compute_start_time = capture_timestamp_step()
    db_bootstrap_step(spec, spec_version)
    spec_text = json.dumps(spec, indent=2)

    DBOS.logger.info("[1/5] Researcher starting (Gemma)...")
    researcher_output = researcher_step(spec_text)
    DBOS.logger.info("[1/5] Researcher done.")
    thermal_guard_step("after Researcher")

    DBOS.logger.info("[2/5] Architect starting (Gemma)...")
    architect_output = architect_step(
        spec,
        researcher_output["pricing_context"],
    )
    DBOS.logger.info("[2/5] Architect done.")
    validate_diagram_renders_step(architect_output["context_diagram"])
    thermal_guard_step("after Architect")

    # --- model swap: Gemma -> LFM happens inside llama-server here ---
    DBOS.logger.info("Model swap: Gemma -> LFM")

    scribe_blackboard = _summarize_for_scribe(
        ResearcherOutput.model_validate(researcher_output),
        ArchitectOutput.model_validate(architect_output),
    )
    DBOS.logger.info("[3/5] Scribe starting (LFM)...")
    adr_output = await scribe_step(prior_spec, spec, scribe_blackboard)
    DBOS.logger.info("[3/5] Scribe done.")
    thermal_guard_step("after Scribe")

    critic_blackboard = _summarize_for_critic(spec, ResearcherOutput.model_validate(researcher_output))
    DBOS.logger.info("[4/5] Critic starting (LFM)...")
    critic_output = await critic_step(architect_output, adr_output, critic_blackboard)
    DBOS.logger.info("[4/5] Critic done.")
    thermal_guard_step("after Critic")

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
    DBOS.logger.info("[5/5] Judge done. Awaiting human review.")
    compute_end_time = capture_timestamp_step()

    # ------------------------------------------------------------------
    # Human review gate (§7). Spec §3 Availability: "Human review pause:
    # unbounded, pipeline blocks on DBOS.recv() awaiting approval signal."
    # recv() has a bounded per-call timeout (docs default 60s), so we loop
    # rather than pass one huge timeout. Each recv() call is its own
    # durable step, so looping is safe across restarts/recovery — a
    # message sent while no recv() is in-flight is queued and picked up
    # by the next call, nothing is lost.
    #
    # Contract with pipeline/send_approval.py:
    #   topic:   "review_decision"
    #   message: {"approved": bool, "notes": str | None}
    # ------------------------------------------------------------------
    REVIEW_TOPIC = "review_decision"
    REVIEW_POLL_TIMEOUT_S = 3600

    decision = await DBOS.recv_async(topic=REVIEW_TOPIC, timeout_seconds=REVIEW_POLL_TIMEOUT_S)
    while decision is None:
        DBOS.logger.info(
            f"No review decision after {REVIEW_POLL_TIMEOUT_S}s — still waiting "
            f"(run pipeline/send_approval.py {DBOS.workflow_id} to unblock)."
        )
        decision = await DBOS.recv_async(topic=REVIEW_TOPIC, timeout_seconds=REVIEW_POLL_TIMEOUT_S)

    approval_end_time = capture_timestamp_step()
    approved = decision["approved"]
    notes = decision.get("notes")
    DBOS.logger.info(f"Review decision received: approved={approved} notes={notes!r}")

    # compute_duration_s is the number the §8 5-minute target is actually
    # measured against — Researcher through Judge, excluding human wait.
    # approval_wait_s is tracked separately so it's never silently folded
    # back into "duration" by a future refactor.
    compute_duration_s = compute_end_time - compute_start_time
    approval_wait_s = approval_end_time - compute_end_time
    DBOS.logger.info(
        f"Timing: compute_duration_s={compute_duration_s:.1f} "
        f"approval_wait_s={approval_wait_s:.1f}"
    )

    if approved:
        adr_record = persist_adr_step(adr_output, spec_version, decision.get("supersedes"))
        artifact_id = persist_approval_step(
            DBOS.workflow_id, architect_output, adr_record, judge_output
        )
        DBOS.logger.info(f"ADR written: {adr_record['adr_id']}, artifact_id={artifact_id}")

    else:
        if not notes:
            raise ValueError(
                "Rejection requires revision_notes — enforced client-side by "
                "send_approval.py's --reject, but re-checked here since this "
                "workflow could in principle be sent a message another way."
            )
        revision_id = persist_rejection_step(DBOS.workflow_id, notes)
        DBOS.logger.info(f"revision_cycles row written: id={revision_id}")

    DBOS.logger.info("Pipeline complete.")

    return {
        "researcher": researcher_output,
        "architect": architect_output,
        "adr": adr_output,
        "critic": critic_output,
        "judge": judge_output,
        "review": {"approved": approved, "notes": notes},
        "timing": {
            "compute_duration_s": compute_duration_s,
            "approval_wait_s": approval_wait_s,
        },
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run(spec_path: str, prior_spec_path: str | None, adr_count: int) -> None:
    config: DBOSConfig = {
        "name": "edge-agent-swarm",
        "system_database_url": _require_env("DBOS_SYSTEM_DATABASE_URL"),
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

    start_time = datetime.now(timezone.utc)
    handle = await DBOS.start_workflow_async(
        architecture_review_workflow, spec, prior_spec, adr_count
    )
    print(f"workflow_id: {handle.workflow_id} start={start_time.isoformat()}")

    result = await handle.get_result()
    end_time = datetime.now(timezone.utc)
    duration_s = (end_time - start_time).total_seconds()
    timing = result.get("timing", {})
    compute_duration_s = timing.get("compute_duration_s")
    approval_wait_s = timing.get("approval_wait_s")
    print(
        f"workflow_id: {handle.workflow_id} end={end_time.isoformat()} "
        f"duration_s={duration_s:.1f} "
        f"compute_duration_s={compute_duration_s:.1f} "
        f"approval_wait_s={approval_wait_s:.1f}"
    )
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