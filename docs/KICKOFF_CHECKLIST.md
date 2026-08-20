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
- [x] Re-validate model swap latency — measured ~13s total for a two-swap cycle under low-contention conditions (v0.9); prior ~20–30s figure retained only as a conservative planning ceiling, not the current measured value
- [x] Confirm Proton VPN reinstalled — **not required.** Pipeline is local-only (`localhost` for mermaid.ink, Infracost, Postgres, Phoenix); only outbound traffic is `git push` (HTTPS) and `apt`, neither needs a VPN. Skipped as non-blocking.
- [x] Confirm Docker containers (mermaid.ink, Infracost, Phoenix) healthy with `--restart unless-stopped` post-upgrade — **note: Postgres is a native `apt install`, not a Docker container** (per spec §4 Existing Systems); only 3 services actually run in Docker. `pg_isready -h localhost -p 5432` confirms it independently.
- [x] Confirm Docker Engine is enabled on boot (`systemctl enable docker`) — v0.9 migration from Docker Desktop to native Docker Engine (docker-ce, docker-ce-cli, containerd.io); `--restart unless-stopped` only governs containers once the daemon is up, it won't start the daemon itself after a reboot.
- [x] Swap file /swapfile2 added and persisted in /etc/fstab — originally sized to absorb a ~3.4–3.7GB Gemma→LFM handoff transient; v0.9 post-Docker Engine-migration testing shows swap usage stayed flat at 764MB (pre-existing, unrelated to model swap) across a full two-swap cycle, no growth, no OOM. Docker Desktop VM overhead, not the model swap, was the leading suspect. Swapfile kept as a safety margin.

---

## 1. GitLab

- [x] Confirm/create `steelwolf180/edge-agent-swarm` (private)
- [x] Register local self-hosted runner on ZenBook
- [x] Add `.gitignore` — exclude `~/models/`, `.env`, `__pycache__`, venv dirs, `*.gguf`
- [x] Commit `KICKOFF_CHECKLIST.md` as first commit
- [x] Confirm Personal Access Token (with `api` or `write_repository` scope) is set up for HTTPS push

---

## 2. Repo Structure

- [x] Create folder scaffold: `agents/`, `artifacts/v1/`, `eval/`, `schemas/`, `pipeline/`, `scripts/`, `tests/`
- [x] Add `eval/rubric_v1.json` — versioned Judge thresholds for the five metrics (`spof_count`, `redundancy_ratio`, `cost_per_component`, `integration_coverage`, `adrs_per_diff`)
- [x] Add `README.md` with architecture overview + local setup instructions

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

- [x] Write + run migration for application schema (separate from DBOS system tables):
  - `spec_versions` — versioned input specs
  - `artifacts` — approved outputs per run
  - `revision_cycles` — rejection records
  - `pipeline_runs` — maps `workflow_id` to spec_version and status
- [x] Confirm DBOS connects to PostgreSQL 18.4 at `localhost:5432` and system tables init cleanly

---

## 5. llama-server

- [x] Write `models.ini` preset (Gemma + LFM, `--threads 4`, `jinja = true` for Gemma)
- [x] Start server in router mode: `--models-preset models.ini --models-max 1 --port 8080`
- [x] Smoke test Gemma via curl — confirm response + tok/s
- [x] Smoke test LFM via curl — confirm response + tok/s
- [x] Confirm LRU model swap triggers cleanly

---

## 6. Agents — Build & Validate One at a Time, in Isolation

Don't wire the pipeline shell until each agent works standalone against its schema.

**Researcher (Gemma)** — do this one first; it's the only agent with a live external tool call
- [x] Infracost GraphQL stub call validates
- [x] Output parses into `ResearcherOutput` Pydantic model
- [x] Pricing context written to blackboard via `DBOS.set_event(...)` *(deferred: full round-trip pending §7 wiring)*

**Architect (Gemma)**
- [x] Reads prior accepted ADRs from `artifacts/v*/adr_*.md` (`ADRRecord`) and folds them into the prompt as `PRIOR_DECISIONS`; superseded/rejected ADRs correctly excluded
- [x] C4Context output starts with `C4Context`
- [x] Diagram renders correctly in mermaid.ink
- [x] `ArchitectOutput` validates (`context_diagram`, `diagram_source`, `docs`, `components`)

**Scribe (LFM)**
- [x] `deepdiff` on `model_dump()` produces diff input
- [x] `ADROutput` validates (`context`, `decision`, `consequences`, `diff_summary`, `affected_diagrams`)
- [x] `affected_diagrams` restricted to `'context'` only for MVP

**Critic (LFM)**
- [x] `CriticOutput` validates (`gaps`, `spofs`, `missing_integrations`)
- [x] Gap list non-empty against a deliberately weak test spec — required raising the LFM completion cap from the input-budget figure (700, spec §6) to 1024; 700 truncated mid-JSON on the variable-length gap list. `CRITIC_TOKEN_BUDGET` in `.env` controls this, no code change needed if it needs retuning later.

**Judge (Gemma)**
- [x] Calculator tool fires, returns deterministic scores
- [x] All five metrics present in `JudgeOutput.scores`
- [x] Reads thresholds from `eval/rubric_v1.json` at runtime

**Token budget hardening (post-§6, cross-agent)**
- [x] All four LLM-calling agents (Researcher, Architect, Scribe, Critic) now read
  their output token cap from a dedicated `.env` var (`RESEARCHER_TOKEN_BUDGET`,
  `ARCHITECT_TOKEN_BUDGET`, `SCRIBE_TOKEN_BUDGET`, `CRITIC_TOKEN_BUDGET`), each
  with no silent fallback — a missing var raises loudly rather than defaulting
  to a wrong value
- [x] All four now check `finish_reason == "length"` explicitly and raise a
  named error before attempting to parse output, rather than surfacing as a
  confusing downstream JSON/parse failure
- [x] Fixed a copy-paste bug in `critic.py`'s HTTP error message (mislabeled
  "Scribe:" instead of "Critic:")
- [x] `RESEARCHER_TOKEN_BUDGET` set to 2048 — reasoned estimate based on
  Researcher's short expected output (tool call + <150-word summary), not
  yet confirmed against observed `finish_reason` behavior the way Critic's
  1024 was; worth a quick smoke-test pass to validate rather than assume

---

## 7. DBOS Pipeline

- [x] Wrap each agent call as `@DBOS.step()`
  - [x] Confirm `CRITIC_TOKEN_BUDGET` (`.env`, currently `1024`) still resolves correctly once `run_critic` runs inside a `@DBOS.step()` — `load_dotenv()` timing/process context can differ under DBOS's workflow execution vs. a bare script; a silent fallback to the 700 default would reintroduce the truncation bug found in §6
- [x] Confirmed: `spec_version` survives into Architect's step. Resolved via
  `_require_spec_version()` (pipeline/run.py) — raises loudly if the raw
  spec dict lacks `spec_version`, called once at workflow start. The
  design choice made here: pass the raw dict through to `architect_step`
  unchanged (not a re-built `ArchitectureSpec.model_dump()`), since
  `ArchitectureSpec` itself deliberately has no `spec_version` field
  (schemas/spec.py) — model_dump() would silently drop it. Fail-loud
  guard, not an auto-merge.
- [x] Wrap each thermal guard check as its own `@DBOS.step()` — implemented as
  `run_thermal_guard()` (pipeline/run.py), wired after Researcher/Architect/
  Scribe/Critic. Config: THERMAL_MAX_C=60, THERMAL_POLL_S=3,
  THERMAL_TIMEOUT_S=120, THERMAL_COOLDOWN_S=15 (unconditional cooldown per
  step, not in original spec). 60/3 chosen more conservative than spec's
  65/5 pending sensors-monitored full pipeline run. Smoke-tested
  (tests/smoke/test_thermal_guard.py, 8/8 passing incl. real sensors read
  on ZenBook). Full run_pipeline.sh integration run: CONFIRMED
  incident-free (22 Jul, workflow_id 4dc2da04-f2c7-4b41-915e-9ab301251bde).
  Guard fired 3x across the full 5-agent run (63.0°C, 64.0°C, 62.0°C),
  each recovered within poll window, no black-screen repeat of the
  earlier incident. Total pipeline time 4m8s (03:14:32–03:18:40), under
  5-min target. NOTE: spec §3 Observability still documents 65/5 —
  update spec or retune config, currently out of sync.
