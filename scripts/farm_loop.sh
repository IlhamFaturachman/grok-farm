#!/usr/bin/env bash
# Continuous Grok browser farm loop (safe defaults for shared VPS).
# Runs as systemd unit farm-loop.service under user `farm`.
#
# Features:
#   - daily cap (default 50) then sleep until next local day
#   - random sleep between batches (min/max)
#   - fail streak → long cooldown
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -d "$ROOT/.venv" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

# Safe .env loader (handles spaces in values like Gmail App Passwords)
if [[ -f "$ROOT/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" != *"="* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    key="$(echo "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    val="$(echo "$val" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    # strip matching quotes
    if [[ "$val" =~ ^\".*\"$ ]]; then val="${val:1:${#val}-2}"; fi
    if [[ "$val" =~ ^\'.*\'$ ]]; then val="${val:1:${#val}-2}"; fi
    [[ -n "$key" ]] && export "$key=$val"
  done <"$ROOT/.env"
fi

# Defaults (override via .env)
BATCH_N="${FARM_LOOP_BATCH_N:-3}"
CONCURRENT="${FARM_LOOP_CONCURRENT:-1}"
SLEEP_OK="${FARM_LOOP_SLEEP_OK:-90}"
SLEEP_OK_MIN="${FARM_LOOP_SLEEP_OK_MIN:-180}"
SLEEP_OK_MAX="${FARM_LOOP_SLEEP_OK_MAX:-600}"
SLEEP_FAIL="${FARM_LOOP_SLEEP_FAIL:-300}"
MAX_FAIL_STREAK="${FARM_LOOP_MAX_FAIL_STREAK:-5}"
COOLDOWN_LONG="${FARM_LOOP_COOLDOWN_LONG:-1800}"
DAILY_CAP="${FARM_LOOP_DAILY_CAP:-50}"
DAY_TZ="${FARM_LOOP_DAY_TZ:-Asia/Jakarta}"

# If only fixed SLEEP_OK set and min/max not customized, allow fixed via min=max=SLEEP_OK
if [[ -n "${FARM_LOOP_SLEEP_OK:-}" && -z "${FARM_LOOP_SLEEP_OK_MIN:-}" && -z "${FARM_LOOP_SLEEP_OK_MAX:-}" ]]; then
  SLEEP_OK_MIN="$SLEEP_OK"
  SLEEP_OK_MAX="$SLEEP_OK"
fi
if [[ "$SLEEP_OK_MIN" -gt "$SLEEP_OK_MAX" ]]; then
  tmp="$SLEEP_OK_MIN"
  SLEEP_OK_MIN="$SLEEP_OK_MAX"
  SLEEP_OK_MAX="$tmp"
fi

export GROK_UI="${GROK_UI:-log}"
export GROK_HEADLESS="${GROK_HEADLESS:-true}"
export GROK_CONCURRENT="$CONCURRENT"
export GROK_MAX_ACCOUNTS="$BATCH_N"

mkdir -p "$ROOT/results"
LOG="$ROOT/results/farm_loop.log"
STATE="$ROOT/results/farm_loop_state.json"
DAILY_DIR="$ROOT/results"
fail_streak=0

log() {
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[$ts] $*" | tee -a "$LOG"
}

today_key() {
  TZ="$DAY_TZ" date +%Y-%m-%d
}

daily_file() {
  echo "$DAILY_DIR/farm_daily_$(today_key).json"
}

read_daily_created() {
  local f
  f="$(daily_file)"
  if [[ -f "$f" ]]; then
    python3 -c "import json;print(int(json.load(open('$f')).get('created',0)))" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

add_daily_created() {
  local n="$1"
  local f day
  day="$(today_key)"
  f="$(daily_file)"
  python3 - <<PY
import json
from pathlib import Path
from datetime import datetime, timezone
p = Path("$f")
data = {"day": "$day", "created": 0, "batches": 0, "tz": "$DAY_TZ"}
if p.is_file():
    try:
        data.update(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        pass
data["created"] = int(data.get("created") or 0) + int("$n")
data["batches"] = int(data.get("batches") or 0) + 1
data["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
data["cap"] = int("$DAILY_CAP")
p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(data["created"])
PY
}

seconds_until_next_day() {
  # seconds until next midnight in DAY_TZ
  python3 - <<PY
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
tz = ZoneInfo("$DAY_TZ")
now = datetime.now(tz)
nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
print(max(60, int((nxt - now).total_seconds())))
PY
}

random_sleep_ok() {
  local lo="$SLEEP_OK_MIN" hi="$SLEEP_OK_MAX" n
  if [[ "$lo" -eq "$hi" ]]; then
    n="$lo"
  else
    n="$(python3 -c "import random;print(random.randint($lo,$hi))")"
  fi
  log "sleep_ok random ${n}s (range ${lo}-${hi})"
  sleep "$n"
}

notify_msg() {
  if [[ -x "$ROOT/.venv/bin/python" ]] || command -v python3 >/dev/null; then
    local py="${ROOT}/.venv/bin/python"
    [[ -x "$py" ]] || py=python3
    NOTIFY_ENABLED=true "$py" - <<PY 2>/dev/null || true
from notify import dispatch, public_ip
text = """$*
IP: """ + public_ip()
dispatch(text, subject="Grok Farm loop")
PY
  fi
}

write_state() {
  local status="$1" created="$2" failed="$3" daily="$4"
  cat >"$STATE" <<EOF
{
  "status": "$status",
  "last_created": $created,
  "last_failed": $failed,
  "fail_streak": $fail_streak,
  "batch_n": $BATCH_N,
  "concurrent": $CONCURRENT,
  "daily_created": $daily,
  "daily_cap": $DAILY_CAP,
  "day_tz": "$DAY_TZ",
  "sleep_ok_min": $SLEEP_OK_MIN,
  "sleep_ok_max": $SLEEP_OK_MAX,
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
}

if [[ "$(id -u)" -eq 0 ]]; then
  log "ERROR: farm-loop must not run as root (Camoufox XPCOM)"
  exit 1
fi

log "farm-loop start batch_n=$BATCH_N concurrent=$CONCURRENT sleep_ok=${SLEEP_OK_MIN}-${SLEEP_OK_MAX}s sleep_fail=${SLEEP_FAIL}s daily_cap=$DAILY_CAP tz=$DAY_TZ"
notify_msg "🟢 Grok Farm LOOP started
batch=${BATCH_N} concurrent=${CONCURRENT}
daily_cap=${DAILY_CAP} tz=${DAY_TZ}
sleep_ok=${SLEEP_OK_MIN}-${SLEEP_OK_MAX}s
host=$(hostname)"

while true; do
  daily_now="$(read_daily_created)"
  if [[ "$daily_now" -ge "$DAILY_CAP" ]]; then
    wait_s="$(seconds_until_next_day)"
    log "DAILY CAP reached created=${daily_now}/${DAILY_CAP} — auto-stop until next day (${wait_s}s) tz=$DAY_TZ"
    write_state "daily_cap" 0 0 "$daily_now"
    notify_msg "⏸ Farm LOOP daily cap
${daily_now}/${DAILY_CAP} accounts today (${DAY_TZ})
sleeping ~$((wait_s / 3600))h until next day"
    sleep "$wait_s"
    fail_streak=0
    continue
  fi

  # Skip if another farm process already running
  if pgrep -f "python .*farm\\.py" >/dev/null 2>&1; then
    log "another farm.py running — sleep ${SLEEP_OK_MIN}s"
    sleep "$SLEEP_OK_MIN"
    continue
  fi

  remain=$((DAILY_CAP - daily_now))
  this_n="$BATCH_N"
  if [[ "$remain" -lt "$this_n" ]]; then
    this_n="$remain"
  fi
  if [[ "$this_n" -le 0 ]]; then
    continue
  fi

  export GROK_MAX_ACCOUNTS="$this_n"
  log "starting batch n=${this_n} c=${CONCURRENT} daily=${daily_now}/${DAILY_CAP}"
  set +e
  if command -v xvfb-run >/dev/null 2>&1 && [[ -z "${DISPLAY:-}" ]]; then
    xvfb-run -a python farm.py -n "$this_n" -c "$CONCURRENT" -y >>"$LOG" 2>&1
  else
    python farm.py -n "$this_n" -c "$CONCURRENT" -y >>"$LOG" 2>&1
  fi
  rc=$?
  set -e

  # Parse latest batch meta
  created=0
  failed=0
  latest="$(ls -1dt "$ROOT"/results/batch_* 2>/dev/null | head -1 || true)"
  if [[ -n "$latest" && -f "$latest/batch_meta.json" ]]; then
    created="$(python3 -c "import json;print(json.load(open('$latest/batch_meta.json')).get('created',0))" 2>/dev/null || echo 0)"
    failed="$(python3 -c "import json;print(json.load(open('$latest/batch_meta.json')).get('failed',0))" 2>/dev/null || echo 0)"
  fi

  log "batch done rc=$rc created=$created failed=$failed dir=${latest:-none}"

  if [[ "$created" -gt 0 ]]; then
    fail_streak=0
    daily_now="$(add_daily_created "$created")"
    write_state "ok" "$created" "$failed" "$daily_now"
    log "daily progress ${daily_now}/${DAILY_CAP}"
    if [[ "$daily_now" -ge "$DAILY_CAP" ]]; then
      wait_s="$(seconds_until_next_day)"
      log "DAILY CAP hit after batch — auto-stop ${wait_s}s"
      notify_msg "✅ Farm LOOP daily cap reached
${daily_now}/${DAILY_CAP} (${DAY_TZ})
auto-stop until next day"
      sleep "$wait_s"
      continue
    fi
  # Rotate WARP exit IP between batches for clean IP per batch
  bash "$ROOT/scripts/warp_rotate.sh" >>"$LOG" 2>&1 || true
    random_sleep_ok
  else
    fail_streak=$((fail_streak + 1))
    daily_now="$(read_daily_created)"
    write_state "fail" "$created" "$failed" "$daily_now"
    log "fail_streak=$fail_streak"
    if [[ "$fail_streak" -ge "$MAX_FAIL_STREAK" ]]; then
      notify_msg "🚨 Farm LOOP cooldown
${fail_streak} batches with 0 success
sleeping ${COOLDOWN_LONG}s — check IP / Turnstile
latest=${latest:-none}"
      fail_streak=0
      sleep "$COOLDOWN_LONG"
    else
      sleep "$SLEEP_FAIL"
    fi
  fi
done
