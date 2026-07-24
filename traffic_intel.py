#!/usr/bin/env python3
"""
Traffic Intel — reverse-proxy logger + project analytics for grok2api.

Sits in front of grok2api, records full request/response (chat messages,
client IP, key prefix, model), clusters into "projects", and serves an
English dashboard with graphs + transcripts.

  Client  →  :8001 (this proxy)  →  :8000 (grok2api)
                    ↓
               SQLite (traffic.db)
                    ↓
            Dashboard /traffic (same process)

Env:
  TRAFFIC_HOST=0.0.0.0
  TRAFFIC_PORT=8001
  TRAFFIC_UPSTREAM=http://127.0.0.1:8000
  TRAFFIC_DB=/opt/grok-farm/results/traffic.db
  TRAFFIC_PANEL_USER=admin
  TRAFFIC_PANEL_PASSWORD=...   # or FARM_PANEL_PASSWORD / GROK2API_ADMIN_PASS
  TRAFFIC_MAX_BODY_MB=128
  TRAFFIC_RETENTION_DAYS=30
  GROK_ENHANCE=true                 # inject server-side system prompt + defaults
  GROK_SYSTEM_PROMPT=...            # or file via GROK_SYSTEM_PROMPT_FILE
  GROK_DEFAULT_TEMPERATURE=0.2
  GROK_DEFAULT_MAX_TOKENS=8192
  GROK_FORCE_DEFAULTS=false         # if true, overwrite client temp/max_tokens
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
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


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)) or default)
    except ValueError:
        return default


HOST = _env("TRAFFIC_HOST", "0.0.0.0")
PORT = _env_int("TRAFFIC_PORT", 8001)
UPSTREAM = _env("TRAFFIC_UPSTREAM", "http://127.0.0.1:8000").rstrip("/")
DB_PATH = Path(_env("TRAFFIC_DB", str(_ROOT / "results" / "traffic.db")))
PANEL_USER = _env("TRAFFIC_PANEL_USER") or _env("FARM_PANEL_USER", "admin")
PANEL_PASSWORD = (
    _env("TRAFFIC_PANEL_PASSWORD")
    or _env("FARM_PANEL_PASSWORD")
    or _env("GROK2API_ADMIN_PASS")
)
TOKEN_TTL = _env_int("TRAFFIC_TOKEN_TTL", 86400)
# Keep aligned with nginx client_max_body_size / grok2api maxBodyBytes (128 MiB).
MAX_BODY = _env_int("TRAFFIC_MAX_BODY_MB", 128) * 1024 * 1024
RETENTION_DAYS = _env_int("TRAFFIC_RETENTION_DAYS", 30)
# Upstream retry: when grok2api/xAI returns a capacity / transient error, retry
# internally instead of forwarding the broken response to the client (which would
# otherwise trip Zod `invalid_union` validation on the client side).
UPSTREAM_RETRY_MAX = _env_int("TRAFFIC_UPSTREAM_RETRY_MAX", 8)
UPSTREAM_RETRY_DELAY = _env_float("TRAFFIC_UPSTREAM_RETRY_DELAY", 2.0)
# Substrings that indicate a transient upstream failure worth retrying.
_UPSTREAM_RETRY_MARKERS = (
    "at capacity",
    "high demand",
    "model is currently at capacity",
    "priority processing",
    "service tier",
    # NOTE: do NOT match bare "upstream" — Chinese 上游账号额度不足 is permanent 402
    "暂时异常",
    "type\":\"error\"",
    "\"type\": \"error\"",
    "rate limit",
    "rate_limit",
    "try again",
    "overloaded",
    "too many requests",
)


def _extract_upstream_error_message(body: bytes | str | None) -> str:
    """Pull human message from xAI/OpenAI/Anthropic-ish error payloads."""
    if body is None:
        return ""
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace")
    else:
        text = str(body)
    text = text.strip()
    if not text:
        return ""
    # SSE: data: {...}
    if text.startswith("data:"):
        text = text[5:].strip()
    try:
        obj = json.loads(text)
    except Exception:
        return text[:500]
    if not isinstance(obj, dict):
        return text[:500]
    # OpenAI: {error:{message}}
    err = obj.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err.get("message"))[:500]
    if isinstance(err, str) and err:
        return err[:500]
    # xAI responses-style: {type:error, message:...}
    if obj.get("message"):
        return str(obj.get("message"))[:500]
    # Anthropic: {error:{message}} already covered; {type:error,error:{message}}
    if isinstance(obj.get("error"), dict):
        return str(obj["error"].get("message") or obj["error"])[:500]
    return text[:500]


def _is_capacity_or_transient_text(text: str) -> bool:
    t = (text or "").lower()
    if not t:
        return False
    return any(m.lower() in t for m in _UPSTREAM_RETRY_MARKERS)


def _is_upstream_transient_fail(status: int, body: bytes) -> bool:
    # 402 = one free account hit spending-limit. Retry at GATEWAY only.
    # grok2api must keep routing.maxAttempts=1 so each attempt burns 1 account,
    # not 3. Gateway then re-issues the request → next clean account.
    if status == 402:
        return True
    if status in (429, 502, 503, 504):
        return True
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return False
    if status >= 500:
        return True
    # xAI often returns HTTP 200 with {type:"error", message:"at capacity..."}
    # which OpenCode Zod rejects as invalid_union (no choices, no error object).
    msg = _extract_upstream_error_message(text)
    if _is_capacity_or_transient_text(msg) or _is_capacity_or_transient_text(text):
        return True
    # structured type=error without choices
    try:
        obj = json.loads(text.strip()[5:].strip() if text.strip().startswith("data:") else text)
        if isinstance(obj, dict) and str(obj.get("type") or "").lower() == "error":
            return True
        if isinstance(obj, dict) and "choices" not in obj and (
            obj.get("error") or obj.get("message")
        ):
            # non-chat error shape — treat capacity-like only
            blob = json.dumps(obj, ensure_ascii=False).lower()
            if _is_capacity_or_transient_text(blob):
                return True
    except Exception:
        pass
    return False


def _openai_error_body(
    message: str,
    *,
    err_type: str = "server_error",
    code: str | None = "upstream_capacity",
    status: int = 503,
) -> bytes:
    """OpenAI-compatible error so clients (OpenCode Zod) accept the payload."""
    payload = {
        "error": {
            "message": message
            or "Upstream model is temporarily at capacity. Please retry.",
            "type": err_type,
            "code": code,
            "param": None,
        },
        # extra hints for UIs that only show top-level message
        "message": message,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _normalize_error_response(
    status: int, body: bytes, path: str = ""
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Ensure failed/capacity responses are OpenAI-shaped (not xAI type=error)."""
    msg = _extract_upstream_error_message(body) or "Upstream error"
    is_cap = _is_capacity_or_transient_text(msg) or _is_upstream_transient_fail(status, body)
    if is_cap:
        out_status = 503 if status in (200, 0) else status
        if out_status < 400:
            out_status = 503
        body_out = _openai_error_body(
            "Model is temporarily at capacity due to high demand. "
            "Please retry in a few seconds — the gateway will auto-retry, "
            "or send your message again.",
            err_type="server_error",
            code="model_at_capacity",
            status=out_status,
        )
        return (
            out_status,
            [("Content-Type", "application/json; charset=utf-8")],
            body_out,
        )
    # already OpenAI-shaped?
    try:
        obj = json.loads(body.decode("utf-8", errors="replace"))
        if isinstance(obj, dict) and isinstance(obj.get("error"), dict) and obj["error"].get("message"):
            return status if status >= 400 else 502, [
                ("Content-Type", "application/json; charset=utf-8")
            ], body
    except Exception:
        pass
    out_status = status if status >= 400 else 502
    return (
        out_status,
        [("Content-Type", "application/json; charset=utf-8")],
        _openai_error_body(msg, code="upstream_error", status=out_status),
    )


# Idle gap between SSE lines (reasoning can pause a long time). Was 45s and
# caused mid-task "done" in OpenCode when we also injected a fake [DONE].
STREAM_IDLE_TIMEOUT_S = max(60, _env_int("TRAFFIC_STREAM_IDLE_TIMEOUT", 300))
STREAM_MAX_WALL_S = max(120, _env_int("TRAFFIC_STREAM_MAX_WALL", 7200))
STREAM_IDLE_CONTINUE_MAX = max(1, _env_int("TRAFFIC_STREAM_IDLE_CONTINUE_MAX", 12))
# Shop billing (optional): reject + debit shop-issued keys when balance hits 0
SHOP_BILLING_URL = _env("SHOP_BILLING_URL", "http://127.0.0.1:8090").rstrip("/")
SHOP_INTERNAL_TOKEN = _env("SHOP_INTERNAL_TOKEN") or _env("INTERNAL_TOKEN")
SHOP_BILLING_ENABLED = _env_bool("SHOP_BILLING_ENABLED", True) and bool(SHOP_INTERNAL_TOKEN)
# API key for calling Grok to summarize projects (generate in grok2api admin panel)
GROK_API_KEY = _env("GROK_API_KEY")
GROK_SUMMARY_MODEL = _env("GROK_SUMMARY_MODEL", "grok-4.5")

# Server-side request enhancement (makes OpenCode "dewa" without client config)
GROK_ENHANCE = _env_bool("GROK_ENHANCE", True)
GROK_FORCE_DEFAULTS = _env_bool("GROK_FORCE_DEFAULTS", False)
GROK_DEFAULT_TEMPERATURE = _env_float("GROK_DEFAULT_TEMPERATURE", 0.2)
GROK_DEFAULT_MAX_TOKENS = _env_int("GROK_DEFAULT_MAX_TOKENS", 12288)
_SYSTEM_MARKER = "[grok-farm-boost]"
_PROMPTS_DIR = _ROOT / "prompts"


def _read_prompt_file(*candidates: Path) -> str:
    for p in candidates:
        if p and p.is_file():
            try:
                return p.read_text(encoding="utf-8").strip()
            except Exception:
                continue
    return ""