- [x] DBOS admin server port collides with mermaid-ink — both default to
  3001 (spec §4 Integration Points). Resolved by setting `admin_port:
  3010` in DBOSConfig rather than letting DBOS silently fail to bind,
  which is what happened on first attempt.
- [x] Step-boundary serialization: agent outputs cross `@DBOS.step()`
  boundaries as `.model_dump(mode="json")` dicts, not raw Pydantic
  instances, and are re-validated on receipt. Plain `.model_dump()`
  leaves datetime fields (e.g. `DiagramProvenance.generated_at`) as live
  `datetime` objects — DBOS checkpointing tolerates that (pickle), but
  `json.dumps()` on the final result doesn't, which broke the print at
  the end of the first real run. `mode="json"` fixes it at the source,
  since the same dicts get written to Postgres/markdown at approval time.
- [x] Print `workflow_id` to terminal on pipeline start
- [x] Write `pipeline/send_approval.py <workflow_id> [--reject "notes"]`
- [x] Confirm `DBOS.recv()` blocks correctly at human review
- [x] Confirm `DBOS.send()` from CLI unblocks the workflow
- [x] Approval path → outputs written to PostgreSQL + `artifacts/v<n>/`
  - [x] Assign `adr_id` — sequential (`adr_0001`, `adr_0002`, ...), not UUID. Matches
    the `ADR_GLOB_PATTERN "v*/adr_*.md"` filename convention `architect.py` already
    reads by. Implemented in `pipeline/persistence.py::_next_adr_id()`.
  - [x] Call `build_adr_record()` to bridge Scribe's `ADROutput` → `ADRRecord`
    (`schemas/adr.py`), invoked from `pipeline/persistence.py::persist_adr()`
  - [x] Serialize `ADRRecord` → `adr_<NNNN>.md` (frontmatter + Context/Decision/
    Consequences body) — writer is `pipeline/persistence.py::serialize_adr_markdown()`,
    the inverse of `architect.py`'s `_parse_adr_markdown()`. Round-trip verified
    against the real parser in `tests/integration/test_persistence.py`
    (`test_approval_path_end_to_end`), not just a shape match.
  - [x] `supersedes` population — human-specified at approval, not inferred from
    spec diff. Passed via `send_approval.py --supersedes adr_0003,adr_0004`
    (comma-separated adr_ids), read in `run.py`'s workflow as
    `decision.get("supersedes")`, defaults to `[]` if omitted.
  - [x] Postgres writes: `spec_versions` + `pipeline_runs` rows bootstrapped at
    workflow start (`db_bootstrap_step`, before Researcher runs, so
    `pipeline_runs.workflow_id` exists before later FK references); `artifacts`
    row written on approval via `insert_artifact_row()`, including the new
    `artifacts.adr_id` column (`schemas/002_add_adr_id.sql`) linking the DB row
    back to the markdown file. `judge_scores` stores the full `MetricScore`
    shape per metric (`value`/`target`/`flag_threshold`/`direction`/`flagged`/
    `flag_reason`), not a flattened `{metric: float}` map.
  - [x] Validated: `tests/smoke/test_persistence.py` (connectivity, migration
    presence, file-write liveness) + `tests/integration/test_persistence.py`
    (full approval/rejection round-trip against `TESTING_DATABASE_URL`,
    idempotency under simulated DBOS step retry) — 6/6 passing.
- [x] Rejection path → `revision_notes` written to blackboard, `revision_cycles` row
  inserted via `persist_rejection_step` → `insert_revision_cycle_row()`. Guarded:
  workflow raises if `notes` is empty even though `send_approval.py --reject`
  already enforces this client-side (defense in depth against another caller
  sending the message a different way). Asserted end-to-end in
  `tests/integration/test_pipeline_approval.py::test_full_pipeline_reject_flow`.
- [x] `validate_diagram_renders_step()` wired after Architect, before human
  review — catches malformed Mermaid (e.g. adr_0002's unclosed
  System_boundary()) before DBOS.recv(). Smoke-tested against fake
  http_get (tests/smoke/test_diagram_render.py, 4/4 passing, no real
  mermaid.ink call).
  
---

## 8. End-to-End Run

- [x] Submit one full spec through all 5 agents
- [x] Confirm swap sequence: Gemma → LFM → Gemma (2 swaps total)
- [x] Confirm full run completes within 5-minute target, `--threads 4` powersave.
  **Measured, not estimated (4 Aug 2026):** `duration_s` conflation bug fixed —
  `run.py` now checkpoints `compute_duration_s` separately from
  `approval_wait_s` via `capture_timestamp_step()`. First clean run
  (workflow `7005e756-9d7c-4e02-9bfe-927e66d211a2`, stress-test spec):
  `compute_duration_s=708.9s` (~11.8 min) — genuinely failing the target,
  not a contaminated number. Per-agent breakdown from run.py log
  timestamps: Researcher ~230s, Architect ~344s, Scribe ~34s, Critic
  ~37s, Judge instant (calculator only). Researcher + Architect (both
  Gemma) account for ~81% of total compute time.
  **Not met, target revised (6 Aug 2026):** three further clean runs, all
  still on Gemma, confirm this isn't noise: `compute_duration_s` of 873.5s
  (17-component spec, workflow `ed7cb519-...`) and 552.3s (6-component
  spec_v2, workflow `b1d55d1a-...`, the fastest yet — smaller diagram, not
  a stability improvement). Researcher + Architect remain ~70-80% of
  total compute across every run regardless of budget/prompt tuning on
  Scribe or Critic. Conclusion: 5 minutes is not achievable on Gemma at
  this hardware/quantization. **Closed (6 Aug 2026): target revised from
  5 min to <15 min on Gemma, met** (552.3s–873.5s observed across three
  runs). Full Researcher/Architect/Judge model swap to reduce this
  further remains parked (see Parking Lot) — not pursued now because
  LFM2.5-8B-A1B is already ruled out for tool-calling agents (Researcher,
  Judge) and LFM2-2.6B is only validated as an Architect-only candidate,
  untested for tool use. A scoped Architect-only pilot is the next step
  if revisited, not a blanket swap.
- [x] Run sustained thermal check across the *whole* pipeline (not just
  per-agent — this hasn't been validated end-to-end yet). **Note (22 July
  2026):** one full pipeline run ended in a hard power-off (black screen,
  no backlight, required power button) with no corresponding journald
  event — thermal_zone0, systemd-oomd, and suspend/resume all checked
  clean across the relevant boots, ruling out an OS-visible cause.
  Decode speed also sagged well below the ~14.8 tok/s baseline during
  the run (down to ~9.2 t/s), consistent with sustained load. Suspected:
  EC/firmware-level thermal cutoff below what `thermal_zone0` reports —
  not yet confirmed. This is the primary reason the thermal-guard-as-step
  item above is treated as higher priority than checklist ordering implies.
- [x] Approve via CLI → confirm artifacts written.
  **Reconfirmed (4 Aug 2026):** workflow `7005e756-9d7c-4e02-9bfe-927e66d211a2`
  approved via CLI, `adr_0003.md` written to `artifacts/v1/`, `artifact_id=5`
  persisted to Postgres. Thermal data point from the same run: peaked at
  64.0°C, ~19% of the run spent at/above the 60°C guard threshold,
  `no_turbo=1` held throughout, no OOM/crash.
- [x] Run a second spec with a deliberate change → confirm ADR triggered by diff.
  **Confirmed (6 Aug 2026):** `spec_v2.json` (10 microservices consolidated
  into 1 + `ops_staff`/admin dashboard actor removed) run against prior
  `spec.json`, workflow `b1d55d1a-92b6-4251-a95c-10a1326dce07`. Clean on
  first attempt after the fixes below: `diff_hunk_count=1`,
  `adrs_per_diff=1.0` (not flagged, target met exactly), non-salvaged ADR
  content. Also first confirmation that Architect's prior-ADR read works
  end-to-end: `informed_by_adrs: ["adr_0001"]`, `Informed by prior ADRs:
  adr_0001` in the review output — the Day 5 design decision (§2/§5 of
  the spec) is now validated in a live run, not just implemented.

