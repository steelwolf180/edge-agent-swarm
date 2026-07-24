# Agent Swarm at the Edge

Spec-driven, multi-agent architecture review pipeline that runs entirely on local CPU-only hardware. No cloud, no API keys, no data leaving the device.

It automates the C4 diagram + Architecture Decision Record (ADR) discipline for solo developers, small teams, and solution architects, closing the gap between technical and business stakeholders. It is not a code generation tool.

> Full spec: `agent_swarm_architecture_spec.docx` (v0.9)
> Public overview: `agent_swarm_at_the_edge_public.docx`
> Environment setup: `KICKOFF_CHECKLIST.md` — start there before this README if you're bootstrapping from scratch.

---

## Architecture Overview

Five agents run sequentially over a shared DBOS blackboard, with a human approval gate before anything persists:

```
                                              ┌─────────────────────────────┐
                                              │  PostgreSQL: prior ADR       │
                                              │  (most recent approved)      │
                                              └───────────────┬───────────────┘
                                                              │ read
                                                              ▼
Researcher (Gemma) → Architect (Gemma) → [swap] → Scribe (LFM) → Critic (LFM) → [swap] → Judge (Gemma)
                                                                                              ↓
                                                                                     Human review (CLI)
                                                                                     approve / reject
                                                                                              ↓
                                                                              PostgreSQL + artifacts/v<n>/
```

| Agent | Model | Role | Tools |
|---|---|---|---|
| Researcher | Gemma 4 E4B QAT | Enriches blackboard with context + cloud pricing | Infracost GraphQL |
| Architect | Gemma 4 E4B QAT | Generates C4 L1 System Context diagram (Mermaid); reads blackboard + most recent approved ADR from the artifact store, so revisions are informed by why prior decisions were made, not just the raw spec diff | none |
| Scribe | LFM2.5-VL-1.6B | Detects spec diff, drafts ADR | none |
| Critic | LFM2.5-VL-1.6B | Devil's advocate: gaps, SPOFs, missing integrations | none |
| Judge | Gemma 4 E4B QAT | Scores output against 5 metrics | Calculator |

**Why sequential:** two independent constraints, not one. Hardware forces one-model-at-a-time execution (single CPU inference engine, RAM ceiling, thermal limits) via LRU hot-swap — 2 swaps per run, Gemma → LFM → Gemma. Separately, each agent consumes the prior agent's output, so the ordering is a genuine data dependency that would hold even on beefier hardware.

**Note on the name:** this is technically a sequential multi-agent pipeline with a shared blackboard, not a true swarm (concurrent, decentralized, emergent). "Agent Swarm" is retained as the project name for brand equity, but the precise term should lead in specs and talk abstracts.

**Scope (MVP):** L1 System Context diagram only. L2 Container is a v2 target.

**Trust model:** on-device SLMs mean architecture specs never leave the machine — viable for air-gapped, regulated, or privacy-sensitive contexts where cloud LLMs are a non-starter.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Inference | llama.cpp build 9595, `llama-server` router mode, `--models-max 1` |
| Models | Gemma 4 E4B QAT (`gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf`), LFM2.5-VL-1.6B Q4_0 |
| Orchestration | DBOS (`dbos-transact-py`) |
| State | PostgreSQL 18.4 (native, not Docker) |
| Observability | Arize Phoenix (OTel/OpenInference) |
| Diagram rendering | mermaid.ink (Docker) |
| Pricing API | Infracost GraphQL stub (Docker) |
| Validation | Pydantic v2 |
| Diff engine | deepdiff |
| Language | Python 3.11 (conda env `swarm`) |

## Hardware

ASUS ZenBook UX325EA — Intel i5-1135G7, 16GB RAM (~7–8GB available for workloads), no discrete GPU, Ubuntu 26.04 LTS "resolute".

---

## Local Setup

Full step-by-step validation lives in `KICKOFF_CHECKLIST.md`. Summary:

1. **Clone + env**
   ```bash
   git clone https://gitlab.com/steelwolf180/edge-agent-swarm.git
   cd edge-agent-swarm
   conda activate swarm  # Python 3.11
   ```

2. **Models** — place GGUF weights under `~/models/` (gitignored):
   - `~/models/gemma-4-e4b-qat/weights/`
   - `~/models/lfm2.5-vl-1.6b/`

3. **llama-server**
   ```bash
   llama-server --models-preset models.ini --models-max 1 --port 8080
   ```
   `models.ini` sets `jinja = true` for Gemma (required for tool calling) and `--threads 4` for both models.

### Start the inference server

```bash
./scripts/start_llama_router.sh
```
Starts llama-server in router mode on port 8080 using `models.ini`. Logs to `logs/llama-router.log`.

### Stop the server
```bash
./scripts/stop_llama_router.sh
```

### Smoke test a model

```bash
./tests/smoke/test_llama.sh gemma   # or: lfm
```

Sends a single chat completion request and reports:
- raw response
- whether `reasoning_content` is present (should be absent — confirms `reasoning = off` took effect)
- timing/tok-per-second data