def _load_system_prompt() -> str:
    path = _env("GROK_SYSTEM_PROMPT_FILE")
    if path:
        text = _read_prompt_file(Path(path))
        if text:
            return text
    # preferred polyglot default, then legacy staff_engineer
    text = _read_prompt_file(
        _PROMPTS_DIR / "polyglot_code.txt",
        _PROMPTS_DIR / "staff_engineer.txt",
    )
    if text:
        return text
    raw = _env("GROK_SYSTEM_PROMPT")
    if raw:
        return raw.replace("\\n", "\n").strip()
    return (
        f"{_SYSTEM_MARKER}\n"
        "You are a world-class senior staff engineer and versatile technical assistant.\n"
        "- Deep multi-step reasoning; correctness over fluff.\n"
        "- Complete runnable code when implementing; never invent APIs/URLs.\n"
        "- Use client tools for live facts when available.\n"
        "- Help with coding and non-coding technical tasks rigorously.\n"
    )


def _load_mode_prompts() -> dict[str, str]:
    base = _load_system_prompt()
    general = _read_prompt_file(_PROMPTS_DIR / "mode_general.txt") or base
    research = _read_prompt_file(_PROMPTS_DIR / "mode_research.txt") or base
    return {
        "code": base,
        "default": base,
        "general": general,
        "research": research,
    }


GROK_SYSTEM_PROMPT = _load_system_prompt()
GROK_MODE_PROMPTS = _load_mode_prompts()

_SIGN = hashlib.sha256(
    (PANEL_PASSWORD or secrets.token_hex(16) + ":traffic").encode()
).digest()
_db_lock = threading.Lock()


# ── time / crypto ────────────────────────────────────────────────────────────
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime | None = None) -> str:
    d = dt or _utc_now()
    return d.isoformat().replace("+00:00", "Z")


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(user: str) -> str:
    payload = json.dumps(
        {"u": user, "exp": int(time.time()) + TOKEN_TTL, "n": secrets.token_hex(6)},
        separators=(",", ":"),
    ).encode()
    body = _b64url(payload)
    sig = _b64url(hmac.new(_SIGN, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expect = _b64url(hmac.new(_SIGN, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        data = json.loads(_b64url_decode(body))
    except Exception:
        return None
    if int(data.get("exp") or 0) < time.time():
        return None
    return str(data.get("u") or "")


# ── message / project heuristics ─────────────────────────────────────────────
_STOP = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "it",
    "this", "that", "with", "as", "at", "be", "by", "from", "are", "was", "were",
    "you", "your", "i", "me", "my", "we", "our", "they", "them", "please", "can",
    "could", "would", "should", "will", "just", "not", "no", "yes", "if", "then",
    "so", "but", "about", "into", "using", "use", "make", "help", "how", "what",
    "when", "where", "why", "who", "do", "does", "did", "have", "has", "had",
    "code", "write", "create", "need", "want", "like", "get", "set", "add",
}


def _extract_text_parts(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits = []
        for part in content:
            if isinstance(part, str):
                bits.append(part)
            elif isinstance(part, dict):
                if part.get("type") in ("text", "input_text", "output_text"):
                    bits.append(str(part.get("text") or part.get("content") or ""))
                elif "text" in part:
                    bits.append(str(part.get("text") or ""))
        return "\n".join(bits)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


def extract_messages(body: dict[str, Any] | None) -> list[dict[str, str]]:
    """Normalize chat/completions + responses-style payloads to role/text list."""
    if not body or not isinstance(body, dict):
        return []
    out: list[dict[str, str]] = []
    msgs = body.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "user")
            text = _extract_text_parts(m.get("content")).strip()
            if text:
                out.append({"role": role, "text": text[:8000]})
    # OpenAI Responses API: input can be string or list
    inp = body.get("input")
    if isinstance(inp, str) and inp.strip():
        out.append({"role": "user", "text": inp.strip()[:8000]})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str) and item.strip():
                out.append({"role": "user", "text": item.strip()[:8000]})
            elif isinstance(item, dict):
                role = str(item.get("role") or item.get("type") or "user")
                text = _extract_text_parts(item.get("content") or item.get("text")).strip()
                if text:
                    out.append({"role": role, "text": text[:8000]})
    if body.get("system") and isinstance(body["system"], str):
        out.insert(0, {"role": "system", "text": body["system"][:8000]})
    return out


def extract_assistant_reply(resp: dict[str, Any] | None, raw_text: str) -> str:
    if not resp:
        # try SSE last data
        if "data:" in raw_text:
            last = ""
            for line in raw_text.splitlines():
                if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                    last = line[5:].strip()
            if last:
                try:
                    resp = json.loads(last)
                except Exception:
                    return raw_text[:4000]
        else:
            return raw_text[:4000]
    if not isinstance(resp, dict):
        return str(resp)[:4000]
    # chat.completions
    choices = resp.get("choices")
    if isinstance(choices, list) and choices:
        c0 = choices[0] if isinstance(choices[0], dict) else {}
        msg = c0.get("message") if isinstance(c0.get("message"), dict) else {}
        content = msg.get("content") or c0.get("text") or ""
        text = _extract_text_parts(content).strip()
        if text:
            return text[:8000]
    # responses API
    if resp.get("output_text"):
        return str(resp["output_text"])[:8000]
    out = resp.get("output")
    if isinstance(out, list):
        bits = []
        for item in out:
            if isinstance(item, dict):
                bits.append(_extract_text_parts(item.get("content") or item.get("text")))
        joined = "\n".join(b for b in bits if b).strip()
        if joined:
            return joined[:8000]
    return json.dumps(resp, ensure_ascii=False)[:4000]


def keyword_bag(text: str, limit: int = 12) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_./+-]{2,}", text.lower())
    bag = [w for w in words if w not in _STOP and not w.isdigit()]
    return [w for w, _ in Counter(bag).most_common(limit)]


def project_fingerprint(messages: list[dict[str, str]], model: str, key_prefix: str) -> str:
    """Stable-ish fingerprint for clustering related work.

    Prefer system-prompt identity (best project signal). If no system prompt,
    fall back to key + coarse topic keywords from the first user message.
    """
    system = " ".join(m["text"] for m in messages if m["role"] == "system")[:2000].strip()
    users = [m["text"] for m in messages if m["role"] == "user"]
    first_user = users[0] if users else ""
    if system:
        # Same system prompt + same API key => same project, even if user asks evolve
        basis = "|".join(
            [
                key_prefix or "-",
                hashlib.sha1(system.encode()).hexdigest()[:16],
            ]
        )
    else:
        kws = keyword_bag(first_user, 6)
        basis = "|".join(
            [
                key_prefix or "-",
                (model or "-")[:40],
                ",".join(kws[:5]) or hashlib.sha1(first_user[:200].encode()).hexdigest()[:10],
            ]
        )
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def project_title(messages: list[dict[str, str]], kws: list[str]) -> str:
    users = [m["text"] for m in messages if m["role"] == "user"]
    first = (users[0] if users else "").strip().replace("\n", " ")
    if first:
        title = first[:72] + ("…" if len(first) > 72 else "")
        return title
    if kws:
        return "Project: " + ", ".join(kws[:4])
    return "Untitled project"


def project_summary(messages: list[dict[str, str]], kws: list[str], reply: str) -> str:
    users = [m["text"] for m in messages if m["role"] == "user"]
    systems = [m["text"] for m in messages if m["role"] == "system"]
    parts = []
    if systems:
        parts.append("System context: " + systems[0][:200].replace("\n", " "))
    if users:
        parts.append("User asks: " + users[-1][:240].replace("\n", " "))
    if reply:
        parts.append("Assistant: " + reply[:200].replace("\n", " "))
    if kws:
        parts.append("Topics: " + ", ".join(kws[:8]))
    return " | ".join(parts) if parts else "No content extracted"


# ── SQLite ───────────────────────────────────────────────────────────────────
def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with _db_lock:
        conn = db()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                  id TEXT PRIMARY KEY,
                  fingerprint TEXT UNIQUE,
                  title TEXT,
                  summary TEXT,
                  keywords TEXT,
                  key_prefix TEXT,
                  model TEXT,
                  first_seen TEXT,
                  last_seen TEXT,
                  request_count INTEGER DEFAULT 0,
                  client_ips TEXT DEFAULT '[]',
                  ai_summary TEXT
                );
                CREATE TABLE IF NOT EXISTS requests (
                  id TEXT PRIMARY KEY,
                  ts TEXT NOT NULL,
                  project_id TEXT,
                  client_ip TEXT,
                  method TEXT,
                  path TEXT,
                  status INTEGER,
                  duration_ms INTEGER,
                  model TEXT,
                  key_prefix TEXT,
                  streaming INTEGER,
                  request_body TEXT,
                  response_body TEXT,
                  messages_json TEXT,
                  assistant_text TEXT,
                  user_text TEXT,
                  prompt_tokens INTEGER,
                  completion_tokens INTEGER,
                  total_tokens INTEGER,
                  error TEXT,
                  FOREIGN KEY(project_id) REFERENCES projects(id)
                );
                CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts);
                CREATE INDEX IF NOT EXISTS idx_req_project ON requests(project_id);
                CREATE INDEX IF NOT EXISTS idx_req_ip ON requests(client_ip);
                CREATE INDEX IF NOT EXISTS idx_proj_last ON projects(last_seen);
                """
            )
            # Migration: add ai_summary column to pre-existing databases
            cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
            if "ai_summary" not in cols:
                conn.execute("ALTER TABLE projects ADD COLUMN ai_summary TEXT")
            conn.commit()
        finally:
            conn.close()


def retention_cleanup() -> None:
    if RETENTION_DAYS <= 0:
        return
    cutoff = _utc_iso(_utc_now() - timedelta(days=RETENTION_DAYS))
    with _db_lock:
        conn = db()
        try:
            conn.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
            conn.execute(
                """
                DELETE FROM projects WHERE id NOT IN (
                  SELECT DISTINCT project_id FROM requests WHERE project_id IS NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def key_prefix_from_auth(auth: str | None, x_api_key: str | None) -> str:
    raw = ""
    if auth and auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
    elif x_api_key:
        raw = x_api_key.strip()
    if not raw:
        return ""
    if len(raw) <= 16:
        return raw[:8] + "…"
    return raw[:14] + "…"


def extract_api_key(auth: str | None, x_api_key: str | None) -> str:
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if x_api_key:
        return x_api_key.strip()
    return ""


def shop_billing_check(api_key: str) -> dict[str, Any] | None:
    """Return shop check payload, or None if billing disabled / not a shop key path."""
    if not SHOP_BILLING_ENABLED or not api_key or not api_key.startswith("g2a_"):
        return None
    try:
        raw = json.dumps({"api_key": api_key}).encode("utf-8")
        req = Request(
            f"{SHOP_BILLING_URL}/api/internal/billing/check",
            data=raw,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Shop-Internal": SHOP_INTERNAL_TOKEN,
            },
        )
        with urlopen(req, timeout=2.5) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except Exception as e:
        sys.stderr.write(f"shop_billing_check error: {e}\n")
        # fail-open for non-shop / shop downtime so API doesn't hard-die
        return None


