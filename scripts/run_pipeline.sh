#!/usr/bin/env bash
#
# scripts/run_pipeline.sh
#
# Wraps `python -m pipeline.run` with mandatory cleanup of stale PENDING
# DBOS workflows before AND after every run.
#
# WHY THIS EXISTS: DBOS auto-recovers PENDING workflows on DBOS.launch() —
# correct behavior for production crash-recovery, but a liability during
# dev iteration where runs get Ctrl-C'd or the process dies mid-pipeline.
# A recovered leftover workflow running concurrently with a fresh one
# doubles RAM/CPU load on the same llama-server instance (both compete for
# the same --threads 4 and the same ~15GB RAM), which measurably degrades
# token/sec on both requests and can push the ZenBook into real OOM
# territory. That double-execution has been the actual root cause behind
# at least one laptop crash and one "ReadTimeout" failure, not just
# wasted compute — this isn't just tidiness, it's meant to remove the most
# likely mechanism putting extra strain on the machine during dev runs.
#
# CAVEAT: the EXIT trap below only fires for a normal process exit,
# Ctrl-C (SIGINT), or an unhandled exception. It CANNOT run if the OS
# OOM-killer sends SIGKILL, or the machine hard-locks — in that case the
# pre-run cleanup at the top of the *next* invocation is what actually
# saves you. Don't treat this script as a substitute for keeping an eye on
# `free -h` during a run; it closes the "forgot to clean up" gap, not the
# "the machine actually died" one.
#
# Usage:
#   ./scripts/run_pipeline.sh --spec tests/smoke/fixtures/spec_smoke.json

set -uo pipefail

# Resolve repo root regardless of where this script is invoked from.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ ! -f .env ]; then
  echo "ERROR: .env not found in $(pwd). Run this script from within the repo." >&2
  exit 1
fi

set -a
source .env
set +a

if [ -z "${DBOS_SYSTEM_DATABASE_URL:-}" ]; then
  echo "ERROR: DBOS_SYSTEM_DATABASE_URL not set in .env" >&2
  exit 1
fi

clear_pending_workflows() {
  echo "[cleanup] Clearing any stale PENDING DBOS workflows..."
  if ! psql "$DBOS_SYSTEM_DATABASE_URL" -t -c \
      "DELETE FROM dbos.workflow_status WHERE status = 'PENDING';" 2>/dev/null; then
    echo "[cleanup] WARNING: could not reach Postgres to clear PENDING workflows." >&2
    echo "[cleanup]          Check it's running: pg_isready -h localhost -p 5432" >&2
  fi
}

# Runs on normal exit, Ctrl-C, or script error — NOT on SIGKILL/hard crash.
# See CAVEAT above.
trap clear_pending_workflows EXIT

clear_pending_workflows

echo "[preflight] Checking Postgres..."
if ! pg_isready -h localhost -p 5432 > /dev/null 2>&1; then
  echo "ERROR: Postgres not responding on :5432." >&2
  exit 1
fi

echo "[preflight] Checking llama-server..."
if ! curl -sf http://localhost:8080/v1/models > /dev/null; then
  echo "ERROR: llama-server not responding on :8080." >&2
  echo "        Start it: ./scripts/start_llama_router.sh" >&2
  exit 1
fi

echo "[run] Starting pipeline..."
python -m pipeline.run "$@"
STATUS=$?

echo "[run] Pipeline exited with status $STATUS"
exit $STATUS