**Bugs found and fixed during §8 stress-testing (6 Aug 2026), not on the original checklist:**
- `agents/scribe.py` — three distinct Scribe failure modes surfaced
  chasing the diff-trigger test above, none previously exercised because
  no real incremental diff had been run before:
  1. Truncation at `SCRIBE_TOKEN_BUDGET` (confirmed reproducible at
     temperature=0.05, not sampling noise). Fixed: `_salvage_truncated_
     scribe_output()` extracts every field that closed as valid JSON,
     salvages the in-progress field at its last complete sentence, marks
     anything unstarted with an explicit (bracket-free) placeholder for
     human review — never a silent blank, never a burned retry.
  2. Malformed JSON on a *complete* generation (`finish_reason != "length"`,
     e.g. `Expecting ',' delimiter`) — LFM finishing normally but producing
     structurally broken JSON. Reuses the same salvage path, tagged with a
     distinct log reason (`malformed_json` vs `truncated`) since the
     failure mode differs.
  3. Non-string field values — `diff_summary` came back as a nested dict
     on a real incremental diff (`spec_v2.json`), reproducible 3/3 times
     at temp=0.05, exhausted `scribe_step`'s DBOS retries and failed the
     whole pipeline (workflow `d26478a0-...`, ~670s of compute, zero
     artifacts). Root cause: `summarize_diff()` interpolated DeepDiff's
     raw dict reprs directly into the prompt; LFM appears to have mirrored
     that shape back into `diff_summary` instead of paraphrasing it. Fixed
     at the source (`_format_diff_detail()` renders diff entries as prose,
     never dict repr) and defended at the boundary (`_coerce_adr_string_
     fields()` stringifies+flags any non-string field instead of raising).
  All three salvage/coercion paths share one `salvage_reason`-tagged log
  line so a future run's `finish_reason=="length"` frequency, malformed-
  JSON frequency, and non-string frequency can be told apart in the logs.
- `pipeline/persistence.py` — `serialize_adr_markdown()` wrote
  `diff_summary` (and only that field) with no bracket-safety handling,
  unlike `supersedes`/`affected_diagrams` which already went through
  `_format_frontmatter_list()`. When Scribe's truncation salvage produced
  a bracketed placeholder (`[MISSING: ...]`), `architect.py`'s frontmatter
  reader parsed it as a YAML-style list instead of a string, silently
  failing `ADRRecord` validation and warn-skipping the file —
  `informed_by_adrs` came back `[]` on every run for two full pipeline
  runs before this was caught, quietly defeating the Day 5 prior-ADR-read
  behavior without any visible pipeline error. Fixed two ways: (1) salvage
  placeholders in `scribe.py` no longer use square brackets, removing the
  trigger; (2) `_guard_frontmatter_string()` added to `persistence.py` —
  fails loud at write time if any frontmatter string value starts with
  `[`, rather than writing a file that misparses silently three steps
  later on read. `artifacts/v1/adr_0001.md`'s corrupted `diff_summary`
  hand-corrected to unblock the fix from #3 above.
- Known limitation surfaced, not a crash — **not yet fixed:** Scribe's
  `decision`/`diff_summary` content shows signs of a canned "centralize
  product data in a single source of truth service" template, confirmed
  byte-identical across two structurally unrelated diffs (workflow
  `d86d5467-...`, a pure creation-diff with nothing to centralize from,
  vs `b1d55d1a-...`, where the sentence is at least directionally
  plausible). Doesn't affect pipeline mechanics — validates, renders,
  persists cleanly — so it isn't a §8 blocker, but it means ADR *content*
  isn't yet trustworthy without human review. Filed under existing
  Known Limitations "Rubber Stamp Risk" (§7 of the spec) rather than as
  a new item. Suggested check before trusting Scribe output generally:
  run against a diff deliberately unrelated to data duplication (e.g. a
  new notification channel, a timeout change) and confirm the canned
  text doesn't still appear; if it does, this is prompt-level (a worked
  few-shot example of a *different* kind of decision, same fix pattern
  that resolved Critic's empty-gaps bug), not mechanical.
- Stress-test artifacts (`adr_0001.md`, `adr_0002.md` and their review
  `.json`/`.md` pairs) relocated out of `artifacts/v1/`/`artifacts/v2/`
  to `artifacts/stress_test/` ahead of §9's GitLab commit, so synthetic
  e-commerce fixtures aren't mistaken for real project decisions in the
  public paper trail. Doubles as a regression fixture set for the
  template-bias issue above — re-run these same diffs after any Scribe
  prompt change to confirm the canned text stops appearing.

**Bugs found and fixed during §8 stress-testing (4 Aug 2026), not on the original checklist:**
- `agents/researcher.py` — hardcoded `timeout=150.0` on the Gemma HTTP call
  was too tight and caused a real `httpx.ReadTimeout` crash under load.
  Fixed: now reads `RESEARCHER_HTTP_TIMEOUT_S` from `.env` (set to 600,
  matching Architect's existing pattern), no silent fallback.
- `agents/architect.py` — two separate runs failed on `_validate_diagram_ids()`
  catching a `Rel(...)` referencing an undeclared element (`notification_service`,
  then `admin_dashboard` — both cases where the entity was correctly present
  in `COMPONENTS` but missing its diagram declaration line). Added an explicit
  declare-before-reference self-consistency instruction to `SYSTEM_PROMPT`.
  Held clean on the next run, but sample size is still too small (1 run
  post-fix) to call this confirmed — could also be diagram-complexity
  variance, since that run produced a coarser decomposition than the
  earlier failures.
- `pipeline/run.py` — `architect_step` given `retries_allowed=True,
  max_attempts=3` as a safety net, independent of whether the prompt fix
  holds.
- `agents/critic.py` — added an explicit "at most 5 gaps / 3 SPOFs / 3
  missing integrations" cap to `CRITIC_SYSTEM_PROMPT`. Deliberately left
  `CRITIC_TOKEN_BUDGET` at 4096 (not bumped) to isolate whether the prompt
  instruction alone is sufficient. First clean-run result: gaps=3 (✓),
  spofs=2 (✓), missing_integrations=5 (cap not respected, but did not
  truncate/crash). Also the first run to return non-empty, substantive
  Critic output grounded in real components — relevant to the Critic
  empty-gaps investigation (Week 1 priority per completion plan), though
  not yet enough runs to call that bug closed.
- Evaluated swapping Gemma (Researcher/Architect/Judge) for a Liquid model
  to cut compute time. Ruled out LFM2.5-8B-A1B: llama.cpp tool-calling is
  currently broken for its MoE architecture (open upstream issue), which
  disqualifies it for Researcher/Judge specifically. LFM2-2.6B remains a
  narrower, unverified option (Architect only, since it has no tool calls)
  — not pursued yet, would confound the in-progress Architect consistency
  investigation above if tested now.

---

## 8.1 Post-Run Fixes (13 Aug 2026, workflow 7cb63d6b-...)

**Confirmed via real run + human reject**, not yet fixed:

- [ ] **Scribe fabrication (P0, blocks trusting any ADR)** — decision text
  (tenant-scoped retrieval isolation) had no basis in spec/Researcher/
  Architect output; invented on a **zero-diff first run**, distinct from
  6 Aug's byte-identical-template issue (that one at least followed a real
  diff). Fix: system prompt must require grounding in SPEC/ArchitectOutput/
  diff, and explicitly say "no meaningful decision to record" when there's
  no real diff to explain, rather than inventing one. Same fix class as
  Critic empty-gaps.

  **Update (15 Aug 2026, workflows `3303ce31-...` and `48b3f065-...`, both
  `cloud_rag.json`):** confirmed again twice, same run session. `decision`
  reproduces Example 3's exact template ("Establish initial architecture: a
  glacier sensor network integrating with...") with real spec entities
  (AWS S3, AWS RDS, Confluence, hosted embedding/LLM APIs) substituted in,
  keeping the literal fictional phrase "glacier sensor network" -- a
  template-fill hybrid, not a full copy or a novel invention. First
  occurrence (`3303ce31-`) was **unflagged** by `_detect_example_copying()`
  -- swapping enough nouns dropped the fuzzy full-sentence similarity ratio
  below the 0.90 threshold even though the sentence shape and one literal
  phrase were copied outright. New `_detect_example_domain_leak()` substring
  guard added (fictional-domain tokens -- glacier/Iridium/crevasse/sensor
  network/etc. -- can never legitimately appear in a real spec, same
  zero-false-positive-risk class as `_DIFF_SYNTAX_TOKENS`), confirmed
  catching the identical failure correctly on the second run (`48b3f065-`).
  **This is a detection-layer win, not the grounding fix** -- `decision`'s
  root behavior (reaching for Example 3's template instead of synthesizing
  from the real diff) is unchanged across both runs. Still open.

  **Update (15 Aug 2026, workflow `18ed94eb-...`, `cloud_rag.json`):**
  confirmed a third time, but a notably different shape than the previous
  two -- `decision` this run is otherwise well-grounded (correctly names
  S3, RDS, Confluence, Zendesk, the embedding/LLM APIs, the vector store,
  and the ingestion chunk-quality-flagging detail, all genuinely present in
  the diff), with only the literal fictional phrase "a glacier sensor
  network integrating with" surviving as connective tissue at the start of
  the sentence. `_detect_example_domain_leak()` caught it correctly
  (`salvage_reason=example_domain_leaked`). New `_detect_ungrounded_content()`
  guard (added same day, not yet independently exercised) would not have
  fired here even if deployed -- `decision` shares heavy real vocabulary
  with the diff, so this case is squarely the domain-leak guard's job, not
  a gap in the new one. Still need a run where fabrication carries no
  glacier/Iridium token to validate the new guard's coverage independently.
  Still open.
