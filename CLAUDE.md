# Agent Swarm at the Edge

## Reference docs

- @README.md
- @docs/KICKOFF_CHECKLIST.md
- @docs/agent_swarm_architecture_spec.md

## Environment

- conda env `swarm` (Python 3.11), CPU-only, ~7-8GB RAM ceiling
- llama-server must be running (router mode, port 8080) before any agent test
- PostgreSQL 18, native install (not Docker) — `pg_isready -h localhost -p 5432`
- pytest always targets TESTING_DATABASE_URL (enforced by conftest.py autouse fixture) — never the real DB

## Workflow rules

- One variable changed per run/fix — never stack fixes (see KICKOFF_CHECKLIST.md "one variable at a time")
- Detection-layer guards (agents/scribe.py) are defense-in-depth, not fixes to model behavior — don't conflate the two in commit messages or status updates
- Token budgets come from .env (RESEARCHER/ARCHITECT/SCRIBE/CRITIC_TOKEN_BUDGET) — no silent fallback if missing, raise loudly
- Agent outputs cross @DBOS.step() boundaries as `.model_dump(mode="json")`, not raw Pydantic instances

## Gotchas

- DBOS admin server defaults to port 3001, collides with mermaid-ink — admin_port is set to 3010 in DBOSConfig