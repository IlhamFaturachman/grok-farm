#!/usr/bin/env python3
"""
Public free-token status API for the landing page.

Serves JSON compatible with the synthlabs-style frontend:
  GET /api/public/status  (also /status)

Env:
  PUBLIC_STATUS_HOST=127.0.0.1
  PUBLIC_STATUS_PORT=8002
  PUBLIC_BASE_URL=https://api.example.com
  PUBLIC_TOKEN_TITLE=Free Token
  PUBLIC_TOKEN_FILE=./results/g2a_client_key.txt
  PUBLIC_TOKEN_KEY=                  # optional override full key
  PUBLIC_TOKEN_NAME=public-free      # match admin client-key name (preferred)
  PUBLIC_TOKEN_PREFIX=               # or match by key prefix
  GROK2API_URL=http://127.0.0.1:8000
  GROK2API_ADMIN_USER=admin
  GROK2API_ADMIN_PASS=...
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    pass


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)) or default)
    except ValueError:
        return default


HOST = _env("PUBLIC_STATUS_HOST", "127.0.0.1")
PORT = _env_int("PUBLIC_STATUS_PORT", 8002)
BASE_URL = _env("PUBLIC_BASE_URL", "https://api.example.com").rstrip("/")
TITLE = _env("PUBLIC_TOKEN_TITLE", "Free Token")
TOKEN_FILE = Path(_env("PUBLIC_TOKEN_FILE", str(_ROOT / "results" / "g2a_client_key.txt")))
TOKEN_KEY = _env("PUBLIC_TOKEN_KEY")
TOKEN_NAME = _env("PUBLIC_TOKEN_NAME", "public-free")
TOKEN_PREFIX = _env("PUBLIC_TOKEN_PREFIX")
G2A_URL = _env("GROK2API_URL", "http://127.0.0.1:8000").rstrip("/")
G2A_USER = _env("GROK2API_ADMIN_USER", "admin")
G2A_PASS = _env("GROK2API_ADMIN_PASS")
TRAFFIC_DB = Path(_env("TRAFFIC_DB", str(_ROOT / "results" / "traffic.db")))
CACHE_S = _env_int("PUBLIC_STATUS_CACHE_S", 10)

_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def _read_public_key() -> str:
    if TOKEN_KEY:
        return TOKEN_KEY.strip()
    if TOKEN_FILE.is_file():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    # fallback .env
    return _env("GROK_API_KEY")


def _mask_key(key: str) -> str:
    if not key:
        return "—"
    if len(key) <= 16:
        return key[:6] + "…"
    return key[:10] + "…" + key[-4:]


def _admin_login() -> str | None:
    if not G2A_PASS:
        return None
    try:
        req = Request(
            f"{G2A_URL}/api/admin/v1/auth/login",
            data=json.dumps({"username": G2A_USER, "password": G2A_PASS}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode())
        return ((body.get("data") or {}).get("tokens") or {}).get("accessToken")
    except Exception:
        return None


def _admin_get(path: str, token: str) -> Any:
    req = Request(
        f"{G2A_URL}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _list_models(api_key: str) -> list[str]:
    try:
        req = Request(
            f"{G2A_URL}/v1/models",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        with urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode())
        items = body.get("data") if isinstance(body.get("data"), list) else body.get("data")
        if isinstance(body.get("data"), list):
            items = body["data"]
        elif isinstance(body, list):
            items = body
        else:
            items = []
        out = []
        for it in items or []:
            if isinstance(it, dict) and it.get("id"):
                out.append(str(it["id"]))
            elif isinstance(it, str):
                out.append(it)
        return out or ["grok-4.5"]
    except Exception:
        return ["grok-4.5"]


def _traffic_stats(prefix: str) -> dict[str, Any]:
    if not TRAFFIC_DB.is_file() or not prefix:
        return {}
    try:
        con = sqlite3.connect(str(TRAFFIC_DB))
        cur = con.cursor()
        row = cur.execute(
            "SELECT COUNT(*), MAX(ts) FROM requests WHERE key_prefix = ?",
            (prefix,),
        ).fetchone()
        con.close()
        return {
            "request_count": int(row[0] or 0) if row else 0,
            "last_seen": row[1] if row else None,
        }
    except Exception:
        return {}


def _find_client_key(items: list[dict], public_key: str) -> dict | None:
    prefix_hint = TOKEN_PREFIX
    if not prefix_hint and public_key.startswith("g2a_"):
        parts = public_key.split("_")
        if len(parts) >= 2:
            prefix_hint = parts[1]

    # name match preferred
    if TOKEN_NAME:
        for it in items:
            if str(it.get("name") or "").lower() == TOKEN_NAME.lower():
                return it
    # prefix match
    if prefix_hint:
        for it in items:
            if str(it.get("prefix") or "") == prefix_hint:
                return it
            if prefix_hint in public_key and str(it.get("prefix") or "") in public_key:
                return it
    # single enabled key fallback
    enabled = [it for it in items if it.get("enabled")]
    if len(enabled) == 1:
        return enabled[0]
    return items[0] if items else None


def build_status() -> dict[str, Any]:
    public_key = _read_public_key()
    models = _list_models(public_key) if public_key else ["grok-4.5"]

    meta: dict[str, Any] = {}
    token = _admin_login()
    if token:
        try:
            body = _admin_get("/api/admin/v1/client-keys", token)
            items = (body.get("data") or {}).get("items") or body.get("items") or []
            if isinstance(items, list):
                meta = _find_client_key([x for x in items if isinstance(x, dict)], public_key) or {}
        except Exception as e:
            meta = {"_admin_error": str(e)[:120]}

    prefix = str(meta.get("prefix") or "")
    if not prefix and public_key.startswith("g2a_"):
        parts = public_key.split("_")
        if len(parts) >= 2:
            prefix = parts[1]

    traffic = _traffic_stats(prefix)
    limit_ticks = int(meta.get("billingLimitUsdTicks") or 0)
    used_ticks = int(meta.get("billedUsageUsdTicks") or 0)
    unlimited = limit_ticks <= 0
    # Present ticks as "usage units" (admin UI uses ticks). Keep numbers readable.
    used_display = used_ticks
    if unlimited:
        quota = 0
        remaining = 0
    else:
        quota = limit_ticks
        remaining = max(0, limit_ticks - used_ticks)

    enabled = bool(meta.get("enabled", True)) if meta else bool(public_key)
    active = bool(public_key) and enabled

    last_used = meta.get("lastUsedAt") or traffic.get("last_seen")
    created = meta.get("createdAt") or meta.get("created_at")

    return {
        "active": active,
        "enabled": enabled,
        "title": TITLE,
        "name": meta.get("name") or TOKEN_NAME or "public",
        "api_key": public_key if active else "",
        "key_masked": _mask_key(public_key) if active else "—",
        "endpoint": f"{BASE_URL}/v1",
        "quota": quota,
        "remaining": remaining,
        "used": used_display if used_display else int(traffic.get("request_count") or 0),
        "unlimited": unlimited,
        "models": models,
        "created_at": created,
        "last_used_at": last_used,
        "rpm_limit": meta.get("rpmLimit"),
        "request_count": traffic.get("request_count"),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def get_status_cached() -> dict[str, Any]:
    now = time.time()
    with _lock:
        if _cache["data"] is not None and now - float(_cache["ts"]) < CACHE_S:
            return _cache["data"]
    data = build_status()
    with _lock:
        _cache["ts"] = now
        _cache["data"] = data
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "GrokFarmPublicStatus/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")
        self.send_header("Cache-Control", "no-store")

    def _json(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/api/public/status", "/status", "/"):
            try:
                self._json(200, get_status_cached())
            except Exception as e:
                self._json(500, {"active": False, "error": str(e)[:200]})
            return
        if path == "/healthz":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})


def main() -> int:
    print("Grok Farm public status", flush=True)
    print(f"  bind : http://{HOST}:{PORT}", flush=True)
    print(f"  g2a  : {G2A_URL}", flush=True)
    print(f"  base : {BASE_URL}", flush=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