Run this after starting the server and after any `models.ini` change, before building on top of either model.

4. **Docker services** (all `--restart unless-stopped`; confirm Docker Engine is enabled on boot first: `systemctl enable docker` — v0.9 migrated from Docker Desktop to native Docker Engine, eliminating its Linux VM overhead)
   ```bash
   # mermaid.ink — requires --cap-add=SYS_ADMIN on Ubuntu 26.04 (AppArmor sandbox fix)
   docker run -d --restart unless-stopped --cap-add=SYS_ADMIN \
     -e 'NODE_OPTIONS=--max-http-header-size=102400000' \
     -p 3001:3000 --name mermaid-ink ghcr.io/jihchi/mermaid.ink

   # Infracost GraphQL stub → localhost:4000
   # Arize Phoenix → localhost:6006 / 4317
   ```
   Services are on-demand: only spin up mermaid.ink / Infracost while testing Architect / Researcher.

5. **PostgreSQL** — native `apt install`, not Docker. Confirm with `pg_isready -h localhost -p 5432`.

6. **DB migration** — application schema (`spec_versions`, `artifacts`, `revision_cycles`, `pipeline_runs`) created and validated on PostgreSQL 18 (`schemas/001_app_tables.sql`), plus `artifacts.adr_id` added in `schemas/002_add_adr_id.sql` (§7). Apply both with:
   ```bash
   python pipeline/run_migration.py              # real DB (DBOS_SYSTEM_DATABASE_URL)
   python pipeline/run_migration.py --target testing   # test DB (TESTING_DATABASE_URL)
   ```

7. **Run the pipeline**
   ```bash
   python pipeline/run.py --spec path/to/spec.json # prints workflow_id
   python pipeline/send_approval.py <workflow_id>                                   # approve
   python pipeline/send_approval.py <workflow_id> --supersedes adr_0003,adr_0004    # approve, superseding prior ADR(s)
   python pipeline/send_approval.py <workflow_id> --reject "notes"                  # reject
   ```
   On approval, the pipeline writes `artifacts/v<n>/adr_<NNNN>.md` and an `artifacts` row in PostgreSQL. On rejection, it writes a `revision_cycles` row with the required notes. **Rejection does not automatically re-run the pipeline from Critic** — re-submit a revised spec via `pipeline/run.py` to go through the pipeline again. Automatic re-run from Critic on rejection is a parked v2 improvement, not yet implemented.

8. **Verify**
   - Phoenix UI at `localhost:6006` — per-agent spans (prompt, tool calls, latency, model)
   - `artifacts/v<n>/` — Markdown paper trail
   - PostgreSQL — versioned record on approval

---

## Testing

```bash
# Fast, isolated — no live services required beyond Postgres
pytest tests/smoke/

# Full round-trip against TESTING_DATABASE_URL (spec_versions/pipeline_runs/artifacts/revision_cycles + real ADR file writes)
pytest tests/integration/test_persistence.py

# Full 5-agent pipeline, real llama-server calls, both model swaps — slow (~5-6 min per run)
RUN_DBOS_TESTS=1 pytest -v -m integration tests/integration/test_pipeline_approval.py
```

All tests run against `TESTING_DATABASE_URL`, never the real DB — enforced by an autouse fixture in `tests/conftest.py`.

---

## Repo Structure

```
agents/             agent implementations (researcher, architect, scribe, critic, judge)
artifacts/v1/        approved run outputs (Mermaid diagrams, ADRs)
eval/                eval/rubric_v1.json — versioned Judge thresholds
schemas/             Pydantic models, 001_app_tables.sql, 002_add_adr_id.sql
pipeline/            run.py (DBOS workflow), persistence.py, send_approval.py, run_migration.py
scripts/             start_llama_router.sh, stop_llama_router.sh
tests/
  conftest.py         redirects all tests to TESTING_DATABASE_URL
  smoke/              per-component liveness checks (test_llama.sh, test_persistence.py, ...)
  integration/         cross-component correctness (test_persistence.py, test_pipeline_approval.py)
    fixtures/           minimal_spec.json
```

## Known Limitations

See spec §7 for the full list — output quality depends on input spec quality, Judge metrics are fixed-threshold with no system-type awareness, and the human-in-the-loop step depends on genuine engagement rather than rubber-stamping. Not a substitute for a senior architect on complex enterprise systems.

Rejection currently persists revision notes but does not automatically loop the pipeline back to Critic — re-submission is manual (see step 7 above). Parked as a v2 improvement.

## Status

v0.9. §6 agent validation and §7 DBOS pipeline wiring (including approval/rejection persistence) complete and validated — unit-level (`tests/integration/test_persistence.py`) and full-pipeline (`tests/integration/test_pipeline_approval.py`), both passing against real inference and real Postgres. Open item: §8 End-to-End Run (see `KICKOFF_CHECKLIST.md` §8) — sustained thermal validation across a full run is the primary remaining item.
