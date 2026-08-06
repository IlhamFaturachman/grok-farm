#!/usr/bin/env bash
# Back-compat wrapper: rotate all VPNX instances via watchdog.
# Prefer scripts/vpnx_watchdog.sh directly.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export VPNX_UPDATE_ENV="${VPNX_UPDATE_ENV:-$ROOT/.env}"
export VPNX_KEEP_WARP="${VPNX_KEEP_WARP:-1}"
exec bash "$ROOT/scripts/vpnx_watchdog.sh" --rotate --require 1
