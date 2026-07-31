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

# THERMAL_MONITOR_PID is unset until the monitor is actually started further
# down — guarded so this is a no-op if we exit during preflight, before the
# monitor ever launches.
stop_thermal_monitor() {
  if [ -n "${THERMAL_MONITOR_PID:-}" ]; then
    echo "[cleanup] Stopping thermal monitor (pid $THERMAL_MONITOR_PID)..."
    kill "$THERMAL_MONITOR_PID" 2>/dev/null
  fi
}

# Runs on normal exit, Ctrl-C, or script error — NOT on SIGKILL/hard crash.
# See CAVEAT above. Monitor stop runs first so cleanup doesn't leave a
# dangling background process even if the Postgres cleanup below fails.
trap 'stop_thermal_monitor; clear_pending_workflows' EXIT

clear_pending_workflows

# ---------------------------------------------------------------------------
# Thermal preflight — separate from the in-workflow @DBOS.step() thermal
# guard in pipeline/run.py. That guard checks temp BETWEEN agent steps
# during a run; it has no way to know how hot the machine already was
# BEFORE the run started. A run kicked off back-to-back with a prior one
# (or right after any other CPU-heavy work) can start from an elevated
# baseline, eating into the margin the in-workflow guard assumes it has.
# This check refuses to launch DBOS at all until the machine has cooled,
# rather than starting Researcher on top of residual heat from last time.
#
# Uses its own THERMAL_PREFLIGHT_* vars (not THERMAL_MAX_C etc., which
# govern the in-workflow guard) so the two can be tuned independently —
# e.g. a stricter preflight bar than the in-run threshold, since there's
# no cost to waiting a bit longer before you've even started.
# ---------------------------------------------------------------------------

for var in THERMAL_PREFLIGHT_MAX_C THERMAL_PREFLIGHT_POLL_S THERMAL_PREFLIGHT_TIMEOUT_S; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: $var not set in .env — thermal preflight has no silent fallback," >&2
    echo "        same as THERMAL_MAX_C etc. Set it explicitly." >&2
    exit 1
  fi
done

read_cpu_temp_c() {
  local out
  out=$(sensors -u 2>/dev/null) || return 1

  local temp
  temp=$(echo "$out" | awk '
    /Package id 0:/ { want=1; next }
    want && /temp[0-9]+_input:/ { print $2; exit }
  ')
  if [ -z "$temp" ]; then
    temp=$(echo "$out" | awk '
      /Tctl:/ { want=1; next }
      want && /temp[0-9]+_input:/ { print $2; exit }
    ')
  fi
  if [ -z "$temp" ]; then
    temp=$(echo "$out" | grep -oE 'temp[0-9]+_input:[[:space:]]*[0-9.]+' \
      | grep -oE '[0-9.]+$' | sort -rn | head -1)
  fi
  [ -n "$temp" ] || return 1
  echo "$temp"
}

echo "[preflight] Checking CPU temp is below ${THERMAL_PREFLIGHT_MAX_C}°C before starting..."
temp="$(read_cpu_temp_c)" || {
  echo "[preflight] WARNING: could not read sensors — skipping thermal preflight check." >&2
  temp=""
}

if [ -n "$temp" ]; then
  elapsed=0
  while awk "BEGIN{exit !($temp >= $THERMAL_PREFLIGHT_MAX_C)}"; do
    if [ "$elapsed" -ge "$THERMAL_PREFLIGHT_TIMEOUT_S" ]; then
      echo "ERROR: CPU still at ${temp}°C after ${THERMAL_PREFLIGHT_TIMEOUT_S}s wait" >&2
      echo "       (limit ${THERMAL_PREFLIGHT_MAX_C}°C). Machine has not cooled from a" >&2
      echo "       prior run. Refusing to start rather than begin Researcher on top of" >&2
      echo "       residual heat. Wait longer, or lower THERMAL_PREFLIGHT_MAX_C if this" >&2
      echo "       baseline is expected on this machine." >&2
      exit 1
    fi
    echo "[preflight] CPU at ${temp}°C >= ${THERMAL_PREFLIGHT_MAX_C}°C, waiting ${THERMAL_PREFLIGHT_POLL_S}s..."
    sleep "$THERMAL_PREFLIGHT_POLL_S"
    elapsed=$((elapsed + THERMAL_PREFLIGHT_POLL_S))
    temp="$(read_cpu_temp_c)" || { echo "[preflight] WARNING: sensors read failed mid-wait, proceeding." >&2; break; }
  done
  echo "[preflight] CPU at ${temp}°C, OK to proceed."
fi

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

THERMAL_MONITOR_SCRIPT="$(dirname "${BASH_SOURCE[0]}")/thermal_monitor.sh"
if [ -x "$THERMAL_MONITOR_SCRIPT" ]; then
  echo "[run] Starting continuous thermal monitor..."
  "$THERMAL_MONITOR_SCRIPT" &
  THERMAL_MONITOR_PID=$!
else
  echo "[run] WARNING: thermal_monitor.sh not found or not executable at $THERMAL_MONITOR_SCRIPT" >&2
  echo "[run]          — proceeding without continuous monitoring." >&2
fi

echo "[run] Starting pipeline..."
python -m pipeline.run "$@"
STATUS=$?

echo "[run] Pipeline exited with status $STATUS"
exit $STATUS