- [ ] **Scribe truncation recurring at 4096** — `diff_summary` salvaged again
  (12-component spec). Check correlation with component count; consider
  raising `SCRIBE_TOKEN_BUDGET` or trimming diff-detail prompt input.
- [ ] **Critic spofs/missing_integrations = gaps, reworded** — all three
  lists identical content on this run, only schema-valid not analysis-valid.
  Needs prompt fix distinguishing the three categories, likely a worked
  example (same pattern as Architect declare-before-reference fix).

  **Second variant confirmed (15 Aug 2026, workflow `18ed94eb-...`,
  `cloud_rag.json`):** distinct failure shape from the original finding
  above. `spofs` (5 items) returned the exact same literal string five
  times -- `"RAG System interacts with Zendesk, Confluence, Git, LLM API,
  Vector Store via Rel"` -- not distinct-but-reworded content, straight
  repetition of one generic sentence. Inflated `spof_count` to 5 in Judge's
  metrics as a knock-on effect (flagged `spof_count` for review, target 0,
  threshold 1). Same root category (schema-valid, analysis-invalid) as the
  original finding, but the fix needs to address non-repetition as well as
  non-distinctness -- a worked example alone may not close the repetition
  case, since that looks more like Scribe's example-copying failure mode
  (degenerate output) than a distinctness problem; likely needs a boundary
  guard (reject/flag a list item identical to a prior item in the same
  list) alongside the prompt fix, not prompt wording alone.

  **Third variant (20 Aug 2026, workflow `1f38387f-...`, `cloud_rag_2.json`
  vs `cloud_rag.json`, single-field diff):** `gaps` and `missing_integrations`
  are now three genuinely distinct items each (Vector Store / Zendesk /
  Confluence) — first clean result on the reworded-duplication variant.
  `spofs` is unchanged: same literal sentence ("RAG System is a single
  instance handling both critical paths with no stated redundancy") three
  times, same as the 15 Aug `18ed94eb-...` finding. Useful narrowing: this
  run's diff is a single non-functional-requirements field change, about as
  far from `18ed94eb-...`'s 11-hunk creation diff as this test set gets, and
  `spofs` still degenerated while `gaps`/`missing_integrations` didn't —
  rules out diff density/complexity as the driver for `spofs` specifically,
  and separates it from whatever was making all three lists collapse
  together originally. Points at something particular to how the `spofs`
  field is generated (prompt ordering, field-specific instruction, or
  token-budget pressure late in the output), not a general list-repetition
  tendency. **Session started same day to fix this** — see status line
  below.
- [ ] **Architect `technology` list-vs-string (P3, self-heals via retry)** —
  add prompt instruction (join to comma string) + boundary coercion in
  `parse_model_sections()`, mirroring `_coerce_adr_string_fields()`.
- [ ] **Timing check, not a fix** — this run: `compute_duration_s=1341.4s`,
  well outside prior 552–874s range. Re-check once #1–#3 are fixed; may
  just be Scribe truncation cost, may be the open retries-timing item.

**Session plan (6 pomodoros, one variable at a time):**
1. Fix Scribe fabrication prompt only
2. Re-run same spec (`tests/simulated/cloud_rag.json`), confirm ADR is
   grounded and nothing else regressed
3. Fix Scribe truncation (budget or prompt trim)
4. Fix Critic gaps/spofs/missing_integrations distinctness
5. Re-run, full checklist sign-off pass
6. Buffer / Architect technology fix if time remains — defer to next
   session if not

**Status (14 Aug 2026):** step 1 is still in progress, not closed — see
§8.1a/§8.1b below. The diff-decomposition root cause under P0/P1 is now
confirmed and fixed (§8.1a), but the fix surfaced a second, previously-
hidden grounding failure (§8.1b, diff-syntax leak) that a same-day prompt
fix did not resolve; a detection backstop is in place but the underlying
grounding behavior is still open. Steps 3–4 (Scribe truncation, Critic
distinctness) not yet started. §8.1c is a new, unplanned item (Architect
id-declaration recurrence) surfaced during today's re-runs — not part of
the original 6-pomodoro scope, logged separately, not yet worked.

Do NOT attempt §9 (Paper Trail) or spec bumping until this addendum is
clear — nothing should hit the versioned artifact store with a known
fabrication bug still open.

**Status (15 Aug 2026):** §8.1b is now closed — see its section below for the
root-cause fix and two confirming runs. That resolves the diff-syntax leak
specifically, but does not clear the gate: P0 (Scribe fabrication on
`decision`) is still open and was reconfirmed twice today on the same
`cloud_rag.json` spec, now with a working detection guard
(`_detect_example_domain_leak()`) but no grounding fix. P1 (Scribe
truncation) and P2 (Critic distinctness) remain untouched. §8.1c (Architect
id-declaration) recurred a third time today, still unworked. §9 remains
blocked.

**Re-run (14 Aug 2026, workflow `726ed8f9-7189-4594-9cb7-5ad43c310228`,
`tests/simulated/cloud_rag.json`) — rejected. Partial progress, P0 not closed:**

- [x] Exact-match guard shipped and confirmed firing in the log:
  `WARNING: Scribe output for ['consequences', 'diff_summary'] exactly
  matches a worked-example string from the system prompt`. Confirms the
  guard mechanism itself works — flags inline, salvages, doesn't blind-retry
  at temp=0.05 (retrying would likely just reproduce the same copy).
- [ ] **P0 NOT closed — guard has a coverage gap.** The `decision` field is
  not in the guarded field list and came through as unflagged, verbatim
  worked-example text: *"Establish initial architecture: a glacier sensor
  network integrating with Iridium satellite network and field base station
  radio."* — has nothing to do with the cloud RAG spec being run. This is
  the same fabrication failure mode as the original P0 finding, just now
  half-caught instead of fully-caught. Two follow-ups, not one:
  1. **Immediate patch:** add `decision` to the exact-match guarded-fields
     list alongside `consequences`/`diff_summary`. Small, low-risk.
  2. **Real fix, still open:** exact-match only catches verbatim copies of
     the worked example. It won't catch paraphrased fabrication (the model
     inventing plausible-sounding but ungrounded content in its own words).
     The original planned fix — require grounding in spec/Researcher/
     Architect output, explicit "no meaningful decision to record" fallback
     on a zero-diff/creation run — is still the actual P0 item and is not
     superseded by the exact-match guard landing.
- [ ] **New: Critic truncation at `CRITIC_TOKEN_BUDGET=4096`, not previously
  tracked.** Log shows `ValueError: Critic: LFM hit max_tokens (4096)
  before finishing output` on attempt 1 against the 9-component
  `cloud_rag.json` spec; DBOS auto-retried (attempt 2 of 3) and the retry
  produced valid output (visible in the review doc). Distinct from the P2
  distinctness bug (spofs/missing_integrations reworded from gaps) — this
  is a straight budget truncation, same class as the original Scribe P1
  truncation but on the Critic side. Not yet confirmed whether this is
  component-count correlated (per the existing P1 hypothesis) or specific
  to this spec's prompt length. Needs its own line item, not folded into
  P2 — track separately since a retry masking it once doesn't mean it
  won't fail all 3 attempts on a denser spec.
