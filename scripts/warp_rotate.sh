#!/bin/bash
# Rotate WARP exit IP — called between farm batches.
# ~12s cycle: set endpoint → disconnect → reconnect → reset → disconnect → reconnect
LOG_TAG="warp-rotate"
ENDPOINTS=("162.159.192.1:2408" "162.159.192.7:2408" "162.159.192.14:2408" "162.159.192.21:2408" "188.114.96.1:2408" "188.114.96.7:2408" "188.114.97.1:2408" "188.114.97.7:2408")
ep="${ENDPOINTS[$RANDOM % ${#ENDPOINTS[@]}]}"
docker exec warp warp-cli tunnel endpoint set "$ep" 2>/dev/null || true
docker exec warp warp-cli disconnect 2>/dev/null || true
sleep 2
docker exec warp warp-cli connect 2>/dev/null || true
sleep 3
docker exec warp warp-cli tunnel endpoint reset 2>/dev/null || true
docker exec warp warp-cli disconnect 2>/dev/null || true
sleep 2
docker exec warp warp-cli connect 2>/dev/null || true
sleep 5
ip=$(curl -s --socks5-hostname 127.0.0.1:1080 https://api.ipify.org --max-time 10 2>/dev/null || echo "FAILED")
echo "[$LOG_TAG] endpoint=$ep exit_ip=$ip"
