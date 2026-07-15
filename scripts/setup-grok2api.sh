#!/usr/bin/env bash
# Generate docker/grok2api/config.yaml from the example (secrets filled in).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE="$ROOT/docker/grok2api/config.example.yaml"
DEST="$ROOT/docker/grok2api/config.yaml"

if [[ -f "$DEST" ]]; then
  echo "Already exists: $DEST (not overwriting)"
  exit 0
fi
if [[ ! -f "$EXAMPLE" ]]; then
  echo "Missing $EXAMPLE"
  exit 1
fi

JWT=$(openssl rand -hex 32 2>/dev/null || python -c "import secrets; print(secrets.token_hex(32))")
ENC=$(openssl rand -base64 32 2>/dev/null || python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())")
ADMIN_PASS=${GROK2API_ADMIN_PASS:-$(openssl rand -base64 18 2>/dev/null | tr -d '/+=' | head -c 20)}
ADMIN_USER=${GROK2API_ADMIN_USER:-admin}

cp "$EXAMPLE" "$DEST"
# portable-ish in-place replace
if command -v python3 >/dev/null 2>&1 || command -v python >/dev/null 2>&1; then
  PY=$(command -v python3 || command -v python)
  "$PY" - <<PY
from pathlib import Path
p = Path(r"""$DEST""")
t = p.read_text(encoding="utf-8")
t = t.replace("replace-with-at-least-32-characters", """$JWT""")
t = t.replace("replace-with-base64-key", """$ENC""")
t = t.replace("replace-with-a-strong-password", """$ADMIN_PASS""")
# bootstrap username
import re
t = re.sub(r'(bootstrapAdmin:\n(?:.*\n)*?\s*username:\s*)"[^"]*"',
           r'\1"$ADMIN_USER"'.replace("$ADMIN_USER", """$ADMIN_USER"""),
           t, count=1)
p.write_text(t, encoding="utf-8")
print("Wrote", p)
print("  admin user:", """$ADMIN_USER""")
print("  admin pass:", """$ADMIN_PASS""")
print("  (save this password — also set GROK2API_ADMIN_PASS in .env)")
PY
else
  echo "python required to fill secrets; copied bare example to $DEST"
fi

# Append hint to .env if present
ENVF="$ROOT/.env"
if [[ -f "$ENVF" ]] && ! grep -q '^GROK2API_URL=' "$ENVF" 2>/dev/null; then
  cat >> "$ENVF" <<EOF

# ── grok2api (auto-export / import) ──────────────────────────────────────────
GROK2API_URL=http://127.0.0.1:8000
GROK2API_ADMIN_USER=$ADMIN_USER
GROK2API_ADMIN_PASS=$ADMIN_PASS
GROK2API_EXPORT=true
GROK2API_AUTO_IMPORT=true
EOF
  echo "Appended GROK2API_* to .env"
fi

echo
echo "Next:"
echo "  docker compose up -d"
echo "  open http://127.0.0.1:8000  (login $ADMIN_USER)"
echo "  ./run.sh   # farm + auto-import when GROK2API_AUTO_IMPORT=true"
