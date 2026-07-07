#!/usr/bin/env bash
# start_llama_router.sh — starts llama-server in router mode, logs to file
set -euo pipefail

PROJECT_DIR="$HOME/project/edge-agent-swarm"
BIN="$HOME/llama.cpp/build/bin/llama-server"
INI="$PROJECT_DIR/models.ini"
LOG="$PROJECT_DIR/logs/llama-router.log"
PORT=8080

mkdir -p "$(dirname "$LOG")"

if pgrep -f "llama-server --models-preset" >/dev/null; then
  echo "llama-server already running:"
  pgrep -af "llama-server --models-preset"
  exit 1
fi

echo "Starting llama-server router on port ${PORT}, logging to ${LOG}"
nohup "$BIN" --models-preset "$INI" --models-max 1 --port "$PORT" > "$LOG" 2>&1 &

echo "PID: $!"
sleep 2
curl -s "http://localhost:${PORT}/v1/models" | jq . || echo "not up yet — check ${LOG}"