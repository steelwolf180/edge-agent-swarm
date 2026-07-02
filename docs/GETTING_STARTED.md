# Agent Swarm at the Edge — Getting Started Checklist

> **Project**: Agent Swarm at the Edge v0.6
> **Hardware**: ASUS ZenBook UX325EA · i5-1135G7 · 16GB RAM · Ubuntu 24 LTS · CPU-only
> **Stack**: Python 3.11 · DBOS · llama-server (build 9595) · PostgreSQL · Arize Phoenix · Docker Desktop

---

## 1. GitLab

- [ ] Create new GitLab project (private)
- [ ] Register local self-hosted runner on ZenBook
- [ ] Add `.gitignore` — exclude `~/models/`, `.env`, `__pycache__`, venv dirs, `*.gguf`

---

## 2. Repo Structure

- [ ] Create folder scaffold:
  ```
  agents/
  artifacts/
    v1/
  eval/
  schemas/
  pipeline/
  tests/
  ```
- [ ] Add `eval/rubric_v1.json` — Judge scoring thresholds for five metrics (`spof_count`, `redundancy_ratio`, `cost_per_component`, `integration_coverage`, `adrs_per_diff`)
- [ ] Add `README.md` with architecture overview and local setup instructions

---

## 3. Infrastructure (Docker)

- [ ] Confirm `mermaid.ink` container runs alongside Infracost at a free port (e.g. `3001`)
  ```bash
  docker run --rm -it \
    -e 'NODE_OPTIONS="--max-http-header-size=102400000"' \
    -p 3001:3000 \
    ghcr.io/jihchi/mermaid.ink
  ```
- [ ] Smoke test: POST base64-encoded Mermaid source → confirm image returned locally
- [ ] Confirm Infracost GraphQL API still reachable at `localhost:4000`

---

## 4. Database

- [ ] Write and run migration for application schema (separate from DBOS system tables):
  - `spec_versions` — versioned input specs (spec JSON, version int, created_at)
  - `artifacts` — approved outputs per run (diagram_source, adr_markdown, critic_output, judge_scores, approved_at, spec_version_id)
  - `revision_cycles` — rejection records (revision_notes, workflow_id, created_at)
  - `pipeline_runs` — maps workflow_id to spec_version and run status
- [ ] Confirm DBOS connects to PostgreSQL at `localhost:5432` and system tables initialise cleanly

---

## 5. llama-server

- [ ] Write `models.ini` preset for Gemma + LFM:
  ```ini
  [gemma]
  model = /home/<user>/models/gemma-4-e4b-qat/weights/UD-Q4_K_XL.gguf
  ctx-size = 4096
  threads = 4
  jinja = true

  [lfm]
  model = /home/<user>/models/lfm2.5-vl-1.6b/LFM2.5-VL-1.6B-Q4_0.gguf
  ctx-size = 4096
  threads = 4
  ```
- [ ] Start server in router mode:
  ```bash
  ~/llama.cpp/build/bin/llama-server \
    --models-preset models.ini \
    --models-max 1 \
    --port 8080
  ```
- [ ] Smoke test Gemma: `curl` with `"model": "gemma"` — confirm response and token rate ~14.8 tok/s
- [ ] Smoke test LFM: `curl` with `"model": "lfm"` — confirm response and token rate ~115 tok/s
- [ ] Confirm model swap triggers cleanly (LRU eviction, ~20–30s per swap)

---

## 6. Agents (one at a time, in order)

### Researcher (Gemma)
- [ ] Tool call to Infracost GraphQL stub validates
- [ ] Output parses into `ResearcherOutput` Pydantic model
- [ ] Pricing context written to DBOS blackboard via `DBOS.set_event("researcher_output", ...)`

### Architect (Gemma)
- [ ] C4Context string output produced — validate starts with `C4Context`
- [ ] Diagram renders correctly in `mermaid.ink` at `localhost:3001`
- [ ] `ArchitectOutput` Pydantic model validates (`context_diagram`, `diagram_source`, `docs`, `components`)

### Scribe (LFM)
- [ ] `deepdiff` on `model_dump()` produces diff input for Scribe context
- [ ] `ADROutput` Pydantic model validates (`context`, `decision`, `consequences`, `diff_summary`, `affected_diagrams`)
- [ ] `affected_diagrams` enforced to only contain `'context'` for MVP (no `'container'`)

### Critic (LFM)
- [ ] `CriticOutput` Pydantic model validates (`gaps`, `spofs`, `missing_integrations`)
- [ ] Gap list non-empty when tested against a deliberately weak spec

### Judge (Gemma)
- [ ] Calculator tool fires and returns deterministic scores
- [ ] All five metrics present in `JudgeOutput.scores`
- [ ] `JudgeOutput` reads scoring thresholds from `eval/rubric_v1.json` at runtime

---

## 7. DBOS Pipeline

- [ ] Wrap each agent call as a `@DBOS.step()`
- [ ] Wrap each thermal guard check as its own `@DBOS.step()` (65°C / 5s poll / 120s timeout)
- [ ] Print `workflow_id` to terminal immediately after pipeline starts
- [ ] Write `pipeline/send_approval.py <workflow_id> [--reject "notes"]` CLI script
- [ ] Confirm `DBOS.recv()` blocks correctly at human review step
- [ ] Confirm `DBOS.send()` from CLI unblocks the workflow
- [ ] On approval → all outputs written to PostgreSQL + `artifacts/v<n>/` on disk
- [ ] On rejection → `revision_notes` written to blackboard, `revision_cycles` row inserted

---

## 8. End-to-End Run

- [ ] Submit one full spec through the pipeline (all 5 agents)
- [ ] Confirm model swap sequence: Gemma → LFM → Gemma (2 swaps total)
- [ ] Confirm pipeline completes within 5-minute target on CPU-only at `--threads 4` powersave
- [ ] Approve output via CLI → confirm artifacts written
- [ ] Run a second spec with a deliberate change → confirm ADR triggered by diff

---

## 9. Paper Trail

- [ ] Commit `artifacts/v1/*.md` to GitLab repo
- [ ] Confirm GitLab renders `context_diagram.md` (C4Context block) inline in the UI
- [ ] Verify `revision_cycles` row written on rejection with `revision_notes`
- [ ] Verify `judge_scores.json` persisted per run with `spec_version` reference for score history

---

## Observability Checks

- [ ] Arize Phoenix running at `localhost:6006`
- [ ] Per-agent OTel spans visible in Phoenix UI (system prompt, tool calls, latency, model used)
- [ ] `lm-sensors` readable from Python thermal guard step

---

*Sequence is intentional — each section unblocks the next. Infra first, agents in isolation, then pipeline wiring.*
