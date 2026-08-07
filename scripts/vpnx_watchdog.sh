#!/usr/bin/env bash
# Heal all VPNX instances so farm proxies stay usable.
# VPN Gate free servers drop — reconnect aggressively so farm rarely starts dead.
#
# name   country  http   api    token
# vpnx   JP       8082   9090   liam-vpnx-secret
# vpnx-kr KR      8085   9093   liam-vpnx-kr
# vpnx-th TH      8086   9094   liam-vpnx-th
# vpnx-jp2 JP     8087   9095   liam-vpnx-jp2
#
# Usage:
#   vpnx_watchdog.sh              # heal all, exit 0 if >=1 healthy
#   vpnx_watchdog.sh --require N  # exit 1 if healthy < N
#   vpnx_watchdog.sh --rotate     # force rotate all instances
#   vpnx_watchdog.sh --rotate-one # rotate ONE container (round-robin) + heal rest
set -uo pipefail

REQUIRE=1
DO_ROTATE=0
ROTATE_ONE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --require) REQUIRE="${2:-1}"; shift 2 ;;
    --rotate) DO_ROTATE=1; shift ;;
    --rotate-one) ROTATE_ONE=1; shift ;;
    *) shift ;;
  esac
done

PROXY_USER="${VPNX_PROXY_USER:-vpnx}"
PROXY_PASS="${VPNX_PROXY_PASS:-liam2026}"
BRIDGE="${VPNX_BRIDGE:-172.18.0.1}"
TEST_URL="${VPNX_TEST_URL:-https://api.ipify.org}"
CURL_HTTP_MAX="${VPNX_CURL_HTTP_MAX:-8}"
CURL_API_MAX="${VPNX_CURL_API_MAX:-18}"
# VPS direct IPs — if proxy returns these, tunnel is dead (fallthrough)
# 103.150.61.32 = public VPS; 103.253.245.148 = observed bare egress
DIRECT_IPS="${VPNX_DIRECT_IPS:-103.150.61.32,103.253.245.148}"
INSTANCES=(
  "vpnx|JP|8082|9090|liam-vpnx-secret"
  "vpnx-kr|KR|8085|9093|liam-vpnx-kr"
  "vpnx-th|TH|8086|9094|liam-vpnx-th"
  "vpnx-jp2|JP|8087|9095|liam-vpnx-jp2"
)

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[vpnx-watchdog] $(ts) $*"; }

is_direct_ip() {
  local ip="$1"
  local d
  IFS=',' read -ra _dirs <<<"$DIRECT_IPS"
  for d in "${_dirs[@]}"; do
    d="${d// /}"
    [[ -n "$d" && "$ip" == "$d" ]] && return 0
  done
  return 1
}

check_http() {
  local port="$1"
  local ip
  ip=$(curl -s -x "http://${PROXY_USER}:${PROXY_PASS}@${BRIDGE}:${port}" \
    "$TEST_URL" --max-time "$CURL_HTTP_MAX" 2>/dev/null || true)
  if [[ -z "$ip" || "$ip" == "FAILED" ]] || is_direct_ip "$ip"; then
    echo ""
    return 1
  fi
  if [[ "$ip" =~ ^10\.|^192\.168\.|^172\.(1[6-9]|2[0-9]|3[0-1])\.|^127\. ]]; then
    echo ""
    return 1
  fi
  echo "$ip"
  return 0
}

ensure_container() {
  local name="$1"
  if ! docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$name"; then
    log "WARN $name container missing"
    return 1
  fi
  local st
  st=$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)
  if [[ "$st" != "running" ]]; then
    log "start $name (was $st)"
    timeout 15 docker start "$name" >/dev/null 2>&1 || true
    sleep 6
  fi
  return 0
}
api_post() {
  local port="$1" token="$2" path="$3" extra="${4:-}"
  curl -s -X POST "http://${BRIDGE}:${port}${path}${extra}" \
    -H "Authorization: Bearer ${token}" --max-time "$CURL_API_MAX" 2>/dev/null || true
}