def shop_billing_debit(
    api_key: str,
    *,
    tokens: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    path: str = "",
    model: str = "",
) -> None:
    if not SHOP_BILLING_ENABLED or not api_key or tokens <= 0:
        return
    if not api_key.startswith("g2a_"):
        return
    try:
        raw = json.dumps(
            {
                "api_key": api_key,
                "tokens": int(tokens),
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "path": path or "",
                "model": model or "",
            }
        ).encode("utf-8")
        req = Request(
            f"{SHOP_BILLING_URL}/api/internal/billing/debit",
            data=raw,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Shop-Internal": SHOP_INTERNAL_TOKEN,
            },
        )
        with urlopen(req, timeout=2.5) as resp:
            resp.read()
    except Exception as e:
        sys.stderr.write(f"shop_billing_debit error: {e}\n")


def usage_from_sse(resp_text: str) -> tuple[int, int, int]:
    """Best-effort parse usage from SSE stream chunks."""
    pt = ct = tt = 0
    for line in (resp_text or "").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        u = obj.get("usage")
        if isinstance(u, dict):
            pt = int(u.get("prompt_tokens") or u.get("input_tokens") or pt or 0)
            ct = int(u.get("completion_tokens") or u.get("output_tokens") or ct or 0)
            tt = int(u.get("total_tokens") or (pt + ct) or tt or 0)
    return pt, ct, tt


def client_ip_from_headers(handler: BaseHTTPRequestHandler) -> str:
    xff = handler.headers.get("X-Forwarded-For") or handler.headers.get("X-Real-IP")
    if xff:
        return xff.split(",")[0].strip()
    return handler.client_address[0]


def usage_tokens(resp: dict[str, Any] | None) -> tuple[int, int, int]:
    if not isinstance(resp, dict):
        return 0, 0, 0
    u = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
    pt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    ct = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    tt = int(u.get("total_tokens") or (pt + ct) or 0)
    return pt, ct, tt


