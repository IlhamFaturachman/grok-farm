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
# API key for calling Grok to summarize projects (generate in grok2api admin panel)
GROK_API_KEY = _env("GROK_API_KEY")
GROK_SUMMARY_MODEL = _env("GROK_SUMMARY_MODEL", "grok-4.5")

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


def proxy_to_upstream(
    method: str,
    path_q: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float = 600.0,
) -> tuple[int, list[tuple[str, str]], bytes]:
    url = UPSTREAM + path_q
    req_headers = {
        k: v
        for k, v in headers.items()
        if k.lower() not in HOP_BY_HOP and not k.lower().startswith("proxy-")
    }
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


def should_log_path(path: str) -> bool:
    return path.startswith("/v1/") or path.startswith("/api/")


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

        status, resp_headers, resp_body = proxy_to_upstream(
            self.command, self.path, headers, body if body else None
        )
        duration = int((time.time() - t0) * 1000)

        # always return upstream response first path
        self._send(status, resp_body, resp_headers)

        if not should_log_path(path):
            return

        # parse bodies for intel
        req_obj = None
        if body:
            try:
                req_obj = json.loads(body.decode("utf-8", errors="replace"))
            except Exception:
                req_obj = None
        resp_text = resp_body.decode("utf-8", errors="replace")
        resp_obj = None
        streaming = "text/event-stream" in (self.headers.get("Accept") or "") or any(
            k.lower() == "content-type" and "event-stream" in v.lower()
            for k, v in resp_headers
        )
        if not streaming:
            try:
                resp_obj = json.loads(resp_text) if resp_text.strip().startswith("{") else None
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
                path=path + (("?" + parsed.query) if parsed.query else ""),
                status=status,
                duration_ms=duration,
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