heal_one() {
  local name="$1" country="$2" http="$3" api="$4" token="$5" force_rot="${6:-0}"
  local ip
  local do_rot=$(( DO_ROTATE + force_rot ))
  local cq="?country=${country}"

  ensure_container "$name" || return 1

  ip=$(check_http "$http" || true)
  if [[ -n "$ip" && "$do_rot" -eq 0 ]]; then
    log "OK $name/$country exit=$ip"
    echo "$ip"
    return 0
  fi

  if [[ -n "$ip" && "$do_rot" -ge 1 ]]; then
    local was_ip="$ip"
    log "rotate $name/$country (was $ip)"
    api_post "$api" "$token" "/rotate" "$cq" >/dev/null
    sleep 3
    ip=$(check_http "$http" || true)
    if [[ -n "$ip" ]]; then
      log "OK $name/$country rotated exit=$ip"
      echo "$ip"
      return 0
    fi
    # rotate-one / force-rotate of a previously-healthy tunnel: prefer restore
    # over full dead-cascade (connect×N + docker restart can burn 60s+ and leak direct IP).
    if [[ "$force_rot" -ge 1 ]]; then
      log "rotate-one failed $name/$country — reconnect keep-alive"
      api_post "$api" "$token" "/connect" "$cq" >/dev/null
      sleep 4
      ip=$(check_http "$http" || true)
      if [[ -n "$ip" ]]; then
        log "OK $name/$country restored exit=$ip (was $was_ip)"
        echo "$ip"
        return 0
      fi
      # fall through to normal heal only if restore also failed
      log "heal $name/$country (rotate-one restore failed)"
    fi
  else
    log "heal $name/$country (dead)"
  fi

  api_post "$api" "$token" "/connect" "$cq" >/dev/null
  sleep 4
  ip=$(check_http "$http" || true)
  if [[ -n "$ip" ]]; then
    log "OK $name/$country reconnected exit=$ip"
    echo "$ip"
    return 0
  fi

  api_post "$api" "$token" "/rotate" "$cq" >/dev/null
  sleep 4
  ip=$(check_http "$http" || true)
  if [[ -n "$ip" ]]; then
    log "OK $name/$country rotate-heal exit=$ip"
    echo "$ip"
    return 0
  fi

  # Last resort: connect without country filter (VPN Gate may have no TH/HK servers)
  api_post "$api" "$token" "/connect" >/dev/null
  sleep 4
  ip=$(check_http "$http" || true)
  if [[ -n "$ip" ]]; then
    log "OK $name/$country fallback-any exit=$ip"
    echo "$ip"
    return 0
  fi

  log "restart container $name"
  timeout 20 docker restart "$name" >/dev/null 2>&1 || true
  sleep 8
  api_post "$api" "$token" "/connect" "$cq" >/dev/null
  sleep 4
  ip=$(check_http "$http" || true)
  if [[ -n "$ip" ]]; then
    log "OK $name/$country after-restart exit=$ip"
    echo "$ip"
    return 0
  fi

  log "FAIL $name/$country still dead"
  echo ""
  return 1
}

