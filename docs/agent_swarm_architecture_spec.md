# Agent Swarm at the Edge

## Architecture Specification

*Version 0.11: MVP Scoped to L1 System Context Diagram*
*6 August 2026 · Max · On-premise / Edge*

| Gemma Model (Researcher, Architect, Judge) | LFM Model (Scribe, Critic) | Inference Engine |
|---|---|---|
| unsloth/gemma-4-E4B-it-qat-UD-Q4_K_XL ~2.5 GB RAM · ~14.8 tok/s confirmed | LiquidAI LFM2.5-VL-1.6B Q4_0 ~1.5 GB RAM · ~115 tok/s confirmed | llama-server router mode LRU hot-swap · 2 swaps per run |

---

## What Is This?

Agent Swarm at the Edge is a spec-driven, multi-agent architecture review pipeline that runs entirely on local CPU-only hardware, no cloud, no API keys, no data leaving the device.

It automates the discipline of solution architecture for solo developers, small engineering teams, and individual solution architects who know the practice matters but cannot sustain it without tooling support.

The output of one pipeline run: a C4 System Context diagram in Mermaid, an Architecture Decision Record triggered by any spec change, a structured gap critique, and a set of scored quality metrics, all requiring human approval before anything is persisted.

## The Problem

Most AI tooling today solves for code generation. But software engineering is not only about producing correct code, it also depends on early-stage artifacts that define what should be built, why, and what trade-offs were made.

The gap between technical teams and business stakeholders is not new. Business understands capabilities, costs, risks, and decisions. Engineers understand systems, services, and code. Architecture is the translation layer between them, but it is rarely practiced consistently, especially below the enterprise level.

The C4 model and Architecture Decision Records (ADRs) are the established tools for closing this gap. Most engineers have seen the Simon Brown C4 talk on YouTube. Most know ADRs exist. But there is no forcing function that makes the discipline low-friction enough to sustain, so diagrams go stale, ADRs never get written, and architecture decisions accumulate as undocumented institutional debt.

### Three Layers This Addresses

- **The gap:** Tech and business have always spoken different languages. C4 + ADRs are the professional bridge, but engineers skip them because they feel like overhead with no immediate payoff.
- **The lift:** AI handles the argument generation: critique, cost analysis, gap detection, diagram production. The engineer focuses on deciding, not documenting. Writing the spec is itself the skill-building.
- **The trust:** On-device SLMs mean sensitive architecture specs never leave the machine. Viable in enterprise, regulated, and air-gapped contexts where cloud LLMs are a non-starter.

## Who It Is For

### Primary Users

- Solo developers building real systems who want to think at architecture level but have no peer reviewer to challenge their design.
- Small teams (2–5 engineers) making architecture decisions in Slack threads that disappear, leaving new joiners with no context on why anything was built the way it was.
- Individual solution architects who need to produce defensible, professional architecture artifacts quickly and privately across multiple clients or projects.

### Secondary Users

- Business stakeholders and non-technical leads who need to understand architecture decisions without reading code. The C4 System Context diagram is designed for this audience.
- Engineers leveling up toward solution architect roles who need to internalize C4 + ADR discipline through practice, not just YouTube talks.

## Why Local SLMs?

Architecture specifications contain sensitive system design, infrastructure topology, and cost assumptions. These should not leave the device.

Running on CPU-only edge hardware means any engineer working in an air-gapped corporate environment, a regulated industry, a resource-constrained startup, or simply someone who values privacy can use this tool without cloud dependencies, API keys, or data egress.

The sequential pipeline design is a direct response to hardware constraints: one agent at a time, thermal management between steps, model hot-swap via LRU eviction. On more capable hardware, agents would run concurrently. The constraint shapes the architecture, which is itself an honest demonstration of the tool's purpose.

| Hardware | Detail |
|---|---|
| Device | ASUS ZenBook UX325EA |
| CPU | Intel Core i5-1135G7 (4 threads, powersave governor) |
| RAM | 16 GB total · ~7–8 GB available for workloads |
| GPU | Intel Iris Xe iGPU, no discrete GPU, CPU-only inference |
| OS | Ubuntu 26.04 LTS "resolute" |

## The Pipeline

Five agents run sequentially. Two models are used, grouped to minimise hot-swap overhead to two swaps per run.

