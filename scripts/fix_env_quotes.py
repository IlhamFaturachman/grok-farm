#!/usr/bin/env python3
from pathlib import Path
import sys

p = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/grok-farm/.env")
out = []
for line in p.read_text(encoding="utf-8").splitlines():
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        out.append(line)
        continue
    k, _, v = s.partition("=")
    k, v = k.strip(), v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        out.append(f"{k}={v}")
        continue
    if " " in v or any(c in v for c in "#$`\\"):
        v = '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    out.append(f"{k}={v}")
p.write_text("\n".join(out) + "\n", encoding="utf-8")
print("env_quoted_ok")
