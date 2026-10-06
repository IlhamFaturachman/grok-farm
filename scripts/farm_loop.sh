#!/usr/bin/env bash
# Continuous Grok browser farm loop — direct IP, random concurrent, smart retry.
#
# Strategy:
#   - Direct IP only (no WARP, no VPNX) — stable, no proxy failures
#   - Random concurrent (1-2) per batch — natural rhythm
#   - 8 custom domains round-robin + temp mail fallback on BFS
#   - Post-farm check: classify THINK (200) / NOTHINK (202) / BFS (403)
#   - THINK → chat pool (grok2api) + SSO cookie for image/video
#   - BFS/NOTHINK → image/video pool only (no_think flag)
#   - Failed batch → pause 10 min, retry
#   - BFS streak → switch to temp mail (mail.tm, guerrillamail, 1secmail)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -d "$ROOT/.venv" ]]; then
  source "$ROOT/.venv/bin/activate"
fi

# Safe .env loader
if [[ -f "$ROOT/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" != *"="* ]] && continue
    key="${line%%=*}"
    val="${line#*=}"
    key="$(echo "$key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    val="$(echo "$val" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [[ "$val" =~ ^\".*\"$ ]]; then val="${val:1:${#val}-2}"; fi
    if [[ "$val" =~ ^\'.*\'$ ]]; then val="${val:1:${#val}-2}"; fi
    [[ -n "$key" ]] && export "$key=$val"
  done <"$ROOT/.env"
fi

# ── Config ───────────────────────────────────────────────────────────────────
BATCH_N="${FARM_LOOP_BATCH_N:-5}"
SLEEP_OK_MIN="${FARM_LOOP_SLEEP_OK_MIN:-60}"
SLEEP_OK_MAX="${FARM_LOOP_SLEEP_OK_MAX:-180}"
SLEEP_FAIL="${FARM_LOOP_SLEEP_FAIL:-300}"        # 10 min on failure
SLEEP_BFS="${FARM_LOOP_SLEEP_BFS:-600}"           # 10 min on BFS detection
MAX_FAIL_STREAK="${FARM_LOOP_MAX_FAIL_STREAK:-5}"
COOLDOWN_LONG="${FARM_LOOP_COOLDOWN_LONG:-1800}"
DAILY_CAP="${FARM_LOOP_DAILY_CAP:-200}"
DAY_TZ="${FARM_LOOP_DAY_TZ:-Asia/Jakarta}"

export GROK_UI="${GROK_UI:-log}"
export GROK_HEADLESS="${GROK_HEADLESS:-true}"
# Direct IP — no proxy. Stable, no proxy connection failures.
export GROK_PROXY_POOL=""
# Don't reject BFS — we want to keep them for image/video pool
export GROK_REJECT_BFS=0

mkdir -p "$ROOT/results"
LOG="$ROOT/results/farm_loop.log"
STATE="$ROOT/results/farm_loop_state.json"
DAILY_DIR="$ROOT/results"
fail_streak=0
bfs_streak=0
canary_mode=0
# Temp mail rotation state — cycle through providers when BFS detected
temp_mail_mode=0
temp_mail_provider_idx=0
TEMP_MAIL_PROVIDERS=("mail_tm" "guerrillamail" "1secmail")

log() {
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[$ts] $*" | tee -a "$LOG"
}

today_key() { TZ="$DAY_TZ" date +%Y-%m-%d; }

daily_file() { echo "$DAILY_DIR/farm_daily_$(today_key).json"; }

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
  python3 - <<PY
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
tz = ZoneInfo("$DAY_TZ")
now = datetime.now(tz)
nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
print(max(60, int((nxt - now).total_seconds())))
PY
}

random_concurrent() {
  # Random 1 or 2 — natural rhythm, avoid looking like a bot
  echo 3
}

random_sleep_ok() {
  local lo="$SLEEP_OK_MIN" hi="$SLEEP_OK_MAX" n
  n="$(python3 -c "import random;print(random.randint($lo,$hi))")"
  log "sleep_ok random ${n}s (range ${lo}-${hi})"
  sleep "$n"
}

notify_msg() {
  local enabled="${NOTIFY_ENABLED:-false}"
  case "${enabled,,}" in
    1|true|yes|on) ;;
    *) return 0 ;;
  esac
  if [[ -x "$ROOT/.venv/bin/python" ]] || command -v python3 >/dev/null; then
    local py="${ROOT}/.venv/bin/python"
    [[ -x "$py" ]] || py=python3
    "$py" - <<PY 2>/dev/null || true
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
  "daily_created": $daily,
  "daily_cap": $DAILY_CAP,
  "day_tz": "$DAY_TZ",
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
}

if [[ "$(id -u)" -eq 0 ]]; then
  log "ERROR: farm-loop must not run as root (Camoufox XPCOM)"
  exit 1
fi

log "farm-loop start batch_n=$BATCH_N concurrent=3 sleep_ok=${SLEEP_OK_MIN}-${SLEEP_OK_MAX}s sleep_fail=${SLEEP_FAIL}s daily_cap=$DAILY_CAP tz=$DAY_TZ direct_ip=yes"
notify_msg "🟢 Grok Farm LOOP started
batch=${BATCH_N} concurrent=3
daily_cap=${DAILY_CAP} tz=${DAY_TZ}
direct_ip=yes (no WARP/VPNX)
sleep_fail=${SLEEP_FAIL}s"

while true; do
  daily_now="$(read_daily_created)"
  if [[ "$daily_now" -ge "$DAILY_CAP" ]]; then
    wait_s="$(seconds_until_next_day)"
    log "DAILY CAP reached created=${daily_now}/${DAILY_CAP} — auto-stop until next day (${wait_s}s) tz=$DAY_TZ"
    write_state "daily_cap" 0 0 "$daily_now"
    notify_msg "⏸ Farm LOOP daily cap
${daily_now}/${DAILY_CAP} accounts today (${DAY_TZ})"
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
  this_c="$(random_concurrent)"
  if [[ "$canary_mode" -eq 1 ]]; then
    this_n=1
    this_c=1
    log "canary_mode sticky — force n=1 c=1 until clean create"
  fi
  if [[ "$remain" -lt "$this_n" ]]; then
    this_n="$remain"
  fi
  if [[ "$this_n" -le 0 ]]; then
    continue
  fi

  # ── Email mode selection ──────────────────────────────────────────────────
  # Strategy: 8 custom domains round-robin first.
  # If BFS detected, switch to temp mail providers (round-robin).
  # Reset to custom domains after a clean THINK batch.
  if [[ "$temp_mail_mode" -eq 1 ]]; then
    provider="${TEMP_MAIL_PROVIDERS[$((temp_mail_provider_idx % ${#TEMP_MAIL_PROVIDERS[@]}))]}"
    export GROK_EMAIL_MODE="$provider"
    log "temp_mail_mode: using $provider (idx=$temp_mail_provider_idx)"
    temp_mail_provider_idx=$((temp_mail_provider_idx + 1))
  else
    # Custom domains — farm.py picks random from GROK_EMAIL_DOMAINS
    export GROK_EMAIL_MODE=domain
  fi

  export GROK_MAX_ACCOUNTS="$this_n"
  export GROK_CONCURRENT="$this_c"
  log "starting batch n=${this_n} c=${this_c} daily=${daily_now}/${DAILY_CAP} canary=${canary_mode} email_mode=${GROK_EMAIL_MODE} direct_ip=yes"
  set +e
  if command -v xvfb-run >/dev/null 2>&1 && [[ -z "${DISPLAY:-}" ]]; then
    xvfb-run -a python farm.py -n "$this_n" -c "$this_c" -y >>"$LOG" 2>&1
  else
    python farm.py -n "$this_n" -c "$this_c" -y >>"$LOG" 2>&1
  fi
  rc=$?
  set -e

  # Parse latest batch meta
  created=0
  failed=0
  bfs_fails=0
  latest="$(ls -1dt "$ROOT"/results/batch_* 2>/dev/null | head -1 || true)"
  if [[ -n "$latest" && -f "$latest/batch_meta.json" ]]; then
    created="$(python3 -c "import json;print(json.load(open('$latest/batch_meta.json')).get('created',0))" 2>/dev/null || echo 0)"
    failed="$(python3 -c "import json;print(json.load(open('$latest/batch_meta.json')).get('failed',0))" 2>/dev/null || echo 0)"
  fi
  if [[ -n "$latest" && -f "$latest/failed.json" ]]; then
    bfs_fails="$(python3 -c "
import json
rows=json.load(open('$latest/failed.json'))
n=0
for r in rows if isinstance(rows,list) else []:
    e=str((r or {}).get('error') or '')
    if 'bot-flagged' in e or 'bfs=1' in e or 'bot_flag_source' in e:
        n+=1
print(n)
" 2>/dev/null || echo 0)"
  fi

  log "batch done rc=$rc created=$created failed=$failed bfs_fails=$bfs_fails canary=${canary_mode} dir=${latest:-none}"

  # ── Post-farm account classification ──────────────────────────────────────
  # Check each newly created account against cli-chat-proxy.grok.com
  # Classify: THINK (200) / NOTHINK (202) / BFS (403) / other
  # Tag BFS/NOTHINK with no_think=true → image/video pool only
  think_count=0
  nothink_count=0
  bfs_count=0
  if [[ -n "$latest" && -f "$latest/accounts.json" ]]; then
    # Only check if accounts exist (created > 0)
    if [[ "$created" -gt 0 ]]; then
      log "post-farm check: classifying $created accounts in $latest"
      set +e
      python3 scripts/check_account.py --accounts "$latest/accounts.json" >>"$LOG" 2>&1
      check_rc=$?
      set -e
      think_count="$(python3 -c "
import json
with open('$latest/accounts.json') as f:
    data = json.load(f)
print(sum(1 for a in data if a.get('classification') == 'THINK'))
" 2>/dev/null || echo 0)"
      bfs_count="$(python3 -c "
import json
with open('$latest/accounts.json') as f:
    data = json.load(f)
print(sum(1 for a in data if a.get('classification') == 'BFS'))
" 2>/dev/null || echo 0)"
      nothink_count="$(python3 -c "
import json
with open('$latest/accounts.json') as f:
    data = json.load(f)
print(sum(1 for a in data if a.get('classification') == 'NOTHINK'))
" 2>/dev/null || echo 0)"
      log "post-farm: THINK=$think_count NOTHINK=$nothink_count BFS=$bfs_count"

      # ── Pool routing ──────────────────────────────────────────────────────
      # THINK accounts → import to grok2api chat pool + Etteum
      # BFS/NOTHINK → import to Etteum for image/video pool only
      if [[ "$think_count" -gt 0 ]]; then
        log "THINK accounts ($think_count) → chat pool (g2a auto-import) + Etteum"
      fi
      if [[ $((bfs_count + nothink_count)) -gt 0 ]]; then
        log "BFS/NOTHINK accounts ($((bfs_count + nothink_count))) → Etteum image/video pool (no_think flag)"
      fi
      # Import all accounts to Etteum (THINK for chat, BFS/NOTHINK for image/video)
      set +e
      python3 scripts/import_to_etteum.py --batch "$latest" >>"$LOG" 2>&1
      etteum_rc=$?
      set -e
      if [[ "$etteum_rc" -eq 0 ]]; then
        log "Etteum import done for $latest"
      else
        log "Etteum import failed (rc=$etteum_rc) — non-fatal, g2a still handles chat pool"
      fi
    fi

    # ── Email mode switching ────────────────────────────────────────────────
    # BFS detected → switch to temp mail for next batch
    if [[ "$bfs_count" -gt 0 && "$think_count" -eq 0 ]]; then
      bfs_streak=$((bfs_streak + 1))
      if [[ "$temp_mail_mode" -eq 0 ]]; then
        log "BFS detected (bfs=$bfs_count think=0) — switching to temp mail mode"
        temp_mail_mode=1
        temp_mail_provider_idx=0
      else
        log "BFS still detected with temp mail — rotating provider"
      fi
    elif [[ "$think_count" -gt 0 ]]; then
      # Clean THINK accounts → reset to custom domains
      if [[ "$temp_mail_mode" -eq 1 ]]; then
        log "Clean THINK with temp mail — resetting to custom domains"
        temp_mail_mode=0
      fi
      bfs_streak=0
    fi
  fi

  # ── Canary / BFS adaptive ────────────────────────────────────────────────
  total_attempts=$((created + failed))
  if [[ "$canary_mode" -eq 1 ]]; then
    if [[ "$created" -gt 0 ]]; then
      log "bfs canary CLEAN created=$created — exit canary, resume full batch"
      canary_mode=0
      bfs_streak=0
      fail_streak=0
      daily_now="$(add_daily_created "$created")"
      write_state "ok" "$created" "$failed" "$daily_now"
      notify_msg "✅ Farm LOOP bfs canary clean
created=$created — resume batch=${BATCH_N}"
      if [[ "$daily_now" -ge "$DAILY_CAP" ]]; then
        wait_s="$(seconds_until_next_day)"
        log "DAILY CAP hit after canary — auto-stop ${wait_s}s"
        sleep "$wait_s"
        continue
      fi
      random_sleep_ok
      continue
    fi
    # canary still bad — pause 10 min, stay sticky n=1
    log "bfs canary still bad created=0 — pause ${SLEEP_BFS}s, stay n=1"
    notify_msg "🚨 Farm LOOP bfs canary still bad
created=0 — stay n=1 after ${SLEEP_BFS}s pause"
    fail_streak=$((fail_streak + 1))
    write_state "canary_fail" 0 "$failed" "$(read_daily_created)"
    sleep "$SLEEP_BFS"
    continue
  fi

  if [[ "$total_attempts" -gt 0 && "$bfs_fails" -gt 0 && "$bfs_fails" -ge $(( (total_attempts + 1) / 2 )) ]]; then
    bfs_streak=$((bfs_streak + 1))
    log "bfs_streak=$bfs_streak (bfs_fails=$bfs_fails/${total_attempts})"
  else
    bfs_streak=0
  fi
  if [[ "$bfs_streak" -ge 2 ]]; then
    if [[ "$created" -gt 0 ]]; then
      fail_streak=0
      daily_now="$(add_daily_created "$created")"
      write_state "ok" "$created" "$failed" "$daily_now"
      log "daily progress ${daily_now}/${DAILY_CAP} (credited before canary enter)"
    fi
    canary_mode=1
    log "BFS enter canary_mode after ${bfs_streak} majority-bfs batches — pause ${SLEEP_BFS}s then n=1"
    notify_msg "⏸ Farm LOOP bfs pause → sticky canary
${bfs_streak} batches majority bot-flagged
sleep ${SLEEP_BFS}s then register 1 until clean"
    sleep "$SLEEP_BFS"
    continue
  fi

  # ── Success / failure handling ────────────────────────────────────────────
  if [[ "$created" -gt 0 ]]; then
    fail_streak=0
    daily_now="$(add_daily_created "$created")"
    write_state "ok" "$created" "$failed" "$daily_now"
    log "daily progress ${daily_now}/${DAILY_CAP}"
    if [[ "$daily_now" -ge "$DAILY_CAP" ]]; then
      wait_s="$(seconds_until_next_day)"
      log "DAILY CAP hit after batch — auto-stop ${wait_s}s"
      notify_msg "✅ Farm LOOP daily cap reached
${daily_now}/${DAILY_CAP} (${DAY_TZ})"
      sleep "$wait_s"
      continue
    fi
    random_sleep_ok
  else
    # Failed batch — pause 10 min
    fail_streak=$((fail_streak + 1))
    daily_now="$(read_daily_created)"
    write_state "fail" "$created" "$failed" "$daily_now"
    log "fail_streak=$fail_streak — pause ${SLEEP_FAIL}s (10 min)"
    if [[ "$fail_streak" -ge "$MAX_FAIL_STREAK" ]]; then
      notify_msg "🚨 Farm LOOP cooldown
${fail_streak} batches with 0 success
sleeping ${COOLDOWN_LONG}s — check Turnstile / IMAP"
      fail_streak=0
      sleep "$COOLDOWN_LONG"
    else
      sleep "$SLEEP_FAIL"
    fi
  fi
done
