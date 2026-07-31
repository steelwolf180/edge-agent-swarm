#!/usr/bin/env bash
#
# thermal_monitor.sh — continuous CPU thermal sampler, independent of the
# DBOS pipeline's run_thermal_guard() step. That guard only samples at
# agent step boundaries (THERMAL_POLL_S=3, checked before each step), so it
# cannot see a spike that occurs and resolves mid-inference (e.g. the 94°C
# spike observed mid-Scribe on 22 Jul, or the suspected EC-level cutoff on
# the hard power-off run). This script polls independently on its own
# interval, for the full lifetime of the pipeline run, and writes a CSV you
# can later plot/correlate against pipeline_runs step timestamps.
#
# Usage:
#   ./thermal_monitor.sh &                # start in background
#   MONITOR_PID=$!
#   python pipeline/run.py --spec ...     # run the pipeline as normal
#   kill "$MONITOR_PID"                   # stop when the run is done
#
# Or just Ctrl+C it after the pipeline finishes if run in its own terminal.
#
# Output: logs/thermal_monitor_<timestamp>.csv
#   columns: epoch_s,iso8601,zone,label,temp_c
#
# Env overrides:
#   THERMAL_MONITOR_POLL_S   default 1 (finer than the in-pipeline guard's 3s
#                            on purpose — we're trying to catch what that
#                            guard misses)
#   THERMAL_MONITOR_LOGDIR   default ./logs

set -uo pipefail

POLL_S="${THERMAL_MONITOR_POLL_S:-1}"
LOGDIR="${THERMAL_MONITOR_LOGDIR:-./logs}"
mkdir -p "$LOGDIR"

RUN_TS="$(date +%Y%m%d_%H%M%S)"
OUTFILE="${LOGDIR}/thermal_monitor_${RUN_TS}.csv"
echo "epoch_s,iso8601,zone,label,temp_c" > "$OUTFILE"

echo "[thermal_monitor] logging every ${POLL_S}s to ${OUTFILE}"
echo "[thermal_monitor] PID $$ — kill this process to stop"

# Prefer `sensors -j` (coretemp, per-core labels) if lm-sensors is present;
# fall back to /sys/class/thermal/thermal_zone*/temp (single zone0 reading,
# which is what was noted in the 22 Jul incident as NOT showing the cutoff —
# keep both sources so we're not relying on the one signal that already
# failed to explain a crash once).
HAVE_SENSORS=0
if command -v sensors >/dev/null 2>&1; then
    HAVE_SENSORS=1
fi

trap 'echo "[thermal_monitor] stopped, wrote ${OUTFILE}"; exit 0' INT TERM

while true; do
    NOW_EPOCH="$(date +%s)"
    NOW_ISO="$(date -Iseconds)"

    if [ "$HAVE_SENSORS" -eq 1 ]; then
        # Parse `sensors -j` for coretemp-style entries. Falls through
        # silently to the thermal_zone reading below if jq isn't available
        # or the JSON shape doesn't match (don't let this crash the loop).
        if command -v jq >/dev/null 2>&1; then
            sensors -j 2>/dev/null | jq -r '
                to_entries[] | select(.key | test("coretemp|k10temp"; "i")) as $chip |
                $chip.value | to_entries[] |
                select(.value | type == "object") |
                .key as $label |
                (.value | to_entries[] | select(.key | test("_input$")) | .value) as $temp |
                "\($label),\($temp)"
            ' 2>/dev/null | while IFS=, read -r label temp; do
                [ -n "${label:-}" ] && [ -n "${temp:-}" ] && \
                    echo "${NOW_EPOCH},${NOW_ISO},sensors,${label},${temp}" >> "$OUTFILE"
            done
        fi
    fi

    # Always also log every thermal_zone under /sys, regardless of whether
    # sensors succeeded — cheap, and gives a second independent source.
    for zone in /sys/class/thermal/thermal_zone*; do
        [ -e "${zone}/temp" ] || continue
        ZONE_NAME="$(basename "$zone")"
        ZONE_TYPE="$(cat "${zone}/type" 2>/dev/null || echo unknown)"
        RAW_TEMP="$(cat "${zone}/temp" 2>/dev/null || echo "")"
        [ -z "$RAW_TEMP" ] && continue
        # /sys reports millidegrees C
        TEMP_C="$(awk -v t="$RAW_TEMP" 'BEGIN { printf "%.1f", t/1000 }')"
        echo "${NOW_EPOCH},${NOW_ISO},${ZONE_NAME},${ZONE_TYPE},${TEMP_C}" >> "$OUTFILE"
    done

    sleep "$POLL_S"
done