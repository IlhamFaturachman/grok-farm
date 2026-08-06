#!/bin/bash
# WARP IP Rotator — cycles WARP exit IP every N minutes.
# Mitigates xAI IP flagging by rotating Cloudflare edge exit IP.
# Downtime per cycle: ~8-10s (gateway fallback=direct covers the gap).
set -e

LOG_TAG="warp-rotator"

# Cloudflare WARP endpoint candidates (different Anycast IPs → different exit IPs)
ENDPOINTS=(
  "162.159.192.1:2408"
  "162.159.192.7:2408"
  "162.159.192.14:2408"
  "162.159.192.21:2408"
  "188.114.96.1:2408"
  "188.114.96.7:2408"
  "188.114.97.1:2408"
  "188.114.97.7:2408"
)

rotate_warp() {
  # Pick a random endpoint
  local ep="${ENDPOINTS[$RANDOM % ${#ENDPOINTS[@]}]}"
  echo "[$LOG_TAG] $(date -u +%Y-%m-%dT%H:%M:%SZ) rotating via endpoint $ep"

  # Set custom endpoint
  docker exec warp warp-cli tunnel endpoint set "$ep" 2>/dev/null || true
  docker exec warp warp-cli disconnect 2>/dev/null || true
  sleep 2
  docker exec warp warp-cli connect 2>/dev/null || true
  sleep 3

  # Reset to default endpoint (this triggers new IP assignment)
  docker exec warp warp-cli tunnel endpoint reset 2>/dev/null || true
  docker exec warp warp-cli disconnect 2>/dev/null || true
  sleep 2
  docker exec warp warp-cli connect 2>/dev/null || true
  sleep 5

  # Verify
  local ip
  ip=$(curl -s --socks5-hostname 127.0.0.1:1080 https://api.ipify.org --max-time 10 2>/dev/null || echo "FAILED")
  echo "[$LOG_TAG] new exit IP: $ip"

  # If WARP failed to reconnect, try once more
  if [ "$ip" = "FAILED" ] || [ -z "$ip" ]; then
    echo "[$LOG_TAG] WARP reconnect failed, retrying..."
    docker exec warp warp-cli connect 2>/dev/null || true
    sleep 8
    ip=$(curl -s --socks5-hostname 127.0.0.1:1080 https://api.ipify.org --max-time 10 2>/dev/null || echo "STILL-FAILED")
    echo "[$LOG_TAG] retry exit IP: $ip"
  fi
}

rotate_warp