- Reject notes filed: "Scribe decision field is fabricated verbatim
  worked-example text (glacier sensor/Iridium), unrelated to spec and
  unguarded by the new exact-match check, which only covers
  consequences/diff_summary." — writes a `revision_cycles` row only, does
  not touch the versioned artifact store (consistent with existing
  rejection-path design, §7).

§8.1 remains open. Do not proceed to §9 until: `decision` field is guarded
(or the grounding-based P0 fix lands and makes the guard moot), Critic
truncation is triaged as its own item (or explicitly folded into P1 once
component-count correlation is confirmed either way), and a clean re-run
against `cloud_rag.json` shows no fabricated/copied content in any of the
three Scribe fields.

**Correction to the entry above:** `decision` was never actually missing
from the guarded-fields list — `agents/scribe.py`'s `_detect_example_copying()`
already checked `("decision", "consequences", "diff_summary")`, all three.
The prior write-up inferred a coverage gap from the log line alone (only 2
of 3 fields printed as flagged) without having read the code; the real
mechanism was different and is corrected below.

**Fix implemented (14 Aug 2026) — near-verbatim (fuzzy) match, not a
field-list gap:**

- [x] Root cause confirmed: the model's fabricated `decision` text dropped
  three articles ("the", "the", "a") relative to the worked-example string
  — 96% similar by raw character comparison, but not byte-identical — so it
  passed the old exact string-membership check (`stripped in
  _EXAMPLE_OUTPUT_STRINGS`) while `consequences`/`diff_summary` (copied
  verbatim) were correctly caught. Not a temperature/sampling issue —
  confirmed reproducible at temp=0.05, i.e. deterministic near-copying, not
  noise a resample would avoid.
- [x] `_detect_example_copying()` rewritten: normalizes case/whitespace/
  articles, then compares via `difflib.SequenceMatcher` at a 0.90
  similarity threshold instead of exact equality. `"No field-level changes
  detected."` special-case (legitimate on a real zero-diff run) left
  unchanged.
- [x] Sanity-checked before shipping: the actual fabricated `decision` text
  normalizes to a 100% match (correctly flagged); two genuinely different,
  real-content decision strings (RAG-specific, Stripe-specific) scored
  0.60 and 0.30 (correctly pass through unflagged) — threshold doesn't
  appear to over-fire on legitimate content.
- [x] **Re-run confirmation (workflow `6a7953f7-3494-4654-a69a-2b0422e46b0c`,
  same spec):** all three fields — `decision`, `consequences`,
  `diff_summary` — now flagged: `WARNING: Scribe output for ['decision',
  'consequences', 'diff_summary'] closely matches...`. `decision` is
  correctly caught this time. Rejected per the same reasoning as
  `726ed8f9` — a guard correctly firing is not grounds to approve, it's
  the guard doing its job.
- [x] Log message wording fixed: was "exactly matches a worked-example
  string", which became inaccurate once the check also fires on
  near-verbatim matches (as it did for `decision` on this run). Now reads
  "closely matches (exact or near-verbatim)".

**Still open — this fix closes the specific evasion found, not the P0
item itself:**

- The fuzzy-match guard is still a detection-layer backstop, not the
  grounding fix. It only catches output that's *structurally* the same
  sentence as a worked example (exact or near-verbatim). It will not catch
  fabricated content that's topically similar but reworded enough to score
  below 0.90 — that class of failure still depends entirely on the
  prompt's CRITICAL GROUNDING RULE / CRITICAL NO-DIFF RULE holding up on
  their own, which is not yet independently confirmed since every observed
  fabrication so far has been a near-copy of Example 3, not a novel
  paraphrase.
- Suspected deeper root cause, still untouched: `compute_spec_diff`/
  `summarize_diff` collapses a creation-diff (`prior_spec=None`) into a
  single `values_changed: root -> changed from {} to {...huge dict...}`
  line, not the clean per-field `dictionary_item_added` bullets Example 3
  in the prompt implicitly trains the model to expect. This mismatch
  between what the model is shown and what the worked example primes it
  for is the leading hypothesis for why the model keeps reaching for
  Example 3 wholesale on every creation-diff run against `cloud_rag.json`,
  rather than this being a one-off. Not yet fixed — next candidate item,
  one variable at a time per session-plan discipline.
- Critic truncation item (above) unchanged, still open, not touched by
  this fix.

---

## 8.1a Diff-Decomposition Fix (14 Aug 2026)

Follow-up to the "suspected deeper root cause" note directly above — confirmed
and fixed, not just inferred.

- [x] **Root cause confirmed via direct DeepDiff testing** (not read from
  docs): `DeepDiff({}, populated_dict)` only emits clean per-key
  `dictionary_item_added` entries when a *single* top-level key is added
  against an empty baseline. The moment 2+ top-level keys are added, it
  collapses the whole comparison into one root-level `values_changed`
  entry instead. Every real creation run (`prior_spec=None`) hits this,
  since `compute_spec_diff()`'s baseline is always `{}` in that case — a
  quirk of a *completely empty* starting dict, not a general DeepDiff bug.
  This is the leading (now confirmed) explanation for why Scribe kept
  reaching for Example 3 wholesale: the model was primed on clean
  per-section bullets but shown one giant dict-repr blob instead.
- [x] `agents/scribe.py::summarize_creation_diff()` added — for
  `prior_spec=None` only, bypasses `compute_spec_diff()`/DeepDiff entirely
  and builds the same `dictionary_item_added: <section> -> added (...)` /
  `<section>.<list> -> added [...]` bullet shape directly from
  `current_spec.model_dump()`. Incremental diffs (`prior_spec` present)
  untouched — DeepDiff already produces clean per-field entries once the
  baseline has real content (confirmed working, `diff_hunk_count=1` on the
  6 Aug `spec_v2.json` run).
- [x] **Confirmed via real run** (workflow `1fd6ff3f-...`, `cloud_rag.json`):
  log shows `hunk_count=11`, eight clean per-section bullets, zero
  collapsed blob, zero glacier/satellite leakage. The specific bug this
  section targeted is fixed.

## 8.1b Diff-Syntax Leak — New Bug Surfaced by the Fix Above (14 Aug 2026)

The clean bullets exposed a second, previously-invisible failure mode:
Scribe echoing the diff's own internal syntax tokens verbatim instead of
paraphrasing.

- [ ] **Not yet actually fixed.** Symptom, both `1fd6ff3f-...` and
  `7570a555-...` (same spec, two separate runs): `decision`/`diff_summary`
  came back as `"Add a dictionary_item_added for project_overview: added
  (purpose, target_users, deployment_environment)."` — the model treated
  DeepDiff's bookkeeping label (`dictionary_item_added`, the `->` arrow,
  the `added (...)` wrapper) as English content to restate, rather than a
  structural marker to read past. `1fd6ff3f-...`'s `consequences` also
  pulled an S3 dependency mention from **Blackboard context** (Researcher's
  pricing summary) even though S3 never appears in the diff — traced to a
  self-contradiction in `SCRIBE_SYSTEM_PROMPT`'s grounding rule, which said
  "ONLY the Spec diff" in one sentence and then explicitly permitted
  "Spec diff or Blackboard context" in the next.
- [x] **Prompt fix attempt (`agents/scribe.py`, `SCRIBE_SYSTEM_PROMPT`):**
  removed the Blackboard-context grounding loophole; added an explicit
  "DIFF FORMAT IS NOT CONTENT" rule naming the exact banned tokens
  (`dictionary_item_added`, `values_changed`, `iterable_item_added`, the
  `->` arrow, `added (...)`/`added [...]`) with a worked before/after
  example using this run's own project_overview bullet.
- [ ] **Confirmed NOT effective on re-run** (workflow `7570a555-...`, same
  spec, same temperature=0.05): `decision` and `diff_summary` came back
  **byte-identical** to the pre-fix output — still containing
  `dictionary_item_added` verbatim. `consequences` was independently
  flagged by the existing example-copy guard as a verbatim copy of
  **Example 1**'s text ("...outbound dependency on satellite uplink
  availability.") — not even Example 3 (the creation-diff example), just
  whichever worked example the model reached for. One clean, deterministic
  failure of the prompt-only fix on a fully unambiguous pattern.
