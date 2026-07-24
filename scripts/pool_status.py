#!/usr/bin/env python3
"""Show farm disk counts + grok2api pool status."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = _ROOT / ".env"
    if not p.is_file():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    os.chdir(_ROOT)
    results = _ROOT / "results"
    batches = sorted(results.glob("batch_*"))
    total_ok = 0
    total_fail = 0
    emails: set[str] = set()
    for b in batches:
        acc = b / "accounts.json"
        fail = b / "failed.json"
        if acc.is_file():
            try:
                data = json.loads(acc.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    total_ok += len(data)
                    for row in data:
                        e = (row.get("email") or "").lower()
                        if e:
                            emails.add(e)
            except Exception:
                pass
        if fail.is_file():
            try:
                data = json.loads(fail.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    total_fail += len(data)
            except Exception:
                pass

    used = results / "used_emails.txt"
    used_n = 0
    if used.is_file():
        used_n = sum(1 for line in used.read_text(encoding="utf-8").splitlines() if line.strip())

    print("=== DISK (hasil farm) ===")
    print(f"batches           : {len(batches)}")
    print(f"accounts sukses   : {total_ok}")
    print(f"email unik        : {len(emails)}")
    print(f"failed rows       : {total_fail}")
    print(f"used_emails index : {used_n}")

    env = load_env()
    base = (env.get("GROK2API_URL") or "http://127.0.0.1:8000").rstrip("/")
    user = env.get("GROK2API_ADMIN_USER", "admin")
    password = env.get("GROK2API_ADMIN_PASS", "")
    if not password:
        print("=== G2A POOL ===")
        print("skip: no GROK2API_ADMIN_PASS")
        return 0

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

    items: list[dict] = []
    page = 1
    while page <= 50:
        req = Request(
            f"{base}/api/admin/v1/accounts?page={page}&pageSize=100",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            body = json.loads(urlopen(req, timeout=20).read())
        except Exception as e:
            print("accounts list error:", e)
            break
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        chunk = []
        if isinstance(data, dict):
            chunk = data.get("items") or data.get("accounts") or []
        elif isinstance(data, list):
            chunk = data
        elif isinstance(body.get("data"), list):
            chunk = body["data"]
        if not chunk:
            if page == 1:
                print("raw keys:", list(body.keys()) if isinstance(body, dict) else type(body))
                print("sample:", json.dumps(body)[:600])
            break
        items.extend([x for x in chunk if isinstance(x, dict)])
        total = data.get("total") if isinstance(data, dict) else None
        if total is not None and len(items) >= int(total):
            break
        if len(chunk) < 100:
            break
        page += 1

    status_count: dict[str, int] = {}
    active = 0
    reauth = 0
    for it in items:
        st = str(
            it.get("status")
            or it.get("state")
            or it.get("credentialStatus")
            or it.get("health")
            or "unknown"
        )
        status_count[st] = status_count.get(st, 0) + 1
        if it.get("reauthRequired") or it.get("reauth_required"):
            reauth += 1
        elif it.get("enabled", True) is not False:
            active += 1

    print("=== G2A POOL (yang dipakai API) ===")
    print(f"total di pool     : {len(items)}")
    print(f"aktif (heuristic) : {active}")
    print(f"butuh reauth      : {reauth}")
    print(f"status breakdown  : {status_count}")

    state = results / "farm_loop_state.json"
    if state.is_file():
        print("=== FARM LOOP ===")
        print(state.read_text(encoding="utf-8").strip())
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "farm-loop"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        print(f"farm-loop service : {r.stdout.strip() or r.stderr.strip()}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