heal_one_wrap() {
  local row="$1" outf="$2" force_rot="${3:-0}"
  IFS='|' read -r name country http api token <<<"$row"
  ip=$(heal_one "$name" "$country" "$http" "$api" "$token" "$force_rot" || true)
  if [[ -n "$ip" ]]; then
    echo "OK|$http|$ip|$name|$country" >"$outf"
  else
    echo "FAIL|$http||$name|$country" >"$outf"
  fi
}
# --rotate-one: pick ONE healthy container to rotate (round-robin), heal rest.
# Skip dead targets so we don't burn ~45-60s force-rotating a corpse.
ROTATE_IDX_FILE="/opt/grok-farm/.vpnx_rotate_idx"
if [[ "$ROTATE_ONE" -eq 1 ]]; then
  idx=0
  [[ -f "$ROTATE_IDX_FILE" ]] && idx=$(cat "$ROTATE_IDX_FILE" 2>/dev/null || echo 0)
  idx=$((idx % ${#INSTANCES[@]}))
  ROTATE_TARGET=-1
  for _try in $(seq 0 $(( ${#INSTANCES[@]} - 1 ))); do
    cand=$(( (idx + _try) % ${#INSTANCES[@]} ))
    IFS='|' read -r _n _c _http _a _t <<<"${INSTANCES[$cand]}"
    if check_http "$_http" >/dev/null 2>&1; then
      ROTATE_TARGET=$cand
      echo $(( (cand + 1) % ${#INSTANCES[@]} )) >"$ROTATE_IDX_FILE"
      log "rotate-one: target=${INSTANCES[$cand]%%|*} (idx=$cand, skipped_dead=${_try})"
      break
    fi
    log "rotate-one: skip dead ${INSTANCES[$cand]%%|*} (idx=$cand)"
  done
  if [[ "$ROTATE_TARGET" -lt 0 ]]; then
    # all dead — advance idx, heal-only this pass
    echo $(( (idx + 1) % ${#INSTANCES[@]} )) >"$ROTATE_IDX_FILE"
    log "rotate-one: all dead — heal only (idx advanced from $idx)"
  fi
else
  ROTATE_TARGET=-1
fi

healthy=0
healthy_ports=()
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

i=0
pids=()
for row in "${INSTANCES[@]}"; do
  outf="$tmpdir/r$i"
  force_rotate=0
  [[ "$ROTATE_ONE" -eq 1 && "$i" -eq "$ROTATE_TARGET" ]] && force_rotate=1
  heal_one_wrap "$row" "$outf" "$force_rotate" &
  pids+=($!)
  i=$((i + 1))
done

for pid in "${pids[@]}"; do
  wait "$pid" || true
done

for f in "$tmpdir"/r*; do
  [[ -f "$f" ]] || continue
  IFS='|' read -r st http ip name country <"$f"
  if [[ "$st" == "OK" && -n "$ip" ]]; then
    healthy=$((healthy + 1))
    healthy_ports+=("$http")
  fi
done

# Rewrite GROK_PROXY_POOL from healthy proxies only (+ optional WARP)
if [[ -n "${VPNX_UPDATE_ENV:-}" && -f "${VPNX_UPDATE_ENV}" && "$healthy" -gt 0 ]]; then
  pool=""
  for p in "${healthy_ports[@]}"; do
    entry="http://${PROXY_USER}:${PROXY_PASS}@${BRIDGE}:${p}"
    if [[ -z "$pool" ]]; then pool="$entry"; else pool="${pool},${entry}"; fi
  done
  if [[ "${VPNX_KEEP_WARP:-1}" == "1" ]]; then
    warp="socks5://127.0.0.1:1080"
    if curl -s -x "$warp" "$TEST_URL" --max-time 6 >/dev/null 2>&1; then
      pool="${warp},${pool}"
    fi
  fi
  if grep -q '^GROK_PROXY_POOL=' "$VPNX_UPDATE_ENV"; then
    sed -i "s|^GROK_PROXY_POOL=.*|GROK_PROXY_POOL=${pool}|" "$VPNX_UPDATE_ENV"
  else
    echo "GROK_PROXY_POOL=${pool}" >>"$VPNX_UPDATE_ENV"
  fi
  log "updated GROK_PROXY_POOL ($healthy healthy) -> $pool"
fi

log "summary healthy=${healthy}/4 require=${REQUIRE} ports=${healthy_ports[*]:-none}"
if [[ "$healthy" -lt "$REQUIRE" ]]; then
  exit 1
fi
exit 0
