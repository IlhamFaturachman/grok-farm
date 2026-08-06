#!/bin/bash
# Rotate VPNX exit IP between farm batches.
API="http://172.18.0.1:9090"
TOKEN="liam-vpnx-secret"

# Rotate to new VPN server
result=$(curl -s -X POST "$API/rotate" -H "Authorization: Bearer $TOKEN" --max-time 40 2>/dev/null)

# Check if status field contains "connected" (handles unicode)
if echo "$result" | grep -q "\"status\".*\"connected\""; then
  sleep 3
  ip=$(curl -s --socks5 vpnx:liam2026@172.18.0.1:1081 https://api.ipify.org --max-time 15 2>/dev/null || echo "FAILED")
  echo "[vpnx-rotate] $(date -u +%Y-%m-%dT%H:%M:%SZ) rotated exit_ip=$ip"
else
  echo "[vpnx-rotate] $(date -u +%Y-%m-%dT%H:%M:%SZ) rotate failed, reconnecting..."
  curl -s -X POST "$API/connect" -H "Authorization: Bearer $TOKEN" --max-time 30 2>/dev/null
  sleep 5
  ip=$(curl -s --socks5 vpnx:liam2026@172.18.0.1:1081 https://api.ipify.org --max-time 15 2>/dev/null || echo "FAILED")
  echo "[vpnx-rotate] $(date -u +%Y-%m-%dT%H:%M:%SZ) fallback exit_ip=$ip"
fi