| Agent | Model | Role |
|---|---|---|
| Researcher | Gemma 4 E4B QAT | Enriches the blackboard with context and Infracost cloud pricing data via tool call. |
| Architect | Gemma 4 E4B QAT | Generates C4 System Context diagram (L1) as Mermaid source. No tools, pure text generation. |
| Scribe | LFM2.5-VL-1.6B | Detects diff from prior spec version. Generates ADR: Context / Decision / Consequences. No tools. |
| Critic | LFM2.5-VL-1.6B | Reviews Architect output as devil's advocate, surfaces gaps, SPOFs, missing integrations. No tools. |
| Judge | Gemma 4 E4B QAT | Scores output against five quality metrics using a calculator tool. Flags items for human review. |

Model swap sequence: Gemma → LFM (after Architect) → Gemma (before Judge). Two swaps total per pipeline run.

## Human in the Loop

The pipeline is not fully automated by design. Every run pauses before persisting any artifact. The engineer reviews the consolidated output, C4 diagram, ADR, Critic gaps, Judge scores, and either approves or rejects with written comments.

- Approve → all outputs written to versioned artifact store in PostgreSQL
- Reject → comments written as revision notes, persisted for the record → engineer revises the spec and re-submits (automatic re-run from Critic is a planned v2 improvement)

This step is philosophically central, not a safety feature. The AI proposes. The engineer decides. The ADR records why. The decision trail belongs to the engineer, not the model.

## Architecture Quality Metrics

The Judge agent scores every run against five deterministic metrics using a calculator tool:

| Metric | Definition |
|---|---|
| spof_count | Number of single points of failure flagged by Critic |
| redundancy_ratio | Redundant components / total components (0–1) |
| cost_per_component | Total Infracost estimate / component count (USD) |
| integration_coverage | Defined integrations / required integration points from spec (0–1) |
| adrs_per_diff | ADRs generated / spec diff hunks (should be ≥ 1) |

## Technical Stack

| Layer | Technology | Role |
|---|---|---|
| Inference | llama.cpp build 9595 · llama-server | CPU-only local inference, router mode, LRU hot-swap |
| Orchestration | DBOS (dbos-transact-py) | Durable workflow execution over PostgreSQL, safe resume after thermal cutoff or OOM |
| State / Storage | PostgreSQL (port 5432) | DBOS workflow state, blackboard events, versioned artifact store |
| Observability | Arize Phoenix (port 6006) | Local LLM trace collection via OpenTelemetry / OpenInference |
| Pricing API | Infracost (Docker Engine, port 4000) | Self-hosted cloud pricing GraphQL API, no outbound dependency |
| Diagram Rendering | mermaid.ink (Docker Engine, port 3001) | Self-hosted Mermaid-to-PNG rendering, no outbound dependency |
| Validation | Pydantic v2 | Structured output validation for all agent responses and tool I/O |
| Diff Engine | deepdiff | Semantic spec diff on Pydantic model_dump() to trigger ADR generation |
| Language | Python 3.11+ (Anaconda venv) | Solo developer, Python-first, no TypeScript or Go components |

## C4 Diagram Roadmap

| Version | C4 Level | Diagram Type | Audience |
|---|---|---|---|
| MVP (v1) | L1: System Context | System + external actors and dependencies | Technical and non-technical stakeholders |
| v2 | L2: Container | Internal containers: services, DBs, APIs, queues | Engineers and architects |
| v3 | L3: Component | Internals of a selected container | Engineers |
| Out of scope | L4: Code | Class / implementation level | n/a |

## What Is Novel

- No public benchmarks exist for AI-assisted software architecture design or ADR generation, a 2026 survey of LLM benchmarks across the software development lifecycle found no public benchmarks for architectural design. The field is almost entirely code-generation focused.
- The few papers on C4 or ADR generation (2025–2026) treat it as a single-turn task. This is a spec-driven multi-agent pipeline with human-in-the-loop approval and iterative revision.
- No existing tool combines C4 diagram generation, automatic ADR triggering on spec diffs, devil's advocate critique, quality metric scoring, and human approval, all within a single local pipeline with no cloud dependency.
- Running this class of architecture reasoning pipeline on CPU-only edge hardware with SLMs challenges the assumption that frontier models and cloud APIs are required.

## Positioning

*Agent Swarm at the Edge is not an AI that replaces architects. It is a discipline scaffold that makes C4 + ADR practice accessible and sustainable for solo engineers and small teams who know the practice matters but cannot sustain it without tooling support, running entirely on their own hardware, with their architecture specs never leaving their machine.*

---

*Agent Swarm at the Edge · v0.11 · August 2026 · Max*