def store_exchange(
    *,
    client_ip: str,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
    model: str,
    key_prefix: str,
    streaming: bool,
    req_body: dict | None,
    resp_obj: dict | None,
    resp_raw: str,
    error: str = "",
) -> str:
    messages = extract_messages(req_body)
    assistant = extract_assistant_reply(resp_obj, resp_raw)
    user_text = "\n\n".join(m["text"] for m in messages if m["role"] == "user")
    all_text = "\n".join(m["text"] for m in messages) + "\n" + assistant
    kws = keyword_bag(all_text, 12)
    fp = project_fingerprint(messages, model, key_prefix)
    title = project_title(messages, kws)
    summary = project_summary(messages, kws, assistant)
    pt, ct, tt = usage_tokens(resp_obj)
    rid = uuid.uuid4().hex
    now = _utc_iso()
    pid = fp  # use fingerprint as project id for stability

    with _db_lock:
        conn = db()
        try:
            row = conn.execute(
                "SELECT id, request_count, client_ips, title, summary FROM projects WHERE fingerprint=?",
                (fp,),
            ).fetchone()
            if row:
                pid = row["id"]
                ips = []
                try:
                    ips = json.loads(row["client_ips"] or "[]")
                except Exception:
                    ips = []
                if client_ip and client_ip not in ips:
                    ips.append(client_ip)
                    ips = ips[-20:]
                # keep original title if more established; refresh summary lightly
                conn.execute(
                    """
                    UPDATE projects SET
                      last_seen=?, request_count=request_count+1,
                      client_ips=?, keywords=?, model=COALESCE(NULLIF(model,''), ?),
                      summary=?, key_prefix=COALESCE(NULLIF(key_prefix,''), ?)
                    WHERE id=?
                    """,
                    (
                        now,
                        json.dumps(ips),
                        json.dumps(kws),
                        model,
                        summary[:1000],
                        key_prefix,
                        pid,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO projects(
                      id, fingerprint, title, summary, keywords, key_prefix, model,
                      first_seen, last_seen, request_count, client_ips
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        pid,
                        fp,
                        title,
                        summary[:1000],
                        json.dumps(kws),
                        key_prefix,
                        model,
                        now,
                        now,
                        1,
                        json.dumps([client_ip] if client_ip else []),
                    ),
                )

            conn.execute(
                """
                INSERT INTO requests(
                  id, ts, project_id, client_ip, method, path, status, duration_ms,
                  model, key_prefix, streaming, request_body, response_body,
                  messages_json, assistant_text, user_text,
                  prompt_tokens, completion_tokens, total_tokens, error
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rid,
                    now,
                    pid,
                    client_ip,
                    method,
                    path,
                    status,
                    duration_ms,
                    model,
                    key_prefix,
                    1 if streaming else 0,
                    json.dumps(req_body, ensure_ascii=False)[:MAX_BODY]
                    if req_body is not None
                    else "",
                    (resp_raw or "")[:MAX_BODY],
                    json.dumps(messages, ensure_ascii=False)[:MAX_BODY],
                    assistant[:8000],
                    user_text[:8000],
                    pt,
                    ct,
                    tt,
                    error[:500],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    return rid


# ── analytics queries ────────────────────────────────────────────────────────
def q_overview(hours: int = 24) -> dict[str, Any]:
    cutoff = _utc_iso(_utc_now() - timedelta(hours=hours))
    with _db_lock:
        conn = db()
        try:
            total = conn.execute(
                "SELECT COUNT(*) c FROM requests WHERE ts>=?", (cutoff,)
            ).fetchone()["c"]
            ok = conn.execute(
                "SELECT COUNT(*) c FROM requests WHERE ts>=? AND status>=200 AND status<400",
                (cutoff,),
            ).fetchone()["c"]
            tokens = conn.execute(
                "SELECT COALESCE(SUM(total_tokens),0) t FROM requests WHERE ts>=?",
                (cutoff,),
            ).fetchone()["t"]
            projects = conn.execute(
                "SELECT COUNT(DISTINCT project_id) c FROM requests WHERE ts>=?",
                (cutoff,),
            ).fetchone()["c"]
            ips = conn.execute(
                "SELECT COUNT(DISTINCT client_ip) c FROM requests WHERE ts>=?",
                (cutoff,),
            ).fetchone()["c"]
            # hourly series
            rows = conn.execute(
                "SELECT ts, total_tokens, status FROM requests WHERE ts>=? ORDER BY ts",
                (cutoff,),
            ).fetchall()
            series: dict[str, dict[str, int]] = defaultdict(
                lambda: {"requests": 0, "tokens": 0, "errors": 0}
            )
            for r in rows:
                # bucket by hour
                hour = (r["ts"] or "")[:13]  # YYYY-MM-DDTHH
                series[hour]["requests"] += 1
                series[hour]["tokens"] += int(r["total_tokens"] or 0)
                if int(r["status"] or 0) >= 400:
                    series[hour]["errors"] += 1
            labels = sorted(series.keys())
            top_projects = conn.execute(
                """
                SELECT p.id, p.title, p.summary, p.ai_summary, p.request_count,
                       p.last_seen, p.keywords, p.client_ips, p.model, p.key_prefix,
                       (SELECT COUNT(*) FROM requests r WHERE r.project_id=p.id AND r.ts>=?) recent
                FROM projects p
                ORDER BY recent DESC, p.last_seen DESC
                LIMIT 12
                """,
                (cutoff,),
            ).fetchall()
            top_ips = conn.execute(
                """
                SELECT client_ip, COUNT(*) c, SUM(total_tokens) t
                FROM requests WHERE ts>=?
                GROUP BY client_ip ORDER BY c DESC LIMIT 10
                """,
                (cutoff,),
            ).fetchall()
            top_models = conn.execute(
                """
                SELECT model, COUNT(*) c FROM requests WHERE ts>=?
                GROUP BY model ORDER BY c DESC LIMIT 8
                """,
                (cutoff,),
            ).fetchall()
            return {
                "hours": hours,
                "total_requests": total,
                "ok_requests": ok,
                "error_requests": total - ok,
                "total_tokens": tokens,
                "project_count": projects,
                "unique_ips": ips,
                "series": {
                    "labels": labels,
                    "requests": [series[h]["requests"] for h in labels],
                    "tokens": [series[h]["tokens"] for h in labels],
                    "errors": [series[h]["errors"] for h in labels],
                },
                "top_projects": [dict(r) for r in top_projects],
                "top_ips": [dict(r) for r in top_ips],
                "top_models": [dict(r) for r in top_models],
            }
        finally:
            conn.close()


def q_projects(limit: int = 50) -> list[dict[str, Any]]:
    with _db_lock:
        conn = db()
        try:
            rows = conn.execute(
                """
                SELECT * FROM projects
                ORDER BY last_seen DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def q_project(pid: str) -> dict[str, Any] | None:
    with _db_lock:
        conn = db()
        try:
            p = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            if not p:
                return None
            reqs = conn.execute(
                """
                SELECT id, ts, client_ip, path, status, duration_ms, model, key_prefix,
                       streaming, user_text, assistant_text, total_tokens, error
                FROM requests WHERE project_id=?
                ORDER BY ts DESC LIMIT 100
                """,
                (pid,),
            ).fetchall()
            # daily series for this project
            series_rows = conn.execute(
                """
                SELECT substr(ts,1,10) day, COUNT(*) c, SUM(total_tokens) t
                FROM requests WHERE project_id=?
                GROUP BY day ORDER BY day
                """,
                (pid,),
            ).fetchall()
            return {
                "project": dict(p),
                "requests": [dict(r) for r in reqs],
                "series": {
                    "labels": [r["day"] for r in series_rows],
                    "requests": [r["c"] for r in series_rows],
                    "tokens": [int(r["t"] or 0) for r in series_rows],
                },
            }
        finally:
            conn.close()


def q_request(rid: str) -> dict[str, Any] | None:
    with _db_lock:
        conn = db()
        try:
            r = conn.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def summarize_project(pid: str) -> dict[str, Any]:
    """Call Grok to summarize what a project is doing based on its transcripts."""
    if not GROK_API_KEY:
        return {"error": "GROK_API_KEY not set — configure it in the environment to enable AI summaries"}
    data = q_project(pid)
    if not data:
        return {"error": "project not found"}
    project = data["project"]
    reqs = data["requests"]
    if not reqs:
        return {"error": "no requests to summarize"}

    # Build a compact transcript sample (latest 10 exchanges)
    excerpts: list[str] = []
    for r in reqs[:10]:
        user = (r.get("user_text") or "").strip()
        asst = (r.get("assistant_text") or "").strip()
        if user:
            excerpts.append(f"User: {user[:500]}")
        if asst:
            excerpts.append(f"Assistant: {asst[:500]}")
    if not excerpts:
        return {"error": "no transcript text available to summarize"}

    title = project.get("title") or "Unknown"
    existing_summary = project.get("summary") or ""
    keywords = project.get("keywords") or "[]"
    transcript_block = "\n".join(excerpts)[:4000]

    prompt = (
        f"You are analysing API traffic captured by a proxy. Below is a sample of "
        f"conversations from a detected project.\n\n"
        f"Project title: {title}\n"
        f"Auto-detected summary: {existing_summary}\n"
        f"Keywords: {keywords}\n\n"
        f"Transcript excerpts (latest first):\n{transcript_block}\n\n"
        f"In 2-3 sentences, describe what this project is doing: what kind of app/bot "
        f"it is, what it helps users with, and any notable patterns. Be concise and factual."
    )

    payload = json.dumps({
        "model": GROK_SUMMARY_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.3,
    }).encode("utf-8")

    url = UPSTREAM + "/v1/chat/completions"
    req = Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {GROK_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=60.0) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        ai_summary = (
            raw.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not ai_summary:
            return {"error": "Grok returned an empty summary"}
    except HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        return {"error": f"Grok API error (HTTP {e.code}): {err}"}
    except Exception as e:
        return {"error": f"failed to call Grok: {e}"}

    # Persist the summary
    with _db_lock:
        conn = db()
        try:
            conn.execute(
                "UPDATE projects SET ai_summary=? WHERE id=?",
                (ai_summary[:2000], pid),
            )
            conn.commit()
        finally:
            conn.close()

    return {"ai_summary": ai_summary, "project_id": pid}


# ── proxy ────────────────────────────────────────────────────────────────────
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


def _upstream_request_headers(
    headers: dict[str, str],
    *,
    body: bytes | None,
    method: str,
) -> dict[str, str]:
    req_headers = {
        k: v
        for k, v in headers.items()
        if k.lower() not in HOP_BY_HOP and not k.lower().startswith("proxy-")
    }
    if body is not None and method not in ("GET", "HEAD"):
        req_headers = {
            k: v for k, v in req_headers.items() if k.lower() != "content-length"
        }
        req_headers["Content-Length"] = str(len(body))
    return req_headers


def _do_upstream_once(
    method: str,
    path_q: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, list[tuple[str, str]], bytes]:
    url = UPSTREAM + path_q
    req_headers = _upstream_request_headers(headers, body=body, method=method)
    req = Request(url, data=body if method not in ("GET", "HEAD") else None, method=method)
    for k, v in req_headers.items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read()
            resp_headers = [
                (k, v)
                for k, v in resp.headers.items()
                if k.lower() not in HOP_BY_HOP
            ]
            return resp.status, resp_headers, resp_body
    except HTTPError as e:
        resp_body = e.read()
        resp_headers = [
            (k, v) for k, v in e.headers.items() if k.lower() not in HOP_BY_HOP
        ]
        return e.code, resp_headers, resp_body


def proxy_to_upstream(
    method: str,
    path_q: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float = 600.0,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Buffered proxy (non-stream). Retries transient 502/503/capacity errors.

    On final failure, normalizes body to OpenAI `{error:{message,type,code}}`
    so OpenCode/Zod does not throw invalid_union on xAI `{type:error,...}`.
    """
    last: tuple[int, list[tuple[str, str]], bytes] | None = None
    for attempt in range(1, UPSTREAM_RETRY_MAX + 1):
        status, resp_headers, resp_body = _do_upstream_once(
            method, path_q, headers, body, timeout
        )
        last = (status, resp_headers, resp_body)
        if not _is_upstream_transient_fail(status, resp_body):
            # success path, or non-retryable error — still normalize weird errors
            if status >= 400 or _is_upstream_transient_fail(status, resp_body):
                return _normalize_error_response(status, resp_body, path_q)
            # HTTP 200 but body is xAI type=error (capacity) — already handled above
            # HTTP 200 with type=error non-transient
            try:
                obj = json.loads(resp_body.decode("utf-8", errors="replace"))
                if isinstance(obj, dict) and str(obj.get("type") or "").lower() == "error":
                    return _normalize_error_response(status, resp_body, path_q)
            except Exception:
                pass
            return status, resp_headers, resp_body
        sys.stderr.write(
            f"[retry] upstream transient {status} (attempt {attempt}/"
            f"{UPSTREAM_RETRY_MAX}) path={path_q} - retry in "
            f"{UPSTREAM_RETRY_DELAY}s\n"
        )
        if attempt >= UPSTREAM_RETRY_MAX:
            break
        # slight backoff so capacity can clear
        time.sleep(UPSTREAM_RETRY_DELAY * (1.0 + 0.15 * (attempt - 1)))
    assert last is not None
    return _normalize_error_response(last[0], last[2], path_q)


def open_upstream(
    method: str,
    path_q: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float = 600.0,
):
    """Open upstream connection without reading body (for SSE streaming)."""
    url = UPSTREAM + path_q
    req_headers = _upstream_request_headers(headers, body=body, method=method)
    req = Request(url, data=body if method not in ("GET", "HEAD") else None, method=method)
    for k, v in req_headers.items():
        req.add_header(k, v)
    return urlopen(req, timeout=timeout)


def is_stream_request(body: bytes | None) -> bool:
    if not body:
        return False
    try:
        obj = json.loads(body.decode("utf-8"))
        return bool(isinstance(obj, dict) and obj.get("stream") is True)
    except Exception:
        return False


# ── Image gen/edit bridge (Grok free path via /v1/responses + image_generation) ─
def _strip_data_url(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("data:") and "," in value:
        return value.split(",", 1)[1]
    return value


def _normalize_image_ref(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, dict):
        if value.get("url"):
            return _normalize_image_ref(value["url"])
        if value.get("image_url"):
            return _normalize_image_ref(value["image_url"])
        raw = value.get("b64_json") or value.get("base64") or value.get("data")
        if raw:
            return "data:image/png;base64," + _strip_data_url(str(raw))
        return None
    value = str(value).strip()
    if value.startswith(("data:", "http://", "https://")):
        return value
    return "data:image/png;base64," + _strip_data_url(value)


def _collect_image_refs(
    image: Any = None,
    images: Any = None,
    image_url: Any = None,
    max_refs: int = 3,
) -> list[str]:
    values: list[Any] = []
    if image is not None:
        values.append(image)
    if image_url is not None:
        values.append(image_url)
    if isinstance(images, list):
        values.extend(images)
    elif images is not None:
        values.append(images)
    refs: list[str] = []
    for value in values:
        normalized = _normalize_image_ref(value)
        if normalized:
            refs.append(normalized)
        if len(refs) >= max_refs:
            break
    return refs


def _extract_generated_images(response: dict[str, Any]) -> list[str]:
    images: list[str] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = str(item.get("type") or "")
        if itype not in ("image_generation_call", "image_generation", "image_gen_call"):
            continue
        raw = item.get("result") or item.get("image") or ""
        if isinstance(raw, dict):
            raw = (
                raw.get("b64_json")
                or raw.get("base64")
                or raw.get("data")
                or raw.get("result")
                or ""
            )
        raw = _strip_data_url(str(raw)) if raw else ""
        if raw:
            images.append(raw)
    return images


def _normalize_image_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    usage = usage if isinstance(usage, dict) else {}
    tin = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    tout = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (tin + tout) or 0)
    return {
        "input_tokens": tin,
        "output_tokens": tout,
        "prompt_tokens": tin,
        "completion_tokens": tout,
        "total_tokens": total,
    }


def build_image_tool_responses_body(
    *,
    prompt: str,
    image_refs: list[str] | None = None,
    model: str = "grok-4.5",
    effort: str = "high",
    quality: str = "max",
) -> dict[str, Any]:
    """Translate OpenAI images API → Grok Responses + image_generation tool."""
    content: list[dict[str, Any]] = []
    for ref in image_refs or []:
        content.append({"type": "input_image", "image_url": ref})
    q = (quality or "max").strip().lower()
    if q in ("max", "hd", "high", "best"):
        quality_hint = (
            "Highest visual quality: sharp detail, natural lighting, clean composition, "
            "no watermark, no text artifacts, photoreal when appropriate."
        )
    elif q in ("low", "draft", "fast"):
        quality_hint = "Fast draft quality is fine."
    else:
        quality_hint = "Good balanced quality."
    if image_refs:
        text = (
            f"Edit this image according to the instruction. {quality_hint} "
            f"Use the image_generation tool once and return the image. Instruction: {prompt}"
        )
    else:
        text = (
            f"Generate one image. {quality_hint} "
            f"Use the image_generation tool once and return the image. Prompt: {prompt}"
        )
    content.append({"type": "input_text", "text": text})
    eff = effort if effort in ("low", "medium", "high") else "high"
    return {
        "model": model or "grok-4.5",
        "input": [{"role": "user", "content": content}],
        "tools": [{"type": "image_generation"}],
        "tool_choice": {"type": "image_generation"},
        "stream": False,
        "reasoning": {"effort": eff},
        "max_output_tokens": 2048 if eff == "high" else 1024,
    }


def handle_images_api(
    path: str,
    body: bytes | None,
    headers: dict[str, str],
) -> tuple[int, list[tuple[str, str]], bytes] | None:
    """If path is images generations/edits, bridge via /v1/responses tool. Else None."""
    p = path.rstrip("/")
    is_gen = p.endswith("/v1/images/generations") or p == "/v1/images/generations"
    is_edit = p.endswith("/v1/images/edits") or p == "/v1/images/edits"
    if not (is_gen or is_edit):
        return None
    try:
        obj = json.loads((body or b"{}").decode("utf-8"))
    except Exception:
        return (
            400,
            [("Content-Type", "application/json; charset=utf-8")],
            json.dumps({"error": {"message": "invalid JSON", "type": "invalid_request_error"}}).encode(),
        )
    if not isinstance(obj, dict):
        return (
            400,
            [("Content-Type", "application/json; charset=utf-8")],
            json.dumps({"error": {"message": "body must be object", "type": "invalid_request_error"}}).encode(),
        )

    prompt = str(obj.get("prompt") or obj.get("text") or "").strip()
    if not prompt:
        return (
            400,
            [("Content-Type", "application/json; charset=utf-8")],
            json.dumps({"error": {"message": "prompt is required", "type": "invalid_request_error"}}).encode(),
        )

    refs = _collect_image_refs(
        image=obj.get("image"),
        images=obj.get("images"),
        image_url=obj.get("image_url"),
        max_refs=3,
    )
    # generations with image field → treat as edit (compat)
    if is_edit and not refs:
        return (
            400,
            [("Content-Type", "application/json; charset=utf-8")],
            json.dumps({"error": {"message": "image is required", "type": "invalid_request_error"}}).encode(),
        )
    if is_gen and not refs:
        refs = []

    model = str(obj.get("model") or "grok-4.5")
    # map imagine model names to grok-4.5 for free tool path
    if "imagine" in model.lower() or model not in ("grok-4.5", "grok-4", "grok-3"):
        model = "grok-4.5"
    effort = str(obj.get("effort") or obj.get("quality") or "high")
    if effort in ("max", "hd", "best"):
        effort = "high"
    quality = str(obj.get("quality") or ("max" if effort == "high" else "standard"))

    last_err_body = b""
    last_status = 502
    last_headers: list[tuple[str, str]] = [
        ("Content-Type", "application/json; charset=utf-8")
    ]
    images: list[str] = []
    upstream: dict[str, Any] = {}
    for attempt in range(1, UPSTREAM_RETRY_MAX + 1):
        upstream_body = build_image_tool_responses_body(
            prompt=prompt,
            image_refs=refs or None,
            model=model,
            effort=effort,
            quality=quality,
        )
        raw = json.dumps(upstream_body, ensure_ascii=False).encode("utf-8")
        status, resp_headers, resp_body = proxy_to_upstream(
            "POST",
            "/v1/responses",
            headers,
            raw,
            timeout=240.0,
        )
        last_status, last_headers, last_err_body = status, resp_headers, resp_body
        if status >= 400:
            if _is_upstream_transient_fail(status, resp_body) and attempt < UPSTREAM_RETRY_MAX:
                sys.stderr.write(
                    f"[retry] image gen transient {status} attempt {attempt}/"
                    f"{UPSTREAM_RETRY_MAX}\n"
                )
                time.sleep(UPSTREAM_RETRY_DELAY)
                continue
            return status, resp_headers, resp_body
        try:
            parsed = json.loads(resp_body.decode("utf-8", errors="replace"))
        except Exception:
            if attempt < UPSTREAM_RETRY_MAX:
                time.sleep(UPSTREAM_RETRY_DELAY)
                continue
            return (
                502,
                [("Content-Type", "application/json; charset=utf-8")],
                json.dumps(
                    {"error": {"message": "invalid upstream response", "type": "server_error"}}
                ).encode(),
            )
        if not isinstance(parsed, dict):
            if attempt < UPSTREAM_RETRY_MAX:
                time.sleep(UPSTREAM_RETRY_DELAY)
                continue
            return (
                502,
                [("Content-Type", "application/json; charset=utf-8")],
                json.dumps(
                    {"error": {"message": "invalid upstream response", "type": "server_error"}}
                ).encode(),
            )
        upstream = parsed
        images = _extract_generated_images(upstream)
        if images:
            break
        # filter / empty tool call — rewrite prompt slightly and retry
        sys.stderr.write(
            f"[retry] image gen no image_generation_call attempt {attempt}/"
            f"{UPSTREAM_RETRY_MAX}\n"
        )
        if attempt < UPSTREAM_RETRY_MAX:
            prompt = (
                f"{prompt}. Artistic high-detail rendering, clean composition, "
                f"no text overlay, photorealistic lighting."
            )
            time.sleep(UPSTREAM_RETRY_DELAY)
            continue
    if not images:
        return (
            last_status if last_status >= 400 else 502,
            last_headers,
            last_err_body
            if last_status >= 400
            else json.dumps(
                {
                    "error": {
                        "message": "no image_generation_call in upstream response after retries",
                        "type": "server_error",
                    }
                }
            ).encode(),
        )
    n = max(1, min(int(obj.get("n") or 1), 4))
    images = images[:n]
    out = {
        "created": int(time.time()),
        "data": [{"b64_json": img} for img in images],
        "usage": _normalize_image_usage(
            upstream.get("usage") if isinstance(upstream, dict) else None
        ),
    }
    return (
        200,
        [("Content-Type", "application/json; charset=utf-8")],
        json.dumps(out, ensure_ascii=False).encode("utf-8"),
    )


def _rewrite_sse_line(line: bytes) -> bytes:
    """Map Grok reasoning_content → common delta fields for UI Thought panels.

    Keeps original reasoning_content; also mirrors into delta.reasoning when
    clients expect that key. Does not touch non-data lines.
    """
    if not line.startswith(b"data:"):
        return line
    raw = line[5:].strip()
    if not raw or raw == b"[DONE]":
        return line if line.endswith(b"\n") else line + b"\n"
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return line if line.endswith(b"\n") else line + b"\n"
    if not isinstance(obj, dict):
        return line if line.endswith(b"\n") else line + b"\n"
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices:
        return line if line.endswith(b"\n") else line + b"\n"
    ch0 = choices[0]
    if not isinstance(ch0, dict):
        return line if line.endswith(b"\n") else line + b"\n"
    delta = ch0.get("delta")
    if not isinstance(delta, dict):
        return line if line.endswith(b"\n") else line + b"\n"
    rc = delta.get("reasoning_content")
    if isinstance(rc, str) and rc:
        # Common aliases used by OpenAI-compatible UIs
        if "reasoning" not in delta:
            delta["reasoning"] = rc
        if "thinking" not in delta:
            delta["thinking"] = rc
        ch0["delta"] = delta
        choices[0] = ch0
        obj["choices"] = choices
        try:
            out = b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
            return out
        except Exception:
            return line if line.endswith(b"\n") else line + b"\n"
    return line if line.endswith(b"\n") else line + b"\n"


def should_log_path(path: str) -> bool:
    return path.startswith("/v1/") or path.startswith("/api/")


def _resolve_mode(headers: dict[str, str] | None, obj: dict[str, Any]) -> str:
    """Pick prompt mode: header X-Grok-Mode wins, else body grok_mode, else code."""
    mode = ""
    if headers:
        for k, v in headers.items():
            if k.lower() in ("x-grok-mode", "x-grok-farm-mode"):
                mode = (v or "").strip().lower()
                break
    if not mode and isinstance(obj.get("grok_mode"), str):
        mode = obj["grok_mode"].strip().lower()
    if mode in ("general", "gen", "chat"):
        return "general"
    if mode in ("research", "analyze", "analysis"):
        return "research"
    return "code"


_CLIPBOARD_POISON_RE = re.compile(
    r"(?is)ERROR:\s*Cannot read\s*[\"']?clipboard[\"']?.*?(?:image input\)\.?|Inform the user\.?)"
)
_CLIPBOARD_POISON_SHORT_RE = re.compile(
    r"(?is)(?:this model does not support image input\.?|Cannot read\s*[\"']?clipboard[\"']?\.?)"
)
_INFORM_USER_ONLY_RE = re.compile(r"(?is)^\s*Inform the user\.?\s*$")
_VISION_HINT = (
    "[vision-fix] Multimodal image parts (image_url / data:image) in this request "
    "ARE real pixels. Describe them. NEVER say you cannot read images. "
    "NEVER call filesystem Read/cat on clipboard, Temp\\\\opencode\\\\tool-content, "
    "or bare filenames — use the image parts in the message instead."
)


def _content_has_image(content: Any) -> bool:
    if isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            t = str(p.get("type") or "").lower()
            if t in ("image_url", "image", "input_image"):
                return True
            if "image_url" in p or "image" in p:
                return True
    if isinstance(content, str) and "data:image" in content:
        return True
    return False


def _messages_have_image(msgs: list[Any]) -> bool:
    for m in msgs:
        if isinstance(m, dict) and _content_has_image(m.get("content")):
            return True
    return False


def _scrub_poison_text(text: str) -> str:
    if not text:
        return text
    out = _CLIPBOARD_POISON_RE.sub("", text)
    out = _CLIPBOARD_POISON_SHORT_RE.sub("", out)
    if _INFORM_USER_ONLY_RE.match(out.strip()):
        return ""
    # OpenCode sometimes injects a standalone poison line as its own part
    lines = []
    for line in out.splitlines():
        low = line.strip().lower()
        if "cannot read" in low and "clipboard" in low:
            continue
        if "does not support image input" in low:
            continue
        if low in ("inform the user.", "inform the user"):
            continue
        lines.append(line)
    out = "\n".join(lines).strip()
    return out


def _sanitize_content_parts(content: Any) -> tuple[Any, bool]:
    """Remove OpenCode clipboard poison; keep real image_url parts. Returns (content, changed)."""
    changed = False
    if isinstance(content, str):
        scrubbed = _scrub_poison_text(content)
        if scrubbed != content:
            changed = True
        return scrubbed, changed
    if not isinstance(content, list):
        return content, False
    new_parts: list[Any] = []
    for p in content:
        if not isinstance(p, dict):
            new_parts.append(p)
            continue
        t = str(p.get("type") or "").lower()
        if t in ("image_url", "image", "input_image"):
            new_parts.append(p)
            continue
        # text-like parts
        text = p.get("text")
        if isinstance(text, str):
            scrubbed = _scrub_poison_text(text)
            if scrubbed != text:
                changed = True
            if not scrubbed.strip():
                changed = True
                continue  # drop empty poison-only part
            q = dict(p)
            q["text"] = scrubbed
            new_parts.append(q)
            continue
        new_parts.append(p)
    if len(new_parts) != len(content):
        changed = True
    return new_parts, changed


def _sanitize_messages_vision(msgs: list[Any]) -> tuple[list[Any], bool, bool]:
    """Scrub poison + detect images. Returns (msgs, changed, has_image)."""
    changed = False
    has_image = False
    out: list[Any] = []
    for m in msgs:
        if not isinstance(m, dict):
            out.append(m)
            continue
        mm = dict(m)
        content = mm.get("content")
        if _content_has_image(content):
            has_image = True
        new_c, ch = _sanitize_content_parts(content)
        if ch:
            mm["content"] = new_c
            changed = True
        # drop user/assistant messages that became empty after scrub
        c2 = mm.get("content")
        if c2 == "" or c2 == []:
            if str(mm.get("role") or "") in ("user", "assistant", "tool"):
                changed = True
                continue
        out.append(mm)
    return out, changed, has_image


def enhance_request_body(
    path: str,
    body: bytes | None,
    headers: dict[str, str] | None = None,
) -> bytes | None:
    """Inject server system prompt + default decoding params for chat APIs.

    Also sanitizes OpenCode Windows clipboard poison and steers vision so the
    model reads image_url parts instead of trying filesystem Read on temp paths.

    Applies to OpenAI-style /v1/chat/completions (and similar JSON chat payloads).
    Does not strip client system prompts — prepends farm boost if missing.
    CRITICAL: never remove tools / tool_choice / functions (OpenCode agent).
    """
    if not body:
        return body
    if not (
        path.startswith("/v1/chat/completions")
        or path.startswith("/v1/messages")
        or path.rstrip("/").endswith("/chat/completions")
    ):
        return body
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    if not isinstance(obj, dict):
        return body

    # Preserve agent fields explicitly (never drop)
    _agent_keys = (
        "tools",
        "tool_choice",
        "functions",
        "function_call",
        "parallel_tool_calls",
        "response_format",
        "stream",
        "stream_options",
    )
    agent_snapshot = {k: obj[k] for k in _agent_keys if k in obj}

    changed = False
    has_image = False

    # Always sanitize messages (even if GROK_ENHANCE is off)
    msgs = obj.get("messages")
    if isinstance(msgs, list):
        new_msgs, ch, has_image = _sanitize_messages_vision(msgs)
        if ch:
            obj["messages"] = new_msgs
            msgs = new_msgs
            changed = True
        # re-detect after scrub
        has_image = _messages_have_image(msgs) if isinstance(msgs, list) else has_image

    if GROK_ENHANCE:
        mode = _resolve_mode(headers, obj)
        prompt = GROK_MODE_PROMPTS.get(mode) or GROK_SYSTEM_PROMPT
        if has_image and prompt:
            if _VISION_HINT not in prompt:
                prompt = prompt.rstrip() + "\n\n" + _VISION_HINT

        # Drop non-upstream helper key if present
        if "grok_mode" in obj:
            obj.pop("grok_mode", None)
            changed = True

        is_anthropic = path.startswith("/v1/messages") or path.rstrip("/").endswith(
            "/messages"
        )

        # Anthropic Messages API: top-level `system` (string or content blocks)
        if prompt and is_anthropic:
            sys_val = obj.get("system")
            if isinstance(sys_val, str):
                if _SYSTEM_MARKER not in sys_val and "grok-farm-boost" not in sys_val:
                    obj["system"] = prompt + "\n\n" + sys_val
                    changed = True
                elif has_image and _VISION_HINT not in sys_val:
                    obj["system"] = sys_val.rstrip() + "\n\n" + _VISION_HINT
                    changed = True
            elif isinstance(sys_val, list):
                # content-block system — prepend text block if boost missing
                joined = json.dumps(sys_val, ensure_ascii=False)
                if _SYSTEM_MARKER not in joined and "grok-farm-boost" not in joined:
                    obj["system"] = [{"type": "text", "text": prompt}, *sys_val]
                    changed = True
            elif sys_val is None:
                obj["system"] = prompt
                changed = True

        # OpenAI chat format — inject system boost (skip for Anthropic messages)
        if prompt and (not is_anthropic) and isinstance(msgs, list):
            already = False
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                if str(m.get("role") or "") != "system":
                    continue
                content = m.get("content")
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                if _SYSTEM_MARKER in text or "grok-farm-boost" in text:
                    already = True
                    # ensure vision hint present when images exist
                    if has_image and _VISION_HINT not in text and isinstance(content, str):
                        m["content"] = content.rstrip() + "\n\n" + _VISION_HINT
                        changed = True
                    break
            if not already:
                obj["messages"] = [{"role": "system", "content": prompt}, *msgs]
                changed = True
                msgs = obj["messages"]

        # Defaults for decoding (only if client omitted, unless FORCE)
        if GROK_FORCE_DEFAULTS or "temperature" not in obj:
            obj["temperature"] = GROK_DEFAULT_TEMPERATURE
            changed = True
        if GROK_FORCE_DEFAULTS or (
            "max_tokens" not in obj and "max_completion_tokens" not in obj
        ):
            obj["max_tokens"] = GROK_DEFAULT_MAX_TOKENS
            changed = True

    # When images present: soft-steer tool_choice away from forced tool use
    # so the model answers from vision first (tools still available if needed).
    if has_image and obj.get("tool_choice") in ("required", "any"):
        obj["tool_choice"] = "auto"
        changed = True

    # Restore agent keys if anything weird happened (except tool_choice we may have softened)
    for k, v in agent_snapshot.items():
        if k == "tool_choice" and has_image:
            continue
        if k not in obj:
            obj[k] = v
            changed = True

    if not changed:
        return body
    try:
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except Exception:
        return body


# ── UI ───────────────────────────────────────────────────────────────────────
def _load_ui() -> str:
    """Load dashboard HTML (neo-brutalism UI lives in static_intel_ui.html)."""
    candidates = [
        Path(__file__).resolve().parent / "static_intel_ui.html",
    ]
    custom = _env("TRAFFIC_UI_PATH")
    if custom:
        candidates.insert(0, Path(custom))
    for c in candidates:
        if c and str(c) != "." and c.is_file():
            return c.read_text(encoding="utf-8")
    return "<!doctype html><h1>Traffic Intel UI missing: static_intel_ui.html</h1>"


UI = _load_ui()


# ── HTTP handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "TrafficIntel/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(code)
        headers = headers or []
        has_len = any(k.lower() == "content-length" for k, _ in headers)
        has_ct = any(k.lower() == "content-type" for k, _ in headers)
        for k, v in headers:
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        if not has_len:
            self.send_header("Content-Length", str(len(body)))
        if not has_ct and body:
            self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, raw, [("Content-Type", "application/json; charset=utf-8")])

    def _html(self, html: str) -> None:
        self._send(200, html.encode("utf-8"), [("Content-Type", "text/html; charset=utf-8")])

    def _discard_body(self, n: int) -> None:
        remaining = max(0, n)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _read_body(self) -> bytes | None:
        """Read request body. Returns None when Content-Length exceeds MAX_BODY."""
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        if n > MAX_BODY:
            # Drain socket so the client gets a clean 413 instead of a hang/partial proxy.
            self._discard_body(n)
            return None
        return self.rfile.read(n)

    def _reject_payload_too_large(self) -> None:
        self._json(
            413,
            {
                "error": "payload_too_large",
                "message": "Payload Too Large",
                "max_body_mb": MAX_BODY // (1024 * 1024),
            },
        )

    def _token(self) -> str | None:
        ck = self.headers.get("Cookie") or ""
        if ck:
            c = SimpleCookie()
            try:
                c.load(ck)
                if "traffic_token" in c:
                    return c["traffic_token"].value
            except Exception:
                pass
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer ") and not auth[7:].strip().startswith("g2a_"):
            # only treat as panel token if not an API key-ish value
            tok = auth[7:].strip()
            if verify_token(tok):
                return tok
        return None

    def _auth(self) -> str | None:
        u = verify_token(self._token())
        if not u:
            self._json(401, {"error": "unauthorized"})
            return None
        return u

    def _handle_intel(self) -> bool:
        parsed = urlparse(self.path)
        path = parsed.path
        method = self.command

        if path in ("/intel", "/intel/", "/intel/index.html"):
            if method == "GET":
                self._html(_load_ui())
                return True
            return False

        if path == "/intel/api/health":
            self._json(200, {"ok": True, "upstream": UPSTREAM, "time": _utc_iso()})
            return True

        if path == "/intel/api/login" and method == "POST":
            raw_body = self._read_body()
            if raw_body is None:
                self._reject_payload_too_large()
                return True
            body = {}
            try:
                body = json.loads(raw_body.decode() or "{}")
            except Exception:
                body = {}
            if not PANEL_PASSWORD:
                self._json(500, {"error": "password not configured"})
                return True
            user = str(body.get("username") or "").strip()
            pw = str(body.get("password") or "")
            if user != PANEL_USER or not hmac.compare_digest(pw, PANEL_PASSWORD):
                time.sleep(0.35)
                self._json(401, {"error": "invalid credentials"})
                return True
            token = issue_token(user)
            raw = json.dumps({"ok": True, "user": user}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header(
                "Set-Cookie",
                f"traffic_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={TOKEN_TTL}",
            )
            self.end_headers()
            self.wfile.write(raw)
            return True

        if path == "/intel/api/logout" and method == "POST":
            if self._read_body() is None:
                self._reject_payload_too_large()
                return True
            raw = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header(
                "Set-Cookie", "traffic_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            )
            self.end_headers()
            self.wfile.write(raw)
            return True

        if path.startswith("/intel/api/"):
            if not self._auth():
                return True
            if path == "/intel/api/overview" and method == "GET":
                qs = parse_qs(parsed.query)
                hours = int((qs.get("hours") or ["168"])[0])
                hours = max(1, min(24 * 60, hours))
                self._json(200, q_overview(hours))
                return True
            if path == "/intel/api/projects" and method == "GET":
                self._json(200, {"projects": q_projects(100)})
                return True
            m = re.match(r"^/intel/api/projects/([^/]+)$", path)
            if m and method == "GET":
                data = q_project(m.group(1))
                if not data:
                    self._json(404, {"error": "not found"})
                else:
                    self._json(200, data)
                return True
            m = re.match(r"^/intel/api/projects/([^/]+)/summarize$", path)
            if m and method == "POST":
                if self._read_body() is None:
                    self._reject_payload_too_large()
                    return True
                self._json(200, summarize_project(m.group(1)))
                return True
            m = re.match(r"^/intel/api/requests/([^/]+)$", path)
            if m and method == "GET":
                data = q_request(m.group(1))
                if not data:
                    self._json(404, {"error": "not found"})
                else:
                    self._json(200, data)
                return True
            self._json(404, {"error": "not found"})
            return True

        return False

    def _log_exchange(
        self,
        *,
        cip: str,
        path: str,
        query: str,
        status: int,
        duration_ms: int,
        body: bytes | None,
        resp_text: str,
        resp_headers: list[tuple[str, str]] | None,
        streaming: bool,
    ) -> None:
        if not should_log_path(path):
            return
        req_obj = None
        if body:
            try:
                req_obj = json.loads(body.decode("utf-8", errors="replace"))
            except Exception:
                req_obj = None
        resp_obj = None
        if not streaming:
            try:
                resp_obj = (
                    json.loads(resp_text)
                    if resp_text.strip().startswith("{")
                    else None
                )
            except Exception:
                resp_obj = None
        model = ""
        if isinstance(req_obj, dict):
            model = str(req_obj.get("model") or "")
        if not model and isinstance(resp_obj, dict):
            model = str(resp_obj.get("model") or "")
        try:
            store_exchange(
                client_ip=cip,
                method=self.command,
                path=path + (("?" + query) if query else ""),
                status=status,
                duration_ms=duration_ms,
                model=model,
                key_prefix=key_prefix_from_auth(
                    self.headers.get("Authorization"), self.headers.get("X-API-Key")
                ),
                streaming=bool(streaming),
                req_body=req_obj if isinstance(req_obj, dict) else None,
                resp_obj=resp_obj if isinstance(resp_obj, dict) else None,
                resp_raw=resp_text,
                error="" if status < 400 else resp_text[:300],
            )
        except Exception as e:
            sys.stderr.write(f"store_exchange error: {e}\n")

    def _maybe_reject_insufficient(self, headers: dict[str, str]) -> bool:
        """Return True if request was rejected (response already sent)."""
        api_key = extract_api_key(headers.get("Authorization"), headers.get("X-API-Key") or headers.get("x-api-key"))
        check = shop_billing_check(api_key)
        if not check or not check.get("shop_key"):
            return False
        if check.get("allowed", True):
            return False
        self._json(
            402,
            {
                "error": {
                    "message": check.get("message")
                    or "Insufficient balance — saldo token habis. Silakan recharge di shop.",
                    "type": "insufficient_balance",
                    "code": "insufficient_balance",
                    "param": None,
                },
                "balance": check.get("balance", 0),
            },
        )
        return True

    def _debit_after(
        self,
        *,
        headers: dict[str, str],
        path: str,
        body: bytes | None,
        resp_text: str,
        streaming: bool,
        status: int,
    ) -> None:
        if status >= 400:
            return
        api_key = extract_api_key(headers.get("Authorization"), headers.get("X-API-Key") or headers.get("x-api-key"))
        if not api_key:
            return
        model = ""
        if body:
            try:
                req_obj = json.loads(body.decode("utf-8", errors="replace"))
                if isinstance(req_obj, dict):
                    model = str(req_obj.get("model") or "")
            except Exception:
                pass
        pt = ct = tt = 0
        if streaming:
            pt, ct, tt = usage_from_sse(resp_text)
        else:
            try:
                resp_obj = json.loads(resp_text) if resp_text.strip().startswith("{") else None
            except Exception:
                resp_obj = None
            pt, ct, tt = usage_tokens(resp_obj if isinstance(resp_obj, dict) else None)
        if tt <= 0:
            # fallback estimate so balance still moves if upstream omits usage
            # ~4 chars/token rough; min 1 if we got any response
            est = max(1, len(resp_text or "") // 4) if resp_text else 0
            if body:
                est += max(1, len(body) // 4)
            tt = est
            pt = pt or est
        if tt > 0:
            shop_billing_debit(
                api_key,
                tokens=tt,
                prompt_tokens=pt,
                completion_tokens=ct,
                path=path,
                model=model,
            )

    def _proxy_stream(
        self,
        *,
        path: str,
        query: str,
        headers: dict[str, str],
        body: bytes | None,
        cip: str,
        t0: float,
    ) -> None:
        """True SSE pass-through: write lines as they arrive; close after real [DONE].

        Critical for OpenCode agents:
        - Do NOT use a short idle timeout (was 45s) — reasoning/tool pauses can be longer.
        - Do NOT inject a fake `data: [DONE]` on timeout/error — that makes OpenCode
          mark the turn complete mid-task (looks like "tiba-tiba done").
        - Only stop on authentic upstream DONE (or client disconnect / hard wall clock).
        """
        collected: list[bytes] = []
        status = 502
        resp_headers: list[tuple[str, str]] = []
        saw_done = False
        saw_finish = False
        idle_timeouts = 0
        resp = None
        try:
            # Retry open on transient 502/503/capacity before any bytes hit the client.
            for _att in range(1, UPSTREAM_RETRY_MAX + 1):
                try:
                    resp = open_upstream(
                        self.command,
                        self.path,
                        headers,
                        body if body else None,
                        timeout=float(STREAM_MAX_WALL_S),
                    )
                    break
                except HTTPError as e:
                    status = e.code
                    err_body = e.read()
                    if not _is_upstream_transient_fail(status, err_body):
                        st_n, hdr_n, body_n = _normalize_error_response(
                            status, err_body, path
                        )
                        self._send(st_n, body_n, hdr_n)
                        self._log_exchange(
                            cip=cip,
                            path=path,
                            query=query,
                            status=st_n,
                            duration_ms=int((time.time() - t0) * 1000),
                            body=body,
                            resp_text=body_n.decode("utf-8", errors="replace"),
                            resp_headers=hdr_n,
                            streaming=True,
                        )
                        return
                    sys.stderr.write(
                        f"[retry] stream open transient {status} (attempt "
                        f"{_att}/{UPSTREAM_RETRY_MAX}) path={path} - retry in "
                        f"{UPSTREAM_RETRY_DELAY}s\n"
                    )
                    if _att >= UPSTREAM_RETRY_MAX:
                        st_n, hdr_n, body_n = _normalize_error_response(
                            status if status >= 400 else 503, err_body, path
                        )
                        self._send(st_n, body_n, hdr_n)
                        self._log_exchange(
                            cip=cip,
                            path=path,
                            query=query,
                            status=st_n,
                            duration_ms=int((time.time() - t0) * 1000),
                            body=body,
                            resp_text=body_n.decode("utf-8", errors="replace"),
                            resp_headers=hdr_n,
                            streaming=True,
                        )
                        return
                    delay = UPSTREAM_RETRY_DELAY * (1.0 + 0.25 * (_att - 1))
                    if _is_upstream_transient_fail(status, err_body):
                        delay = max(delay, 1.5 * _att)
                    time.sleep(delay)
            if resp is None:
                st_n, hdr_n, body_n = _normalize_error_response(
                    503,
                    b'{"message":"upstream unavailable after retries"}',
                    path,
                )
                self._send(st_n, body_n, hdr_n)
                return

            status = getattr(resp, "status", 200) or 200
            # Some gateways return 200 + capacity JSON body (non-SSE).
            ct = ""
            try:
                ct = str(resp.headers.get("Content-Type") or "").lower()
            except Exception:
                ct = ""
            if status >= 400 or (
                "application/json" in ct and "event-stream" not in ct
            ):
                try:
                    peek = resp.read(65536)
                except Exception:
                    peek = b""
                try:
                    resp.close()
                except Exception:
                    pass
                if _is_upstream_transient_fail(status, peek) or status >= 400:
                    # buffered path already retries + normalizes OpenAI error shape
                    st2, hdr2, body2 = proxy_to_upstream(
                        self.command,
                        self.path,
                        headers,
                        body if body else None,
                        timeout=float(min(STREAM_MAX_WALL_S, 180)),
                    )
                    self._send(st2, body2, hdr2)
                    self._log_exchange(
                        cip=cip,
                        path=path,
                        query=query,
                        status=st2,
                        duration_ms=int((time.time() - t0) * 1000),
                        body=body,
                        resp_text=body2.decode("utf-8", errors="replace")[:2000],
                        resp_headers=hdr2,
                        streaming=True,
                    )
                    return
                self._send(
                    status or 200,
                    peek,
                    [("Content-Type", ct or "application/json")],
                )
                return

            resp_headers = [
                (k, v)
                for k, v in resp.headers.items()
                if k.lower() not in HOP_BY_HOP
            ]
            self.send_response(status)
            has_ct = False
            for k, v in resp_headers:
                lk = k.lower()
                if lk in HOP_BY_HOP or lk == "content-length":
                    continue
                if lk == "content-type":
                    has_ct = True
                self.send_header(k, v)
            if not has_ct:
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            # Keep-alive friendly; still close after authentic DONE
            self.send_header("Connection", "close")
            self.end_headers()

            def _set_idle_timeout(seconds: float) -> None:
                try:
                    resp.fp.raw._sock.settimeout(seconds)  # type: ignore[attr-defined]
                except Exception:
                    try:
                        resp.timeout = seconds  # type: ignore[attr-defined]
                    except Exception:
                        pass

            def _sse_keepalive() -> None:
                # Comment frames keep Cloudflare/nginx/client from idle-killing the stream
                # during long reasoning/tool pauses. Not a fake [DONE].
                try:
                    ping = b": keepalive\n\n"
                    self.wfile.write(ping)
                    self.wfile.flush()
                    collected.append(ping)
                except Exception:
                    pass

            _set_idle_timeout(float(STREAM_IDLE_TIMEOUT_S))

            try:
                while True:
                    if time.time() - t0 > STREAM_MAX_WALL_S:
                        sys.stderr.write(
                            f"stream wall clock exceeded {STREAM_MAX_WALL_S}s "
                            f"path={path} saw_done={saw_done} saw_finish={saw_finish}\n"
                        )
                        break
                    try:
                        line = resp.readline()
                        idle_timeouts = 0
                    except Exception as e:
                        # Idle timeout between SSE lines — keep waiting + ping client.
                        # Old server used 45s + fake DONE → OpenCode "interrupted"/done mid-task.
                        idle_timeouts += 1
                        ename = type(e).__name__
                        is_idle = (
                            "timeout" in ename.lower()
                            or "timed out" in str(e).lower()
                            or ename in ("TimeoutError", "socket.timeout")
                        )
                        if is_idle and idle_timeouts <= STREAM_IDLE_CONTINUE_MAX:
                            sys.stderr.write(
                                f"stream idle timeout #{idle_timeouts}/"
                                f"{STREAM_IDLE_CONTINUE_MAX} "
                                f"({STREAM_IDLE_TIMEOUT_S}s) path={path} — keepalive\n"
                            )
                            _sse_keepalive()
                            _set_idle_timeout(float(STREAM_IDLE_TIMEOUT_S))
                            continue
                        sys.stderr.write(
                            f"stream readline end: {ename}: {e} "
                            f"path={path} saw_done={saw_done} saw_finish={saw_finish}\n"
                        )
                        break
                    if not line:
                        break
                    # Normalize SSE reasoning field for broader client support
                    out = _rewrite_sse_line(line)
                    collected.append(out)
                    # Track finish_reason (stop / tool_calls / length) without ending early
                    if (
                        b'"finish_reason":"' in line
                        or b'"finish_reason": "' in line
                    ) and b'"finish_reason":null' not in line and b'"finish_reason": null' not in line:
                        saw_finish = True
                    try:
                        self.wfile.write(out)
                        self.wfile.flush()
                    except Exception:
                        # client gone
                        break
                    # Only authentic OpenAI-compatible end marker ends the stream
                    stripped = line.strip()
                    if stripped == b"data: [DONE]" or stripped == b"data:[DONE]":
                        saw_done = True
                        break
                    if stripped.endswith(b"[DONE]") and stripped.startswith(b"data:"):
                        # tolerate "data: [DONE]\r" etc.
                        payload = stripped[5:].strip()
                        if payload == b"[DONE]":
                            saw_done = True
                            break
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

            # NEVER inject fake DONE unless we already saw a finish_reason from upstream.
            # Fake DONE mid-reasoning/tool-call makes OpenCode mark the turn complete.
            if not saw_done and collected and saw_finish:
                try:
                    tail = b"\n\ndata: [DONE]\n\n"
                    self.wfile.write(tail)
                    self.wfile.flush()
                    collected.append(tail)
                    saw_done = True
                    sys.stderr.write(
                        f"stream: injected DONE after finish_reason (upstream omitted DONE) path={path}\n"
                    )
                except Exception:
                    pass
            elif not saw_done and collected and not saw_finish:
                sys.stderr.write(
                    f"stream: ended WITHOUT DONE/finish_reason path={path} "
                    f"bytes={sum(len(x) for x in collected)} — not injecting fake DONE\n"
                )
        except BrokenPipeError:
            pass
        except Exception as e:
            sys.stderr.write(f"stream proxy error: {e}\n")
            if not collected:
                try:
                    self._json(502, {"error": f"stream proxy failed: {e}"})
                except Exception:
                    pass

        duration = int((time.time() - t0) * 1000)
        resp_text = b"".join(collected).decode("utf-8", errors="replace")
        self._log_exchange(
            cip=cip,
            path=path,
            query=query,
            status=status,
            duration_ms=duration,
            body=body,
            resp_text=resp_text,
            resp_headers=resp_headers,
            streaming=True,
        )
        try:
            self._debit_after(
                headers=headers,
                path=path,
                body=body,
                resp_text=resp_text,
                streaming=True,
                status=status,
            )
        except Exception as e:
            sys.stderr.write(f"debit after stream error: {e}\n")

    def _proxy(self) -> None:
        t0 = time.time()
        parsed = urlparse(self.path)
        path = parsed.path
        if self.command not in ("GET", "HEAD"):
            body = self._read_body()
            if body is None:
                self._reject_payload_too_large()
                return
        else:
            body = b""

        # health passthrough without log noise optional
        headers = {k: v for k, v in self.headers.items()}
        # preserve client IP for upstream + our log
        cip = client_ip_from_headers(self)
        headers["X-Forwarded-For"] = cip
        headers["X-Real-IP"] = cip

        # Shop balance gate (shop-issued g2a keys only)
        if self.command in ("POST", "PUT", "PATCH") and path.startswith("/v1/"):
            if self._maybe_reject_insufficient(headers):
                return

        # Image gen/edit bridge (OpenAI-style → Grok free image_generation tool)
        if self.command == "POST" and (
            path.rstrip("/").endswith("/v1/images/generations")
            or path.rstrip("/").endswith("/v1/images/edits")
            or path in ("/v1/images/generations", "/v1/images/edits")
        ):
            bridged = handle_images_api(path, body, headers)
            if bridged is not None:
                status, resp_headers, resp_body = bridged
                duration = int((time.time() - t0) * 1000)
                self._send(status, resp_body, resp_headers)
                resp_text = resp_body.decode("utf-8", errors="replace")
                self._log_exchange(
                    cip=cip,
                    path=path,
                    query=parsed.query,
                    status=status,
                    duration_ms=duration,
                    body=body,
                    resp_text=resp_text[:2000],  # don't store multi-MB base64
                    resp_headers=resp_headers,
                    streaming=False,
                )
                try:
                    self._debit_after(
                        headers=headers,
                        path=path,
                        body=body,
                        resp_text=resp_text,
                        streaming=False,
                        status=status,
                    )
                except Exception as e:
                    sys.stderr.write(f"debit after images error: {e}\n")
                return

        # Server-side Grok boost (system prompt + defaults) before upstream
        if body and self.command in ("POST", "PUT", "PATCH"):
            body = enhance_request_body(path, body, headers) or body

        # True streaming for chat SSE (OpenCode "typewriter" feel)
        if body and is_stream_request(body):
            self._proxy_stream(
                path=path,
                query=parsed.query,
                headers=headers,
                body=body,
                cip=cip,
                t0=t0,
            )
            return

        status, resp_headers, resp_body = proxy_to_upstream(
            self.command, self.path, headers, body if body else None
        )
        duration = int((time.time() - t0) * 1000)

        # always return upstream response first path
        self._send(status, resp_body, resp_headers)

        resp_text = resp_body.decode("utf-8", errors="replace")
        streaming = any(
            k.lower() == "content-type" and "event-stream" in v.lower()
            for k, v in resp_headers
        )
        self._log_exchange(
            cip=cip,
            path=path,
            query=parsed.query,
            status=status,
            duration_ms=duration,
            body=body,
            resp_text=resp_text,
            resp_headers=resp_headers,
            streaming=bool(streaming),
        )
        try:
            self._debit_after(
                headers=headers,
                path=path,
                body=body,
                resp_text=resp_text,
                streaming=bool(streaming),
                status=status,
            )
        except Exception as e:
            sys.stderr.write(f"debit after proxy error: {e}\n")

    def do_GET(self) -> None:  # noqa: N802
        if self._handle_intel():
            return
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802
        if self._handle_intel():
            return
        self._proxy()

    def do_PUT(self) -> None:  # noqa: N802
        if self._handle_intel():
            return
        self._proxy()

    def do_DELETE(self) -> None:  # noqa: N802
        if self._handle_intel():
            return
        self._proxy()

    def do_PATCH(self) -> None:  # noqa: N802
        if self._handle_intel():
            return
        self._proxy()

    def do_OPTIONS(self) -> None:  # noqa: N802
        # CORS preflight passthrough / allow
        if self.path.startswith("/intel"):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()
            return
        self._proxy()


def main() -> int:
    if not PANEL_PASSWORD:
        print("ERROR: set TRAFFIC_PANEL_PASSWORD / FARM_PANEL_PASSWORD / GROK2API_ADMIN_PASS", flush=True)
        return 1
    init_db()
    retention_cleanup()
    print("Traffic Intel", flush=True)
    print(f"  proxy  : http://{HOST}:{PORT}  →  {UPSTREAM}", flush=True)
    print(f"  UI     : http://{HOST}:{PORT}/intel", flush=True)
    print(f"  db     : {DB_PATH}", flush=True)
    print(f"  user   : {PANEL_USER}", flush=True)
    print(
        "  NOTE   : Point API clients at this port so traffic is captured.",
        flush=True,
    )
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
