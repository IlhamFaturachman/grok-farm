#!/bin/bash
set -euo pipefail
cd /opt/grok-farm
source .venv/bin/activate
set -a
source .env
set +a
export GROK_HEADLESS=true GROK_UI=log HOME=/home/farm
OUT="results/relogin_runs/pilot_$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
echo "OUT=$OUT"
exec python scripts/mass_relogin_g2a.py \
  --source-accounts results/relogin_candidates.jsonl \
  --priority reauth \
  --limit 20 \
  --concurrent 1 \
  --delay 8 \
  --retries 1 \
  --out-dir "$OUT" \
  2>&1 | tee "$OUT/console.log"
