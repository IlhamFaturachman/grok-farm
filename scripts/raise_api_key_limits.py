#!/usr/bin/env python3
"""Raise grok2api client key rpm/concurrent limits."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = _ROOT / ".env"
    if not p.is_file():
        p = Path("/opt/grok-farm/.env")
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    env = load_env()
    base = (env.get("GROK2API_URL") or "http://127.0.0.1:8000").rstrip("/")
    user = env.get("GROK2API_ADMIN_USER", "admin")
    password = env.get("GROK2API_ADMIN_PASS", "")
    rpm = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    conc = int(sys.argv[2]) if len(sys.argv) > 2 else 16
    if not password:
        print("no admin pass")
        return 1

    login = json.loads(
        urlopen(
            Request(
                f"{base}/api/admin/v1/auth/login",
                data=json.dumps({"username": user, "password": password}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=15,
        ).read()
    )
    token = login["data"]["tokens"]["accessToken"]
    h = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    keys = json.loads(
        urlopen(Request(f"{base}/api/admin/v1/client-keys", headers=h), timeout=15).read()
    )
    items = keys.get("data", {}).get("items") or []
    print("before:", json.dumps(items, indent=2)[:1000])
    if not items:
        print("no keys")
        return 2
    kid = str(items[0]["id"])
    bodies = [
        {"rpmLimit": rpm, "maxConcurrent": conc},
        {
            "name": items[0].get("name") or "opencode-farm",
            "enabled": True,
            "rpmLimit": rpm,
            "maxConcurrent": conc,
        },
    ]
    ok = False
    for method in ("PATCH", "PUT"):
        for body in bodies:
            try:
                req = Request(
                    f"{base}/api/admin/v1/client-keys/{kid}",
                    data=json.dumps(body).encode(),
                    headers=h,
                    method=method,
                )
                with urlopen(req, timeout=15) as r:
                    print("ok", method, r.read()[:400])
                    ok = True
                    break
            except HTTPError as e:
                print("fail", method, e.code, e.read()[:200])
        if ok:
            break
    keys2 = json.loads(
        urlopen(Request(f"{base}/api/admin/v1/client-keys", headers=h), timeout=15).read()
    )
    print("after:", json.dumps(keys2.get("data", {}).get("items"), indent=2)[:1000])
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
