#!/usr/bin/env bash
# smoke_test_llama.sh — checks a router-mode model responds, reports reasoning + tok/s
set -euo pipefail

MODEL="${1:-gemma}"
PORT="${2:-8080}"

echo "== Smoke testing '${MODEL}' on port ${PORT} =="

RESPONSE=$(curl -s http://localhost:${PORT}/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"${MODEL}\",
    \"messages\": [{\"role\": \"user\", \"content\": \"Reply with exactly: OK\"}],
    \"max_tokens\": 20,
    \"timings_per_token\": true
  }")

echo "$RESPONSE" | jq .

echo ""
echo "-- reasoning_content check --"
if echo "$RESPONSE" | jq -e '.choices[0].message.reasoning_content // empty' >/dev/null 2>&1; then
  echo "⚠️  reasoning_content present — reasoning NOT off"
else
  echo "✅ no reasoning_content — reasoning off confirmed"
fi

echo ""
echo "-- timings --"
echo "$RESPONSE" | jq '.timings // "no timings block — check /slots endpoint instead"'