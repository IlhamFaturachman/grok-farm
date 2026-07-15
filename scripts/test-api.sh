#!/usr/bin/env bash
# Quick test for the community API endpoint.
# Verifies that the API is reachable and your key works.
#
# Usage:
#   ./scripts/test-api.sh                          # prompts for key
#   GROK_API_KEY=sk-xxx ./scripts/test-api.sh      # key via env
set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
API_BASE="${GROK_API_BASE:-https://api.example.com}"

# ── Key ──────────────────────────────────────────────────────────────────────
if [[ -z "${GROK_API_KEY:-}" ]]; then
  read -r -p "Enter your API key: " GROK_API_KEY
fi

if [[ -z "$GROK_API_KEY" ]]; then
  echo "ERROR: no API key provided."
  exit 1
fi

echo "Testing $API_BASE ..."
echo

# ── 1. Health check ──────────────────────────────────────────────────────────
echo -n "[1/3] Health check ........... "
if HEALTH=$(curl -s -m 10 -w "%{http_code}" "$API_BASE/healthz" 2>/dev/null); then
  CODE="${HEALTH: -3}"
  BODY="${HEALTH%???}"
  if [[ "$CODE" == "200" ]]; then
    echo "OK ($BODY)"
  else
    echo "FAIL (HTTP $CODE)"
    exit 1
  fi
else
  echo "UNREACHABLE"
  exit 1
fi

# ── 2. List models ───────────────────────────────────────────────────────────
echo -n "[2/3] List models (auth) ..... "
MODELS=$(curl -s -m 15 -H "Authorization: Bearer $GROK_API_KEY" \
  "$API_BASE/v1/models" 2>/dev/null) || true
CODE=$(curl -s -m 15 -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $GROK_API_KEY" "$API_BASE/v1/models" 2>/dev/null || echo "000")

if [[ "$CODE" == "200" ]]; then
  # Extract model IDs (portable: works with or without jq)
  if command -v jq >/dev/null 2>&1; then
    COUNT=$(echo "$MODELS" | jq -r '.data[].id' 2>/dev/null | wc -l)
    echo "OK ($COUNT models)"
  else
    echo "OK"
  fi
elif [[ "$CODE" == "401" ]]; then
  echo "FAIL (invalid key — HTTP 401)"
  exit 1
else
  echo "FAIL (HTTP $CODE)"
  exit 1
fi

# ── 3. Chat completion ───────────────────────────────────────────────────────
echo -n "[3/3] Chat completion ........ "
RESP=$(curl -s -m 30 -w "\n%{http_code}" \
  -H "Authorization: Bearer $GROK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"grok-3","messages":[{"role":"user","content":"Say hi in one word"}]}' \
  "$API_BASE/v1/chat/completions" 2>/dev/null) || true
RCODE="${RESP##*$'\n'}"

if [[ "$RCODE" == "200" ]]; then
  echo "OK ✅"
  echo
  echo "Response:"
  if command -v jq >/dev/null 2>&1; then
    echo "$RESP" | head -n -1 | jq -r '.choices[0].message.content' 2>/dev/null \
      || echo "$RESP" | head -n -1
  else
    echo "$RESP" | head -n -1
  fi
else
  echo "FAIL (HTTP $RCODE)"
  echo "$RESP" | head -n -1
  exit 1
fi

echo
echo "All checks passed. Endpoint is ready to use:"
echo "  $API_BASE/v1/chat/completions"
