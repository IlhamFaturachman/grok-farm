#!/usr/bin/env bash
# Open an SSH tunnel to reach grok2api admin panel + traffic-intel dashboard
# on your local machine. Both services are locked to 127.0.0.1 on the server.
#
# While this is running, open in your browser:
#   Admin panel  →  http://localhost:8000
#   Intel dashboard  →  http://localhost:8001
#
# Close the SSH session (Ctrl-C or `kill %1`) to tear down.
set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
SERVER="${TUNNEL_SERVER:-user@your-server.example.com}"
ADMIN_PORT="${TUNNEL_ADMIN_PORT:-8000}"
INTEL_PORT="${TUNNEL_INTEL_PORT:-8001}"

# ── Pre-flight ───────────────────────────────────────────────────────────────
if ! command -v ssh >/dev/null 2>&1; then
  echo "ERROR: ssh not found."
  exit 1
fi

# If local port is busy, pick a reminder so the URL isn't a surprise.
for p in "$ADMIN_PORT" "$INTEL_PORT"; do
  if command -v ss >/dev/null 2>&1 && ss -tlnH "sport = :$p" 2>/dev/null | grep -q .; then
    echo "WARN: local port $p is already in use (admin/intel may fail to forward)"
  fi
done

echo "╔══════════════════════════════════════════════════════╗"
echo "║  SSH tunnel — grok2api admin + intel                ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Admin panel   →  http://localhost:$ADMIN_PORT            ║"
echo "║  Intel dash    →  http://localhost:$INTEL_PORT            ║"
echo "║                                                      ║"
echo "║  Ctrl-C to close the tunnel.                         ║"
echo "╚══════════════════════════════════════════════════════╝"
echo

# -N  no remote command (just forward ports)
# -f  background after connecting (uncomment -f below to daemonise)
exec ssh -N \
  -L "${ADMIN_PORT}:127.0.0.1:8000" \
  -L "${INTEL_PORT}:127.0.0.1:8001" \
  "$SERVER"
