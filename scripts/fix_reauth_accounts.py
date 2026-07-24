#!/usr/bin/env python3
"""Disable reauthRequired accounts so they leave the live routing pool."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
env: dict[str, str] = {}
env_path = ROOT / ".env"
if env_path.is_file():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip("\"'")

BASE = env.get("GROK2API_URL", "http://127.0.0.1:8000").rstrip("/")
USER = env.get("GROK2API_ADMIN_USER", "admin")
PASS = env.get("GROK2API_ADMIN_PASS", "")


def http(method: str, url: str, token: str | None = None, body: dict | None = None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:800]


def login() -> str:
    code, d = http(
        "POST",
        f"{BASE}/api/admin/v1/auth/login",
        body={"username": USER, "password": PASS},
    )
    if code >= 400:
        raise SystemExit(f"login failed {code} {d}")
    root = d.get("data") if isinstance(d.get("data"), dict) else d
    tokens = root.get("tokens") if isinstance(root.get("tokens"), dict) else root
    tok = (
        (tokens or {}).get("accessToken")
        or (tokens or {}).get("access_token")
        or root.get("accessToken")
    )
    if not tok:
        raise SystemExit(f"no token {d}")
    return str(tok)


def list_all(tok: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while page < 100:
        code, d = http(
            "GET",
            f"{BASE}/api/admin/v1/accounts?page={page}&pageSize=100",
            token=tok,
        )
        if code >= 400:
            break
        chunk: list = []
        total = None
        if isinstance(d.get("data"), dict):
            chunk = d["data"].get("items") or []
            total = d["data"].get("total")
        elif isinstance(d.get("data"), list):
            chunk = d["data"]
        if not chunk:
            break
        if items and chunk[0].get("id") == items[0].get("id") and page > 1:
            break
        items.extend(chunk)
        if total and len(items) >= int(total):
            break
        if len(chunk) < 50:
            break
        page += 1
    return items


def disable_account(tok: str, aid) -> tuple[bool, str]:
    last = ""
    for method, path, body in [
        ("PATCH", f"/api/admin/v1/accounts/{aid}", {"enabled": False}),
        ("PUT", f"/api/admin/v1/accounts/{aid}", {"enabled": False}),
        ("POST", f"/api/admin/v1/accounts/{aid}/disable", None),
    ]:
        code, resp = http(method, BASE + path, token=tok, body=body)
        if code < 400:
            return True, f"{method} {path}"
        last = f"{method} {path} => {code} {str(resp)[:160]}"
    return False, last


def main() -> int:
    dry = "--dry" in sys.argv
    tok = login()
    items = list_all(tok)
    reauth = [it for it in items if "reauth" in str(it.get("authStatus") or "").lower()]
    enabled_reauth = [it for it in reauth if it.get("enabled")]
    print(f"total={len(items)} reauth={len(reauth)} enabled_reauth={len(enabled_reauth)}")
    for it in reauth:
        print(
            f"  id={it.get('id')} email={it.get('email')} status={it.get('authStatus')} "
            f"enabled={it.get('enabled')} rfc={it.get('refreshFailureCount')} exp={it.get('expiresAt')}"
        )
    if dry or not enabled_reauth:
        return 0
    ok = fail = 0
    for it in enabled_reauth:
        success, how = disable_account(tok, it.get("id"))
        if success:
            ok += 1
            print(f"DISABLED id={it.get('id')} via {how}")
        else:
            fail += 1
            print(f"FAIL id={it.get('id')} {how}")
    items2 = list_all(tok)
    still = [
        it
        for it in items2
        if "reauth" in str(it.get("authStatus") or "").lower() and it.get("enabled")
    ]
    print(f"done disabled={ok} fail={fail} still_enabled_reauth={len(still)}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
