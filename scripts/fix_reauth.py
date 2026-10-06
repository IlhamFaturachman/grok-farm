#!/usr/bin/env python3
"""Disable reauthRequired accounts in grok2api pool + optional force-refresh try."""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("/opt/grok-farm")
env = {}
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip().strip("\"'")

BASE = env.get("GROK2API_URL", "http://127.0.0.1:8000").rstrip("/")
USER = env.get("GROK2API_ADMIN_USER", "admin")
PASS = env.get("GROK2API_ADMIN_PASS", "")


def http(method, url, token=None, body=None):
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


def login():
    code, d = http("POST", f"{BASE}/api/admin/v1/auth/login", body={"username": USER, "password": PASS})
    if code >= 400:
        raise SystemExit(f"login failed {code} {d}")
    root = d.get("data") if isinstance(d.get("data"), dict) else d
    tokens = root.get("tokens") if isinstance(root.get("tokens"), dict) else root
    tok = (tokens or {}).get("accessToken") or (tokens or {}).get("access_token") or root.get("accessToken")
    if not tok:
        raise SystemExit(f"no token {d}")
    return str(tok)


def list_all(tok):
    items = []
    page = 1
    while page < 100:
        code, d = http("GET", f"{BASE}/api/admin/v1/accounts?page={page}&pageSize=100", token=tok)
        if code >= 400:
            break
        chunk = []
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


def disable_account(tok, aid):
    # try several shapes
    for method, path, body in [
        ("PATCH", f"/api/admin/v1/accounts/{aid}", {"enabled": False}),
        ("PUT", f"/api/admin/v1/accounts/{aid}", {"enabled": False}),
        ("POST", f"/api/admin/v1/accounts/{aid}/disable", None),
        ("PATCH", f"/api/admin/v1/accounts/{aid}", {"enabled": False, "priority": 0}),
    ]:
        code, resp = http(method, BASE + path, token=tok, body=body)
        if code < 400:
            return True, method, path, code, resp
        if code != 404:
            last = (method, path, code, str(resp)[:200])
    return False, None, None, None, last if "last" in dir() else None


def main():
    dry = "--dry" in sys.argv
    tok = login()
    items = list_all(tok)
    reauth = [it for it in items if "reauth" in str(it.get("authStatus") or "").lower()]
    print(f"total={len(items)} reauth={len(reauth)}")
    for it in reauth:
        print(f"  id={it.get('id')} email={it.get('email')} status={it.get('authStatus')} rfc={it.get('refreshFailureCount')} exp={it.get('expiresAt')}")
    if dry or not reauth:
        return
    ok = fail = 0
    for it in reauth:
        aid = it.get("id")
        success, method, path, code, resp = disable_account(tok, aid)
        if success:
            ok += 1
            print(f"DISABLED id={aid} via {method} {path}")
        else:
            fail += 1
            print(f"FAIL disable id={aid} {resp}")
    print(f"done disabled={ok} fail={fail}")
    # re-check
    items2 = list_all(tok)
    reauth2 = [it for it in items2 if "reauth" in str(it.get("authStatus") or "").lower()]
    enabled_reauth = [it for it in reauth2 if it.get("enabled")]
    print(f"after: reauth={len(reauth2)} still_enabled={len(enabled_reauth)}")


if __name__ == "__main__":
    main()
