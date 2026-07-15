#!/usr/bin/env bash
# Run the HTTP-only Grok farmer (loads .venv + .env).
# No browser required — uses curl_cffi + external Turnstile solver.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  echo "Missing .venv — run ./install.sh first"
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if [[ ! -f .env ]]; then
  echo "Missing .env — cp .env.example .env && edit it"
  exit 1
fi

# Check curl_cffi is installed
python -c "import curl_cffi" 2>/dev/null || {
  echo "curl_cffi not installed — run: pip install curl_cffi"
  exit 1
}

exec python farm_http.py "$@"
