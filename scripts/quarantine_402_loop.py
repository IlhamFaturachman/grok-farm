#!/usr/bin/env python3
"""Hard-delete grok2api accounts whose lastError contains 402 (spending-limit).

Keeps the rotation pool clean. Logs to results/deleted_402_auto.jsonl
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/grok-farm")
ENV_PATH = ROOT / ".env"
LOG_JSONL = ROOT / "results" / "deleted_402_auto.jsonl"
STATE = ROOT / "results" / "quarantine_402.json"
INTERVAL = int(os.environ.get("QUARANTINE_402_INTERVAL", "15"))


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def login(base: str, user: str, password: str) -> str:
    req = urllib.request.Request(
        f"{base}/api/admin/v1/auth/login",
        data=json.dumps({"username": user, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    body = json.loads(urllib.request.urlopen(req, timeout=20).read())
    return body["data"]["tokens"]["accessToken"]


def list_accounts(base: str, token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    items: list[dict] = []
    page = 1
    while page <= 80:
        req = urllib.request.Request(
            f"{base}/api/admin/v1/accounts?page={page}&pageSize=100",
            headers=headers,
        )
        body = json.loads(urllib.request.urlopen(req, timeout=30).read())
        data = body.get("data") or {}
        chunk = data.get("items") or []
        if not chunk:
            break
        items.extend(chunk)
        total = data.get("total")
        if total is not None and len(items) >= int(total):
            break
        if len(chunk) < 100:
            break
        page += 1
    return items


def delete_account(base: str, token: str, aid) -> bool:
    req = urllib.request.Request(
        f"{base}/api/admin/v1/accounts/{aid}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read() or b"{}")
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, dict) and data.get("deleted") is True:
                return True
            # some versions return 200 empty
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True
        raise


def append_log(rows: list[dict]) -> None:
    LOG_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with LOG_JSONL.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    # rolling summary
    prev: dict = {}
    if STATE.is_file():
        try:
            prev = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            prev = {}
    accs = prev.get("accounts") or []
    seen = {str(x.get("id")) for x in accs}
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for r in rows:
        if str(r.get("id")) in seen:
            continue
        accs.append(r)
        seen.add(str(r.get("id")))
    prev.update(
        {
            "at": now,
            "count": len(accs),
            "accounts": accs[-500:],
            "mode": "hard_delete",
            "watcher": "quarantine_402_loop",
        }
    )
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(prev, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE)


def cycle() -> int:
    env = load_env()
    base = (env.get("GROK2API_URL") or "http://127.0.0.1:8000").rstrip("/")
    user = env.get("GROK2API_ADMIN_USER") or "admin"
    password = env.get("GROK2API_ADMIN_PASS") or ""
    if not password:
        print("[quarantine402] no admin pass", flush=True)
        return 0
    token = login(base, user, password)
    items = list_accounts(base, token)
    targets = [a for a in items if "402" in (a.get("lastError") or "")]
    if not targets:
        return 0
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    done = []
    for a in targets:
        aid = a.get("id")
        email = a.get("email") or a.get("name")
        try:
            if delete_account(base, token, aid):
                print(f"[quarantine402] DELETED {aid} {email}", flush=True)
                done.append(
                    {
                        "id": str(aid),
                        "email": email,
                        "lastError": a.get("lastError"),
                        "reason": "auto hard-delete 402 spending-limit",
                        "deletedAt": now,
                        "action": "deleted",
                    }
                )
            else:
                print(f"[quarantine402] unexpected {aid}", flush=True)
        except Exception as e:
            print(f"[quarantine402] fail {aid}: {e}", flush=True)
        time.sleep(0.03)
    if done:
        append_log(done)
    return len(done)


def main() -> None:
    print(f"[quarantine402] start interval={INTERVAL}s mode=hard_delete", flush=True)
    while True:
        try:
            n = cycle()
            if n:
                print(f"[quarantine402] cycle deleted={n}", flush=True)
        except Exception as e:
            print(f"[quarantine402] cycle error: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
