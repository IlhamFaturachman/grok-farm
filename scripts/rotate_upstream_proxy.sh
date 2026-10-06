#!/bin/bash
# Rotate grok2api upstream proxy through VPNX SOCKS5 pool
# Prevents xAI from seeing all requests from single IP
set -euo pipefail

PROXIES=(
  "socks5://172.18.0.1:1081"  # VPNX JP
  "socks5://172.18.0.1:1085"  # VPNX KR
  "socks5://172.18.0.1:1086"  # VPNX TH
  "socks5://172.18.0.1:1087"  # VPNX JP2
)

idx_file="/tmp/grok2api_proxy_idx"
if [[ -f "$idx_file" ]]; then
  idx=$(cat "$idx_file")
else
  idx=0
fi

proxy="${PROXIES[$((idx % ${#PROXIES[@]}))]}"
echo $(( (idx + 1) % ${#PROXIES[@]} )) > "$idx_file"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Rotating grok2api upstream proxy to: $proxy (idx=$idx)"

# Update docker container env and restart
cd /opt/grok-farm
export HTTPS_PROXY="$proxy"
export HTTP_PROXY="$proxy"
docker compose up -d grok2api 2>&1 | tail -3
echo "grok2api restarted with proxy: $proxy"