- [x] **Boundary backstop added** (`agents/scribe.py`):
  `_detect_diff_syntax_leak()` — plain substring check for DeepDiff's
  internal category tokens (`dictionary_item_added`,
  `dictionary_item_removed`, `iterable_item_added`, `iterable_item_removed`,
  `values_changed`, `type_changes`) across `decision`/`consequences`/
  `diff_summary`, flagged inline as `"POSSIBLE DIFF-SYNTAX LEAK -- FLAG FOR
  HUMAN REVIEW: ..."` rather than retried (same reasoning as the example-
  copy guard: temp=0.05 retry would likely reproduce the same leak).
  Unlike the fuzzy example-copy check, this is a safe plain substring match
  with no false-positive risk — these tokens can never legitimately appear
  in English ADR prose — so it was added immediately after one confirmed
  prompt-fix failure rather than waiting for a second, matching the
  severity of a fully deterministic, unambiguous pattern rather than a
  judgment call.
- **Still open, not superseded by the guard landing:** the guard is
  detection-layer only, not the grounding fix. Two same-spec creation-diff
  runs in a row both show Scribe defaulting to shallow copy behavior on
  this diff shape — grabbing the first (least substantive) bullet for
  `decision`/`diff_summary`, and reaching for an unrelated worked example
  for `consequences` rather than synthesizing from the real diff at all.
  Worth treating as an open question about whether this is a wording
  problem or an attention/capacity limitation on `cloud_rag.json`'s
  11-hunk diff specifically, not assumed closed by either fix so far.

**Rejected runs, this thread:**
- `1fd6ff3f-5371-4520-b016-05426483ae12` — rejected: diff-syntax echo in
  decision/diff_summary; consequences leaked S3 from Blackboard context
  (not in the diff).
- `7570a555-c13e-475e-9359-8df3a7383db9` — rejected: diff-syntax echo
  persisted unchanged after the prompt fix; consequences independently
  flagged by the example-copy guard as a verbatim Example 1 copy.

**Root-cause fix confirmed (15 Aug 2026) — diff-syntax tokens removed from
prompt input, not just detected at the boundary:**

- [x] Root cause confirmed: `SCRIBE_SYSTEM_PROMPT`'s worked examples (Example
  1, Example 3) showed DeepDiff's raw category tokens
  (`dictionary_item_added`, etc.) as the normal shape of "Spec diff" input,
  and `summarize_diff()`/`summarize_creation_diff()` interpolated those same
  literal tokens into every real diff bullet fed to the model. The model
  wasn't disregarding an instruction — it was pattern-matching a template it
  had genuinely been shown twice (once as worked-example input, once as real
  input). The 14 Aug prompt-only fix failed for exactly this reason: telling
  the model "don't repeat this token" changes nothing when the token is
  still the first thing on every line it's asked to summarize.
- [x] `_CHANGE_TYPE_VERBS` + `_describe_change()` added (`agents/scribe.py`):
  every DeepDiff category is translated to a plain verb (Added / Removed /
  Changed) before it reaches the prompt — no raw category token, no `->`
  arrow, in either `summarize_diff()` (incremental) or
  `summarize_creation_diff()` (creation). Example 1 and Example 3's "Spec
  diff:" text in `SCRIBE_SYSTEM_PROMPT` rewritten to match the new real
  input shape exactly, closing the train/test mismatch that let the leak
  happen in the first place.
- [x] **Confirmed via two independent `cloud_rag.json` runs** (both 15 Aug
  2026): workflow `3303ce31-acf5-444d-91b6-45666c16c645` and workflow
  `48b3f065-157c-4339-a69a-5c68e724cc59`. Both show clean prose diffs in the
  run log (`Added project_overview: purpose, target_users,
  deployment_environment`, no tokens, no arrows) and **zero**
  `_detect_diff_syntax_leak()` warnings on either run.
  `_DIFF_SYNTAX_TOKENS` substring check kept in place as a zero-cost
  backstop for any future unmapped DeepDiff category — not expected to fire
  again in normal operation, but costs nothing to keep.

**§8.1b closed (15 Aug 2026).** Diff-syntax leak fixed at the source. Note:
both confirmation runs surfaced a *separate*, still-open fabrication issue on
`decision` (Example 3 template-fill with real entities) — tracked under
§8.1's P0 item above, not reopened here; it is not a recurrence of the
diff-syntax mechanism this section targeted.

## 8.1c Architect: `_validate_diagram_ids` Recurrence (14 Aug 2026, workflow `7570a555-...`)

Reopens the 4 Aug §8 item that was closed as "held clean on the next run,
but sample size still too small to call confirmed" — this run is that
confirmation, and it says the fix isn't holding.

- [ ] **Not fixed.** `architect_step` retried twice before succeeding
  (17:20:32 → 17:37:27, ~17 min vs. the normal ~5-6 min compute for this
  step): attempt 1's `Rel(...)` referenced `agentSidebar`, undeclared
  against a camelCase id scheme (`adminConsole`, `docsTeam`, ...); attempt
  2's `Rel(...)` referenced `postgresql`, undeclared against a *different*,
  snake_case id scheme (`admin_console`, `agent_sidebar`, ...) on the same
  spec. Two failures in one run, two different id-casing conventions —
  suggests the model isn't holding a stable id vocabulary across the
  diagram generation at all, not just occasionally forgetting one
  declaration.
- Logged as its own item, separate from §8.1's Scribe/Critic bugs (Gemma/
  Architect, not LFM/Scribe) — not yet worked. Next pomodoro candidate,
  not bundled into the current Scribe session per one-variable-at-a-time.

**Third recurrence (15 Aug 2026, workflow `48b3f065-157c-4339-a69a-5c68e724cc59`):**
`architect_step` retried once (15:12:21 → 15:24:01, ~11.5 min vs. normal
~5-6 min): attempt 1's `Rel(...)` referenced undeclared `admin_console`
against a declared-id set (`agent`, `confluence`, `customer`, `doc_team`,
`embedding_service`, `git_repo`, `llm_api`, `postgres`, `rag_system`,
`vector_store`, `zendesk`) that didn't include it; self-healed on retry.
Third occurrence across three separate dates (4 Aug, 14 Aug, 15 Aug), still
not fixed, still not worked — logged for the pomodoro queue.

**Fourth recurrence (15 Aug 2026, workflow `18ed94eb-7f78-40e4-86c3-b26278b8ad5a`):**
`architect_step` retried once (18:10:57 → 18:13:30, ~13 min vs. normal
~5-6 min): attempt 1's `Rel(...)` referenced undeclared `vector_store`
against a declared-id set (`admin_console`, `agent_sidebar`, `confluence`,
`customer`, `docs_team`, `embedding_api`, `escalation_handler`,
`evaluation_service`, `feedback_service`, `generation_service`, `git_repo`,
`ingestion_service`, `llm_api`, `rag_system`, `retrieval_service`,
`support_agent`, `support_chat_widget`, `zendesk`) that didn't include it;
self-healed on retry. New observation beyond the prior three occurrences:
this failed attempt's decomposition is far more granular (18 components,
service-level) than the final, approved-shape output that shipped after
retry (9 components: `customer`, `agent`, `rag_system`, `zendesk`,
`confluence`, `git_repo`, `llm_api`, `embedding_api`, `vector_store`) — so
the model isn't just inconsistent about id-casing across attempts, it's
producing genuinely different decomposition granularity attempt to attempt
on the identical spec. Stronger signal than previously logged that this
isn't a shallow prompt-wording issue. Fourth occurrence across four
separate dates (4 Aug, 14 Aug, 15 Aug x2), still not fixed, still not
worked — logged for the pomodoro queue.

---

## 8.1d Run Review — 15 Aug 2026, workflow `18ed94eb-7f78-40e4-86c3-b26278b8ad5a`

**Rejected.** Confirms P0 still open (third occurrence, new shape), surfaces
a new P2 variant, and adds the fourth §8.1c data point (logged above).

- P0 (Scribe fabrication) and the fourth §8.1c recurrence are logged under
  their respective sections above, not duplicated here.
- P2 second variant (Critic `spofs` literal repetition, not just reworded
  duplication) logged under §8.1's P2 bullet above.
- **Non-blocking observation, Scribe `diff_summary` quality:**
  `hunk_count=11` but `diff_summary` omitted `project_overview`,
  `core_features`, and `key_user_flows` entirely — no truncation warning
  fired (not `finish_reason=="length"`, no salvage placeholder), so this
  reads as the model choosing incompleteness over hitting the token
  budget, not a P1 recurrence. Not conflating with P1 on this run; worth a
  line item of its own if it recurs.
