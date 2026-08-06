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
- [ ] Confirm full run completes within 5-minute target, `--threads 4` powersave.
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
  this hardware/quantization; closing this line as **not met on Gemma —
  target requires the planned LFM2.5-2.6B swap for Researcher/Architect/
  Judge (see Parking Lot), not further tuning**. Re-open and re-measure
  once that swap is validated.
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