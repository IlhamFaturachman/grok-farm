#!/usr/bin/env bash
# Heal all VPNX instances so farm proxies stay usable.
# VPN Gate free servers drop — reconnect aggressively so farm rarely starts dead.
#
# name   country  http   api    token
# vpnx   JP       8082   9090   liam-vpnx-secret
# vpnx-kr KR      8085   9093   liam-vpnx-kr
# vpnx-th TH      8086   9094   liam-vpnx-th
# vpnx-hk HK      8087   9095   liam-vpnx-hk
#
# Usage:
#   vpnx_watchdog.sh              # heal all, exit 0 if >=1 healthy
#   vpnx_watchdog.sh --require N  # exit 1 if healthy < N
#   vpnx_watchdog.sh --rotate     # also rotate healthy ones (new exit IP)
set -uo pipefail

REQUIRE=1
DO_ROTATE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --require) REQUIRE="${2:-1}"; shift 2 ;;
    --rotate) DO_ROTATE=1; shift ;;
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
  "vpnx-hk|HK|8087|9095|liam-vpnx-hk"
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
  local port="$1" token="$2" path="$3"
  curl -s -X POST "http://${BRIDGE}:${port}${path}" \
    -H "Authorization: Bearer ${token}" --max-time "$CURL_API_MAX" 2>/dev/null || true
}

heal_one() {
  local name="$1" country="$2" http="$3" api="$4" token="$5"
  local ip

  ensure_container "$name" || return 1

  ip=$(check_http "$http" || true)
  if [[ -n "$ip" && "$DO_ROTATE" -eq 0 ]]; then
    log "OK $name/$country exit=$ip"
    echo "$ip"
    return 0
  fi

  if [[ -n "$ip" && "$DO_ROTATE" -eq 1 ]]; then
    log "rotate $name/$country (was $ip)"
    api_post "$api" "$token" "/rotate" >/dev/null
    sleep 3
    ip=$(check_http "$http" || true)
    if [[ -n "$ip" ]]; then
      log "OK $name/$country rotated exit=$ip"
      echo "$ip"
      return 0
    fi
  else
    log "heal $name/$country (dead)"
  fi

  api_post "$api" "$token" "/connect" >/dev/null
  sleep 4
  ip=$(check_http "$http" || true)
  if [[ -n "$ip" ]]; then
    log "OK $name/$country reconnected exit=$ip"
    echo "$ip"
    return 0
  fi

  api_post "$api" "$token" "/rotate" >/dev/null
  sleep 4
  ip=$(check_http "$http" || true)
  if [[ -n "$ip" ]]; then
    log "OK $name/$country rotate-heal exit=$ip"
    echo "$ip"
    return 0
  fi

  log "restart container $name"
  timeout 20 docker restart "$name" >/dev/null 2>&1 || true
  sleep 8
  api_post "$api" "$token" "/connect" >/dev/null
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
  local row="$1" outf="$2"
  IFS='|' read -r name country http api token <<<"$row"
  ip=$(heal_one "$name" "$country" "$http" "$api" "$token" || true)
  if [[ -n "$ip" ]]; then
    echo "OK|$http|$ip|$name|$country" >"$outf"
  else
    echo "FAIL|$http||$name|$country" >"$outf"
  fi
}

healthy=0
healthy_ports=()
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

i=0
pids=()
for row in "${INSTANCES[@]}"; do
  outf="$tmpdir/r$i"
  heal_one_wrap "$row" "$outf" &
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
