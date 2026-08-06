#!/bin/bash
# Rotate VPNX exit IP between farm batches.
# Uses HTTP proxy for verification (SOCKS5 auth incompatible with curl/browser).
API="http://172.18.0.1:9090"
TOKEN="liam-vpnx-secret"
HTTP_PROXY="http://vpnx:liam2026@172.18.0.1:8082"

# Rotate to new VPN server
result=$(curl -s -X POST "$API/rotate" -H "Authorization: Bearer $TOKEN" --max-time 40 2>/dev/null)

if echo "$result" | grep -q "\"status\".*\"connected\""; then
  sleep 3
  ip=$(curl -s -x "$HTTP_PROXY" https://api.ipify.org --max-time 15 2>/dev/null || echo "FAILED")
  echo "[vpnx-rotate] $(date -u +%Y-%m-%dT%H:%M:%SZ) rotated exit_ip=$ip"
else
  echo "[vpnx-rotate] $(date -u +%Y-%m-%dT%H:%M:%SZ) rotate failed, reconnecting..."
  curl -s -X POST "$API/connect" -H "Authorization: Bearer $TOKEN" --max-time 30 2>/dev/null >/dev/null
  sleep 5
  ip=$(curl -s -x "$HTTP_PROXY" https://api.ipify.org --max-time 15 2>/dev/null || echo "FAILED")
  echo "[vpnx-rotate] $(date -u +%Y-%m-%dT%H:%M:%SZ) fallback exit_ip=$ip"
fi
