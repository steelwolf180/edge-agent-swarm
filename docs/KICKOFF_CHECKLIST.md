# Agent Swarm at the Edge — Kickoff Checklist

> Start here. This sits *before* `GETTING_STARTED.md` — it closes the gap between
> the v0.6 spec (written against build 8783 / Ubuntu 24 LTS) and your current
> validated environment (build 9595 / Ubuntu 26.04), then hands off into the
> original infra → agents → pipeline sequence.

---

## 0. Pre-Flight: Reconcile Spec with Current Environment

- [x] Update spec/doc references from llama.cpp build 8783 → **build 9595**
- [x] Update OS references from Ubuntu 24 LTS → **Ubuntu 26.04 "resolute"**
- [x] Re-confirm `--jinja` flag still triggers Gemma tool calling correctly on build 9595
- [x] Re-confirm router mode / `--models-preset` / `--models-max 1` behave identically on 9595
- [x] Re-validate token rates on 9595 (previous confirmed: Gemma ~14.8 tok/s, LFM ~115 tok/s) — builds can shift perf
- [x] Re-validate model swap latency (previous confirmed: ~20–30s) — this drives your 2-swap pipeline budget
- [x] Confirm Proton VPN reinstalled — **not required.** Pipeline is local-only (`localhost` for mermaid.ink, Infracost, Postgres, Phoenix); only outbound traffic is `git push` (HTTPS) and `apt`, neither needs a VPN. Skipped as non-blocking.
- [x] Confirm Docker containers (mermaid.ink, Infracost, Phoenix) healthy with `--restart unless-stopped` post-upgrade — **note: Postgres is a native `apt install`, not a Docker container** (per spec §4 Existing Systems); only 3 services actually run in Docker. `pg_isready -h localhost -p 5432` confirms it independently.
- [x] Confirm Docker Desktop itself is set to start on login (`systemctl --user enable docker-desktop`) — `--restart unless-stopped` only governs containers once the daemon is up; it won't launch Docker Desktop after a crash or logout.

---

## 1. GitLab

- [x] Confirm/create `steelwolf180/edge-agent-swarm` (private)
- [x] Register local self-hosted runner on ZenBook
- [x] Add `.gitignore` — exclude `~/models/`, `.env`, `__pycache__`, venv dirs, `*.gguf`
- [x] Commit `KICKOFF_CHECKLIST.md` as first commit
- [x] Confirm Personal Access Token (with `api` or `write_repository` scope) is set up for HTTPS push

---

## 2. Repo Structure

- [x] Create folder scaffold: `agents/`, `artifacts/v1/`, `eval/`, `schemas/`, `pipeline/`, `tests/`
- [ ] Add `eval/rubric_v1.json` — versioned Judge thresholds for the five metrics (`spof_count`, `redundancy_ratio`, `cost_per_component`, `integration_coverage`, `adrs_per_diff`)
- [ ] Add `README.md` with architecture overview + local setup instructions

---

## 3. Infrastructure (Docker)

- [x] Confirm `mermaid.ink` container reachable at `localhost:3001`
- [x] Smoke test: GET URL-safe-base64-encoded Mermaid source → confirm image returned (see corrected command below — endpoint is **GET**, not POST)
- [x] Confirm Infracost GraphQL API reachable at `localhost:4000` (bare GET returns `400` — expected; needs a POST with a GraphQL query body to return `200`)

**mermaid.ink known issue (Ubuntu 26.04) — Chromium sandbox failure:**
Ubuntu 23.10+ restricts unprivileged user namespaces via AppArmor by default, which breaks Puppeteer/Chromium's sandbox inside the container (`No usable sandbox!`, zygote crash, restart loop). Fix: add `--cap-add=SYS_ADMIN` to the container's run command. Scoped to this container only — does not weaken host-wide AppArmor.

Corrected run command (also fixes a quoting bug in the original `NODE_OPTIONS` value that caused silent failures):
```bash
docker run -d --restart unless-stopped \
  --cap-add=SYS_ADMIN \
  -e 'NODE_OPTIONS=--max-http-header-size=102400000' \
  -p 3001:3000 \
  --name mermaid-ink \
  ghcr.io/jihchi/mermaid.ink
```

Corrected smoke test (endpoint expects **GET** + **URL-safe** base64, not POST + standard base64):
```bash
echo -n 'C4Context
    Person(user, "User")
    System(sys, "System")' | base64 -w0 | tr '+/' '-_' > /tmp/test.b64

curl -s http://localhost:3001/img/$(cat /tmp/test.b64) -o /tmp/test.png
file /tmp/test.png   # should report JPEG/PNG image data, not "ASCII text"
```

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
