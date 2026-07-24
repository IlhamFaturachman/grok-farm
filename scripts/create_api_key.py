#!/usr/bin/env python3
"""Create a grok2api client API key and save to results/g2a_client_key.txt."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env = _ROOT / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _env(k: str, default: str = "") -> str:
    return (os.environ.get(k) or default).strip()


def _http(method: str, url: str, *, token: str | None = None, body: dict | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {err[:800]}") from e


def admin_login(base: str, user: str, password: str) -> str:
    data = _http("POST", f"{base}/api/admin/v1/auth/login", body={"username": user, "password": password})
    root = data.get("data") if isinstance(data.get("data"), dict) else data
    tokens = root.get("tokens") if isinstance(root.get("tokens"), dict) else root
    access = (
        (tokens or {}).get("accessToken")
        or (tokens or {}).get("access_token")
        or (root or {}).get("accessToken")
        or data.get("accessToken")
    )
    if not access:
        raise RuntimeError(f"login missing token: {json.dumps(data)[:500]}")
    return str(access)


def create_key(base: str, token: str, name: str) -> dict:
    # Try common grok2api shapes
    payloads = [
        {"name": name, "enabled": True},
        {"name": name},
        {"label": name, "enabled": True},
        {"title": name, "enabled": True},
    ]
    paths = [
        "/api/admin/v1/client-keys",
        "/api/admin/v1/keys",
        "/api/admin/v1/api-keys",
        "/api/admin/v1/client-keys/create",
    ]
    last_err = None
    for path in paths:
        for body in payloads:
            try:
                return _http("POST", f"{base}{path}", token=token, body=body)
            except Exception as e:
                last_err = e
                continue
    raise RuntimeError(f"could not create key: {last_err}")


def list_keys(base: str, token: str) -> list:
    for path in ("/api/admin/v1/client-keys", "/api/admin/v1/keys", "/api/admin/v1/api-keys"):
        try:
            data = _http("GET", f"{base}{path}", token=token)
            if isinstance(data.get("data"), list):
                return data["data"]
            if isinstance(data.get("items"), list):
                return data["items"]
            if isinstance(data, list):
                return data
            if isinstance(data.get("data"), dict):
                for k in ("items", "keys", "list", "clientKeys"):
                    if isinstance(data["data"].get(k), list):
                        return data["data"][k]
        except Exception:
            continue
    return []


def extract_key(payload: dict) -> str | None:
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower()
                if lk in ("key", "api_key", "apikey", "secret", "token", "clientkey", "value") and isinstance(v, str) and len(v) > 10:
                    if v.startswith("g2a_") or len(v) > 20:
                        return v
                found = walk(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for it in obj:
                found = walk(it)
                if found:
                    return found
        return None

    return walk(payload)


def main() -> int:
    _load_env()
    base = _env("GROK2API_URL", "http://127.0.0.1:8000").rstrip("/")
    user = _env("GROK2API_ADMIN_USER", "admin")
    password = _env("GROK2API_ADMIN_PASS")
    name = sys.argv[1] if len(sys.argv) > 1 else "opencode-farm"
    if not password:
        print("ERROR: GROK2API_ADMIN_PASS missing", file=sys.stderr)
        return 1

    token = admin_login(base, user, password)
    print("admin login ok")

    existing = list_keys(base, token)
    print(f"existing_keys={len(existing)}")

    created = create_key(base, token, name)
    print("create response:", json.dumps(created)[:800])
    key = extract_key(created)
    if not key:
        # re-list after create
        existing = list_keys(base, token)
        print("keys after create:", json.dumps(existing)[:1000])
        key = extract_key({"data": existing})

    out = _ROOT / "results" / "g2a_client_key.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    if key:
        out.write_text(key + "\n", encoding="utf-8")
        os.chmod(out, 0o600)
        print(f"saved {out}")
        print(f"KEY={key}")
        return 0

    print("WARN: key string not found in response — open admin UI to copy key", file=sys.stderr)
    out.write_text(json.dumps(created, indent=2) + "\n", encoding="utf-8")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