- **Reject command used:**
  ```
  python pipeline/send_approval.py 18ed94eb-7f78-40e4-86c3-b26278b8ad5a --reject "Scribe decision retains fictional 'glacier sensor network' phrase amid otherwise grounded content (P0, still open). Critic spofs list is 5 identical duplicate strings, not distinct analysis (P2, new degenerate variant). Architect self-healed a 4th §8.1c recurrence, also showing unstable decomposition granularity across retry attempts, not just id-casing."
  ```

**Status (15 Aug 2026, later same day):** `_detect_ungrounded_content()`
guard (agents/scribe.py) shipped same day, ahead of this run, but not yet
independently exercised — this run's fabrication was already caught by
`_detect_example_domain_leak()` before the new guard's logic would matter.
Still need a run with fabrication carrying no glacier/Iridium token to
confirm the new guard's coverage. P0, P1, and P2 all remain open; P2 now
has two confirmed distinct failure shapes (reworded duplication and literal
repetition) that the eventual fix needs to cover. §8.1c stands at four
confirmed recurrences across three dates, with growing evidence (this run)
that it's a decomposition-granularity instability, not just an id-casing
one. §9 remains blocked.

---

## 8.1e P0 Closure — Guard Coverage, Not Model Behavior (19 Aug 2026)

Continues from §8.1d. Covers a full-day thread of `agents/scribe.py` fixes
and six consecutive `cloud_rag.json` runs (all `prior_spec=None`, all
`hunk_count=11`), ending in a deliberate decision to close P0 on guard
coverage rather than continue chasing model behavior. Also folds in two
fixes from an unsynced prior session (15 Aug evening, delivered as a
download, never committed to this file) that this thread's runs depend on.

**Fixes shipped this thread, in order:**

- [x] **Conditional system-prompt selection** (`_build_scribe_system_prompt()`).
  Follow-up to the 15 Aug bracket-placeholder fix, which closed Examples 1/3
  as copy targets but left Example 2 (the zero-diff boilerplate) as the one
  remaining finished, quotable text in the prompt — duplicated a second time
  in the CRITICAL NO-DIFF RULE block. `run_scribe()` now selects between two
  prompt variants based on whether `diff_summary` is genuinely empty; the
  NO-DIFF rule and Example 2 are only in context on a real zero-diff run.
  **Confirmed selecting correctly on all six runs this thread** (log line:
  `Scribe system prompt variant selected: real-diff...`). **Not yet
  confirmed on the zero-diff branch itself** — no approved spec version
  exists yet to produce a genuine empty diff against. See Status below.
- [x] **List-coercion salvage** (`_salvage_list_valued_field()`,
  `_find_matching_bracket()`). Malformed-JSON salvage previously only
  matched `"field": "string"` shape and returned `MISSING` on any
  list-valued field, producing a total 3-field blackout (workflow
  `b67bea1a-...`). New path extracts what it can from a list-shaped field
  (via `json.loads()` on the bracket span, falling back to a regex scan of
  quoted substrings if the array itself is also malformed), joins, caps at
  300 chars, and prefixes `LIST-COERCED (flag for review...)`. **Confirmed
  working** (workflow `34876593-...`): readable flagged text instead of a
  blackout.
- [x] **`_detect_diff_dump()` guard.** Closes the gap exposed by
  `34876593-...`: that run's near-total diff copy was only caught because
  the copied text happened to contain literal brackets (`_detect_bracket_
  leak()` fired by coincidence). Flags a field that's either far past
  SCRIBE_SYSTEM_PROMPT's own "ONE sentence, <=25 words" rule (3x buffer) or
  a large fraction of the raw diff's own length. **Confirmed independently
  capable** in isolation against real run data (workflow `166ad426-...`'s
  decision text: 167 words, would trip the word-count path on its own) but
  **not yet confirmed as the first guard to fire in a live run** — every
  run since has been caught by `_detect_bracket_leak()` first.
