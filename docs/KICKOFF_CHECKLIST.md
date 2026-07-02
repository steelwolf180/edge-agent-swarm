# Agent Swarm at the Edge — Kickoff Checklist

> Start here. This sits *before* `GETTING_STARTED.md` — it closes the gap between
> the v0.6 spec (written against build 8783 / Ubuntu 24 LTS) and your current
> validated environment (build 9595 / Ubuntu 26.04), then hands off into the
> original infra → agents → pipeline sequence.

---

## 0. Pre-Flight: Reconcile Spec with Current Environment

- [ ] Update spec/doc references from llama.cpp build 8783 → **build 9595**
- [ ] Update OS references from Ubuntu 24 LTS → **Ubuntu 26.04 "resolute"**
- [ ] Re-confirm `--jinja` flag still triggers Gemma tool calling correctly on build 9595
- [ ] Re-confirm router mode / `--models-preset` / `--models-max 1` behave identically on 9595
- [ ] Re-validate token rates on 9595 (previous confirmed: Gemma ~14.8 tok/s, LFM ~115 tok/s) — builds can shift perf
- [ ] Re-validate model swap latency (previous confirmed: ~20–30s) — this drives your 2-swap pipeline budget
- [ ] Confirm Proton VPN reinstalled (removed during Ubuntu upgrade) if any dev step needs outbound access
- [ ] Confirm all four Docker containers (mermaid.ink, Infracost, Postgres, Phoenix) are still healthy with `--restart unless-stopped` post-upgrade

---

## 1. GitLab

- [x] Confirm/create `steelwolf180/edge-agent-swarm` (private)
- [x] Register local self-hosted runner on ZenBook
- [ ] Add `.gitignore` — exclude `~/models/`, `.env`, `__pycache__`, venv dirs, `*.gguf`
- [ ] Commit `GETTING_STARTED.md` and this checklist as first commits
- [ ] Confirm Personal Access Token (with `api` or `write_repository` scope) is set up for HTTPS push

---

## 2. Repo Structure

- [x] Create folder scaffold: `agents/`, `artifacts/v1/`, `eval/`, `schemas/`, `pipeline/`, `tests/`
- [ ] Add `eval/rubric_v1.json` — versioned Judge thresholds for the five metrics (`spof_count`, `redundancy_ratio`, `cost_per_component`, `integration_coverage`, `adrs_per_diff`)
- [ ] Add `README.md` with architecture overview + local setup instructions

---

## 3. Infrastructure (Docker)

- [ ] Confirm `mermaid.ink` container reachable at `localhost:3001`
- [ ] Smoke test: POST base64-encoded Mermaid source → confirm image returned
- [ ] Confirm Infracost GraphQL API reachable at `localhost:4000`

---

## 4. Database

- [ ] Write + run migration for application schema (separate from DBOS system tables):
  - `spec_versions` — versioned input specs
  - `artifacts` — approved outputs per run
  - `revision_cycles` — rejection records
  - `pipeline_runs` — maps `workflow_id` to spec_version and status
- [ ] Confirm DBOS connects to PostgreSQL 18.4 at `localhost:5432` and system tables init cleanly

---

## 5. llama-server

- [ ] Write `models.ini` preset (Gemma + LFM, `--threads 4`, `jinja = true` for Gemma)
- [ ] Start server in router mode: `--models-preset models.ini --models-max 1 --port 8080`
- [ ] Smoke test Gemma via curl — confirm response + tok/s
- [ ] Smoke test LFM via curl — confirm response + tok/s
- [ ] Confirm LRU model swap triggers cleanly

---

## 6. Agents — Build & Validate One at a Time, in Isolation

Don't wire the pipeline shell until each agent works standalone against its schema.

**Researcher (Gemma)** — do this one first; it's the only agent with a live external tool call
- [ ] Infracost GraphQL stub call validates
- [ ] Output parses into `ResearcherOutput` Pydantic model
- [ ] Pricing context written to blackboard via `DBOS.set_event(...)`

**Architect (Gemma)**
- [ ] C4Context output starts with `C4Context`
- [ ] Diagram renders correctly in mermaid.ink
- [ ] `ArchitectOutput` validates (`context_diagram`, `diagram_source`, `docs`, `components`)

**Scribe (LFM)**
- [ ] `deepdiff` on `model_dump()` produces diff input
- [ ] `ADROutput` validates (`context`, `decision`, `consequences`, `diff_summary`, `affected_diagrams`)
- [ ] `affected_diagrams` restricted to `'context'` only for MVP

**Critic (LFM)**
- [ ] `CriticOutput` validates (`gaps`, `spofs`, `missing_integrations`)
- [ ] Gap list non-empty against a deliberately weak test spec

**Judge (Gemma)**
- [ ] Calculator tool fires, returns deterministic scores
- [ ] All five metrics present in `JudgeOutput.scores`
- [ ] Reads thresholds from `eval/rubric_v1.json` at runtime

---

## 7. DBOS Pipeline

- [ ] Wrap each agent call as `@DBOS.step()`
- [ ] Wrap each thermal guard check as its own `@DBOS.step()` (65°C / 5s poll / 120s timeout)
- [ ] Print `workflow_id` to terminal on pipeline start
- [ ] Write `pipeline/send_approval.py <workflow_id> [--reject "notes"]`
- [ ] Confirm `DBOS.recv()` blocks correctly at human review
- [ ] Confirm `DBOS.send()` from CLI unblocks the workflow
- [ ] Approval path → outputs written to PostgreSQL + `artifacts/v<n>/`
- [ ] Rejection path → `revision_notes` written to blackboard, `revision_cycles` row inserted

---

## 8. End-to-End Run

- [ ] Submit one full spec through all 5 agents
- [ ] Confirm swap sequence: Gemma → LFM → Gemma (2 swaps total)
- [ ] Confirm full run completes within 5-minute target, `--threads 4` powersave
- [ ] Run sustained thermal check across the *whole* pipeline (not just per-agent — this hasn't been validated end-to-end yet)
- [ ] Approve via CLI → confirm artifacts written
- [ ] Run a second spec with a deliberate change → confirm ADR triggered by diff

---

## 9. Paper Trail

- [ ] Commit `artifacts/v1/*.md` to GitLab
- [ ] Confirm GitLab renders `context_diagram.md` (C4Context block) inline
- [ ] Verify `revision_cycles` row written on rejection
- [ ] Verify `judge_scores.json` persisted per run with `spec_version` reference

---

## Observability Checks (run throughout, not just at the end)

- [ ] Arize Phoenix reachable at `localhost:6006`
- [ ] Per-agent OTel spans visible in Phoenix UI (system prompt, tool calls, latency, model)
- [ ] `lm-sensors` readable from Python thermal guard step

---

*Sequence is intentional: pre-flight reconciliation → infra → agents in isolation → pipeline wiring → end-to-end → paper trail. Don't skip ahead to pipeline wiring before each agent is independently validated.*