- [x] **`key_user_flows` terse-label reformatting** (`_terse_flow_label()`,
  applied in `summarize_creation_diff()`). Hypothesis: `key_user_flows` is
  the only diff section written as narrative sentences chained with `->`,
  making it structurally read like a pre-written decision. Reformatted each
  entry to `<trigger clause> (<n>-step flow)`, dropping the arrow chain
  entirely — confirmed feeding cleanly into the prompt (workflow
  `8b89bcad-...`'s log shows the terse form). **Hypothesis ruled out, not
  confirmed**: the very next run copied the exact terse list wholesale
  anyway. Whatever draws the model to this section isn't its narrative
  shape — more likely its position in the diff or some other structural
  property not yet identified. Not pursuing further this session per the
  closure decision below.

**Six-run log, same spec, same `hunk_count=11`, six distinct failure
shapes, six correct catches — the evidence base for closing P0 on guard
coverage:**

| Workflow | Failure shape | Caught by |
|---|---|---|
| `b67bea1a-...` | `decision` returned as malformed JSON array of diff bullets, unrecoverable (pre-dates list-coercion fix) | Total blackout, `MISSING` placeholder (motivated the list-coercion fix) |
| `34876593-...` | `decision`/`consequences` both list-valued, near-total diff copy | `_detect_bracket_leak()` (coincidental — motivated `_detect_diff_dump()`) |
| `166ad426-...` | `decision` clean string, raw `key_user_flows` diff line pasted in with brackets intact | `_detect_bracket_leak()` |
| `b0d73799-...` | Same as above, this time breaking JSON via an unescaped quote inside the copied text | `_detect_bracket_leak()` (post-salvage) |
| `7e7b0948-...` | Byte-identical to `166ad426-...` despite an added prompt instruction naming `key_user_flows` specifically — confirms instruction-only fixes don't move this model | `_detect_bracket_leak()` |
| `8b89bcad-...` | Terse-label reformatting fed cleanly into the prompt, model still copied the shortened list wholesale — rules out the narrative-shape hypothesis | `_detect_bracket_leak()` |

All six rejected. `adrs_per_diff` stayed at 0.09 across every run — no
usable ADR content was produced against this diff on any of the six
attempts, but nothing ungrounded or copied reached the approval gate on
any of them either.

**Closure decision (19 Aug 2026): P0 closed on guard coverage, not on
model behavior.** Explicit reframe from the original bar ("the model must
not fabricate") to the bar that actually protects `PRIOR_DECISIONS`
integrity ("nothing ungrounded or copied can reach approval silently").
Six runs, six distinct shapes — malformed list, coerced list, clean-string
diff-paste, diff-paste with broken JSON, a repeat after an instruction-only
fix, a repeat after a structural reformatting fix — every one caught before
approval, by an existing guard in five of six cases and independently
confirmable by a sixth. This model, at this diff density, reliably defaults
to copying `key_user_flows` under grounding pressure; two independent fix
attempts (instruction-level, then structural) both failed to change that,
and a third attempt wasn't pursued — decisive closure over continuing to
chase root behavior on a 1.6B CPU model, per existing project principle.
Filed as a permanent, confirmed instance of the existing "Rubber Stamp
Risk" known limitation (spec §7), not as a reopened bug.

- [ ] **Remaining before P0 is fully closed:** a zero-diff confirmation
  run — resubmit any approved spec version unchanged, confirm `diff_summary`
  reads exactly `"No field-level changes detected."` and the zero-diff
  prompt branch (CRITICAL NO-DIFF RULE + Example 2, only included on this
  branch per the conditional-prompt fix above) still produces the correct
  fixed boilerplate. Does not require `cloud_rag.json` specifically — any
  spec that reaches approval first unblocks this, since `spec_versions`
  needs at least one approved row to diff against. `cloud_rag.json` itself
  remains rejected six-for-six and is not being pursued toward a seventh
  attempt; it stays on record as the stress fixture that established guard
  coverage, not as the source of the `v1` artifact.

**Status (19 Aug 2026):** §8.1 closed for the purposes of unblocking §9 —
detection coverage confirmed across every failure shape produced against
the hardest fixture in the test set, model-behavior root cause explicitly
not pursued further and filed as a known limitation instead. P1 (Scribe
truncation) and P2 (Critic distinctness) remain open, untouched this
thread, not blocking §9. §8.1c (Architect id-declaration) held clean across
all six runs this thread (zero recurrences) — worth one more data point
before considering it closed, not urgent. §9 unblocked pending only the
zero-diff confirmation run above.

---

## 8.1f Scribe Diff-Path Leak — Fixed and Confirmed Closed (20 Aug 2026)

First real incremental-diff run (`prior_spec` present, not a `cloud_rag.json`
creation diff) exercised in this whole thread — `spec.json` (v1) →
`spec_v2.json` (v2), single-field change (`non_functional_requirements.
performance`, 5s → 10s). Surfaced a leak the §8.1a/§8.1b fixes never
touched, because `summarize_diff()` (the incremental path) is a different
code path from `summarize_creation_diff()` (what every prior `cloud_rag.json`
run exercised).

- [x] **Root cause confirmed:** `summarize_diff()` builds each bullet via
  `_describe_change(change_type, path, detail)`, and `path` came straight
  from `diff.to_dict()` under DeepDiff's `view="tree"` — DeepDiff's own path
  repr, e.g. `root['non_functional_requirements']['performance']`. The
  8.1a/8.1b fixes translated the category token (`values_changed` →
  `Changed`) and cleaned up value formatting, but never touched the path
  string. `summarize_creation_diff()` was never exposed to this because it
  builds paths by hand from `_non_empty_field_names()`, not from DeepDiff's
  repr — so nothing in the six-run §8.1e evidence base could have surfaced
  it.
- [x] **Confirmed on workflow `1060730f-...` (20 Aug 2026):** `decision`
  contained `root['non_functional_requirements']['performance']` verbatim.
  Caught by `_detect_bracket_leak()` as an incidental backstop (the repr
  happens to contain brackets), not by `_detect_diff_syntax_leak()` (whose
  token list doesn't include `root[` / bracket-path syntax) — same
  "caught by an accident of what the leak looked like, not a targeted
  check" pattern as `_detect_diff_dump()`'s own motivating case.
- [x] **Fix shipped (`agents/scribe.py`):** `_clean_diff_path()` + backing
  `_DEEPDIFF_PATH_RE`, added directly after `_CHANGE_TYPE_VERBS`; converts
  `root['a']['b'][2]` → `a.b[2]`, matching the dotted/bracketed style
  already shown as normal in `SCRIBE_SYSTEM_PROMPT`'s own examples.
  `_describe_change()`'s one return line updated to call it. No new import
  needed (`re` already used elsewhere in the file).
- [x] **Confirmed via re-run, workflow `1f38387f-...` (20 Aug 2026), same
  spec pair:** log line reads `Changed non_functional_requirements.
  performance: changed from '...' to '...'` — no `root[`, no bracket-path
  syntax. `decision`/`consequences`/`diff_summary` were still flagged this
  run, but by `_detect_diff_dump()` (length/diff-dump), a separate and
  already-known-open issue, not this leak. **§8.1f closed** — this specific
  leak shape does not recur.

**New open item surfaced by these same two runs, not part of this
fix:** neither run's `consequences`/`diff_summary` got the *direction* of
the change right — both described the 5s→10s change (a loosened, more
permissive latency requirement) as "tightened" / "a stricter performance
expectation," the opposite of what happened. Confirmed 2/2 on workflows
`1060730f-...` and `1f38387f-...`. No existing guard checks this — every
guard so far targets copying/leaking/length, not semantic correctness of a
numeric or threshold change. Both instances happened to also get flagged
by `_detect_diff_dump()` on length, so nothing has silently passed yet, but
that's incidental, not because the directionality is checked. Logged as an
open item, not scheduled this session — needs design thought (what does
"backwards" mean generically across arbitrary field types?), not a quick
prompt patch, and per the project's own track record instruction-only
fixes haven't held on this model.

## 7.1 Infra Risk: `run_pipeline.sh`'s EXIT Trap Can Delete a Workflow Correctly Awaiting Review (20 Aug 2026)

Surfaced by an accidental Ctrl-C on workflow `1f38387f-...` — hit *after*
the pipeline had already printed `[5/5] Judge done. Awaiting human review.`
(i.e. the workflow was correctly blocked on `DBOS.recv()`, not stuck or
crashed).

- [ ] **Not yet fixed, needs verification first.** `run_pipeline.sh`'s
  `trap 'restore_turbo_state; stop_thermal_monitor; clear_pending_workflows'
  EXIT` runs `clear_pending_workflows()` unconditionally on any exit,
  including Ctrl-C. That function does `DELETE FROM dbos.workflow_status
  WHERE status = 'PENDING'` — but a workflow correctly paused at
  `DBOS.recv()` awaiting human approval is *also* status `PENDING` in DBOS;
  there's no separate status distinguishing "genuinely stale, safe to
  sweep" (the case the script's own header comment describes: a Ctrl-C'd or
  crashed run whose leftover would double-execute alongside the next
  `DBOS.launch()`) from "correctly blocked on a human gate, ~15 min of
  Researcher+Architect compute already sunk into it." Log confirms: `DELETE
  1` immediately after the Ctrl-C.
- [ ] **Action item before next run:** confirm whether `1f38387f-...` is
  actually unrecoverable — `python pipeline/send_approval.py 1f38387f-...
  --reject "test"` and check whether it errors (workflow_status row gone,
  decision unrecordable, compute sunk) or succeeds (row survived, only
  something else was cleared). Don't assume either way without checking.
- [ ] **Proposed fix, not yet applied:** the pre-run `clear_pending_
  workflows()` call at the top of the script (before `DBOS.launch()`)
  already covers the concurrent-recovery problem the header comment
  describes — a stale leftover from *last* run gets swept before *this*
  run starts. The trap-based post-run call adds no protection beyond that
  for next time, but does add this failure mode on any interrupt during a
  review wait. Proposed: drop `clear_pending_workflows` from the `trap`
  line, keep only the explicit pre-run call. Not applied yet — should be a
  standalone change per the one-variable-at-a-time rule, not bundled with
  the Critic `spofs` fix below.

**Status (20 Aug 2026):** §8.1f closed (Scribe diff-path leak fixed and
confirmed via `1f38387f-...`). Directionality gap logged as open, not
scheduled. §7.1 (`run_pipeline.sh` trap risk) open, verification pending
before the fix is applied. P2 (`spofs` repetition) — see updated bullet
above (third variant, workflow `1f38387f-...`) — session started same day
to fix `critic.py`; not yet closed. P1 (Scribe truncation) still untouched.
§9 remains unblocked per 19 Aug status; none of today's findings reopen it.

---

**Known gap carried into §9 (19 Aug 2026):** Zero-diff prompt branch
(CRITICAL NO-DIFF RULE + Example 2, conditional prompt selection) has
never fired in a real run — no approved spec version existed today to
diff against, so `diff_summary == "No field-level changes detected."`
was never produced. Implemented and reviewed, not empirically confirmed.
Accepted as low-risk for demo purposes (not a scripted demo path);
revisit only if a live resubmit-unchanged flow is planned on stage.

## 9. Paper Trail

**Status:** Unblocked, started 19 Aug 2026.

- [ ] Verify `revision_cycles` rows exist for today's six rejected
  `cloud_rag.json` workflows (`b67bea1a`, `34876593`, `166ad426`,
  `b0d73799`, `7e7b0948`, `8b89bcad`) — data already exists, query only.
- [ ] Verify `judge_scores.json` persisted per run with `spec_version`
  reference for the same six workflows — query only.
- [ ] Produce a real approved run (any spec, not necessarily
  `cloud_rag.json`) to generate `artifacts/v1/*.md` content —
  `cloud_rag.json` stays on record 6-for-6 rejected, not pursued
  toward a 7th attempt.
- [ ] Commit `artifacts/v1/*.md` to GitLab
- [ ] Confirm GitLab renders `context_diagram.md` (C4Context block) inline

---

## Observability Checks (run throughout, not just at the end)

- [ ] Arize Phoenix reachable at `localhost:6006`
- [ ] Per-agent OTel spans visible in Phoenix UI (system prompt, tool calls, latency, model)
- [ ] `lm-sensors` readable from Python thermal guard step

---

*Sequence is intentional: pre-flight reconciliation → infra → agents in isolation → pipeline wiring → end-to-end → paper trail. Don't skip ahead to pipeline wiring before each agent is independently validated.*