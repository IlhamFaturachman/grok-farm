#!/usr/bin/env python3
"""
Small web control panel for grok-farm on a VPS.

  - Login (token cookie)
  - Status: farm running?, latest batch, g2a health
  - Start farm job (n, concurrent, non-interactive)
  - Stop farm job
  - Live log tail
  - List recent batches

Run:
  python farm_panel.py
  # or: FARM_PANEL_PORT=9000 FARM_PANEL_PASSWORD=secret python farm_panel.py

Env:
  FARM_PANEL_HOST=0.0.0.0
  FARM_PANEL_PORT=9000
  FARM_PANEL_PASSWORD=...          # required (or GROK2API_ADMIN_PASS fallback)
  FARM_PANEL_USER=admin
  FARM_PANEL_TOKEN_TTL=86400
  FARM_DIR=/opt/grok-farm          # auto-detected from script dir
  GROK2API_URL=http://127.0.0.1:8000
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# ── paths / env ──────────────────────────────────────────────────────────────
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


FARM_DIR = Path(_env("FARM_DIR", str(_ROOT))).resolve()
HOST = _env("FARM_PANEL_HOST", "0.0.0.0")
PORT = _env_int("FARM_PANEL_PORT", 9000)
PANEL_USER = _env("FARM_PANEL_USER", "admin")
PANEL_PASSWORD = _env("FARM_PANEL_PASSWORD") or _env("GROK2API_ADMIN_PASS")
TOKEN_TTL = _env_int("FARM_PANEL_TOKEN_TTL", 86400)
G2A_URL = _env("GROK2API_URL", "http://127.0.0.1:8000").rstrip("/")
RUN_LOG = FARM_DIR / "results" / "run_live.log"
RUN_PID = FARM_DIR / "results" / "run_live.pid"
RUN_META = FARM_DIR / "results" / "run_live.meta.json"
RESULTS = FARM_DIR / "results"

# signing secret for session tokens (derived from password so restarts stay valid-ish)
_SIGN_SECRET = hashlib.sha256(
    (PANEL_PASSWORD or secrets.token_hex(16) + ":farm-panel").encode()
).digest()

_lock = threading.Lock()


# ── helpers ──────────────────────────────────────────────────────────────────
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    import base64

    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_token(username: str) -> str:
    payload = json.dumps(
        {"u": username, "exp": int(time.time()) + TOKEN_TTL, "n": secrets.token_hex(8)},
        separators=(",", ":"),
    ).encode()
    body = _b64url(payload)
    sig = _b64url(hmac.new(_SIGN_SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expect = _b64url(hmac.new(_SIGN_SECRET, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        data = json.loads(_b64url_decode(body))
    except Exception:
        return None
    if int(data.get("exp") or 0) < time.time():
        return None
    return str(data.get("u") or "")


def farm_pid() -> int | None:
    # Prefer pid file, then pgrep (browser farm.py or HTTP farm_http.py)
    if RUN_PID.is_file():
        try:
            pid = int(RUN_PID.read_text().strip().split()[0])
            os.kill(pid, 0)
            return pid
        except Exception:
            pass
    for pattern in ("python farm_http.py", "python farm.py"):
        try:
            out = subprocess.check_output(
                ["pgrep", "-f", pattern], text=True, stderr=subprocess.DEVNULL
            )
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    return int(line)
        except Exception:
            pass
    return None


def farm_running() -> bool:
    return farm_pid() is not None


def read_run_meta() -> dict[str, Any]:
    if RUN_META.is_file():
        try:
            return json.loads(RUN_META.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def write_run_meta(meta: dict[str, Any]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    RUN_META.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def list_batches(limit: int = 15) -> list[dict[str, Any]]:
    if not RESULTS.is_dir():
        return []
    batches = sorted(
        [p for p in RESULTS.iterdir() if p.is_dir() and p.name.startswith("batch_")],
        key=lambda p: p.name,
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for b in batches[:limit]:
        meta: dict[str, Any] = {}
        mp = b / "batch_meta.json"
        if mp.is_file():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        created = meta.get("created")
        failed = meta.get("failed")
        if created is None and (b / "accounts.json").is_file():
            try:
                created = len(json.loads((b / "accounts.json").read_text(encoding="utf-8")))
            except Exception:
                created = 0
        if failed is None and (b / "failed.json").is_file():
            try:
                failed = len(json.loads((b / "failed.json").read_text(encoding="utf-8")))
            except Exception:
                failed = 0
        out.append(
            {
                "id": b.name,
                "path": str(b),
                "created": created if created is not None else 0,
                "failed": failed if failed is not None else 0,
                "elapsed_s": meta.get("elapsed_s"),
                "started_at": meta.get("started_at"),
                "finished_at": meta.get("finished_at"),
                "max_accounts": meta.get("max_accounts"),
                "mode": meta.get("mode") or "browser",
                "has_g2a_import": (b / "g2a_import.json").is_file(),
            }
        )
    return out


def latest_batch_summary() -> dict[str, Any] | None:
    batches = list_batches(1)
    return batches[0] if batches else None


def g2a_health() -> dict[str, Any]:
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    url = f"{G2A_URL}/healthz"
    try:
        req = Request(url, method="GET")
        with urlopen(req, timeout=3) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except Exception:
                data = {"raw": body[:200]}
            return {"ok": True, "status": resp.status, "body": data, "url": G2A_URL}
    except Exception as e:
        return {"ok": False, "error": str(e), "url": G2A_URL}


def g2a_account_count() -> dict[str, Any]:
    """Best-effort count of Build accounts (needs admin creds in .env)."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    user = _env("GROK2API_ADMIN_USER", "admin")
    pw = _env("GROK2API_ADMIN_PASS")
    if not pw:
        return {"ok": False, "error": "no admin pass"}
    try:
        req = Request(
            f"{G2A_URL}/api/admin/v1/auth/login",
            data=json.dumps({"username": user, "password": pw}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=8) as resp:
            login = json.loads(resp.read().decode())
        token = (
            (login.get("data") or {}).get("tokens") or {}
        ).get("accessToken") or (login.get("tokens") or {}).get("accessToken")
        if not token:
            return {"ok": False, "error": "login failed"}
        req = Request(
            f"{G2A_URL}/api/admin/v1/accounts",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []
        active = sum(
            1
            for it in items
            if isinstance(it, dict)
            and it.get("enabled")
            and str(it.get("authStatus") or "").lower() in ("active", "")
        )
        return {"ok": True, "total": len(items), "active": active}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def read_log_tail(path: Path, lines: int = 120) -> str:
    if not path.is_file():
        return ""
    try:
        # efficient-ish tail
        data = path.read_bytes()
        if len(data) > 512_000:
            data = data[-512_000:]
        text = data.decode("utf-8", errors="replace").replace("\r", "\n")
        parts = text.splitlines()
        return "\n".join(parts[-lines:])
    except Exception as e:
        return f"(log read error: {e})"


def progress_from_log(text: str) -> dict[str, Any]:
    """Parse last OK/FAIL/step from live log."""
    ok = len(re.findall(r"\] OK\s+", text))
    fail = len(re.findall(r"\] FAIL\s+", text))
    # last progress line
    last = ""
    for line in reversed(text.splitlines()):
        if re.search(r"\[\d+\]", line) or "DONE:" in line or "[g2a]" in line:
            last = line.strip()
            break
    m = re.search(r"Create\s*:\s*(\d+)\s*accounts", text)
    target = int(m.group(1)) if m else None
    return {"ok_lines": ok, "fail_lines": fail, "last_line": last, "target": target}


def start_farm(
    n: int,
    concurrent: int,
    yes: bool = True,
    mode: str = "browser",
) -> dict[str, Any]:
    """Start a farm job.

    mode:
      - browser  → farm.py / run.sh (Camoufox)
      - http     → farm_http.py / run_http.sh (curl_cffi + Turnstile solver)
    """
    with _lock:
        if farm_running():
            return {"ok": False, "error": "farm already running", "pid": farm_pid()}

        mode = (mode or "browser").strip().lower()
        if mode not in ("browser", "http"):
            return {"ok": False, "error": f"invalid mode: {mode} (use browser|http)"}

        # Match CLI caps: farm_http.py / farm.py allow up to 1000 accounts.
        # (Old panel hard-capped at 100 — that's why UI "1000" only ran 100.)
        n_cap = 1000
        n = max(1, min(n_cap, int(n)))
        # HTTP can sustain higher concurrency (no browser); still cap for safety
        conc_cap = 20 if mode == "http" else 10
        concurrent = max(1, min(conc_cap, int(concurrent)))
        RESULTS.mkdir(parents=True, exist_ok=True)

        # truncate live log for this run
        RUN_LOG.write_text("", encoding="utf-8")

        if mode == "http":
            run_sh = FARM_DIR / "run_http.sh"
            script = FARM_DIR / "farm_http.py"
            if not script.is_file() and not run_sh.is_file():
                return {
                    "ok": False,
                    "error": "HTTP farm not installed (missing farm_http.py / run_http.sh)",
                }
            if run_sh.is_file():
                cmd = ["bash", str(run_sh), "-n", str(n), "-c", str(concurrent)]
                if yes:
                    cmd.append("-y")
            else:
                cmd = [
                    sys.executable,
                    str(script),
                    "-n",
                    str(n),
                    "-c",
                    str(concurrent),
                ]
                if yes:
                    cmd.append("-y")
        else:
            run_sh = FARM_DIR / "run.sh"
            if run_sh.is_file():
                cmd = ["bash", str(run_sh), "--", "-n", str(n), "-c", str(concurrent)]
                if yes:
                    cmd.append("-y")
            else:
                cmd = [
                    sys.executable,
                    str(FARM_DIR / "farm.py"),
                    "-n",
                    str(n),
                    "-c",
                    str(concurrent),
                ]
                if yes:
                    cmd.append("-y")

        env = os.environ.copy()
        env["GROK_UI"] = "log"
        env["GROK_VERBOSE"] = env.get("GROK_VERBOSE") or "true"
        # Prefer non-root; if we are root, try runuser farm
        popen_cmd = cmd
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            # run as farm user if exists
            try:
                import pwd

                pwd.getpwnam("farm")
                popen_cmd = ["sudo", "-u", "farm", "--"] + cmd
            except KeyError:
                pass

        # open log
        logf = open(RUN_LOG, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                popen_cmd,
                cwd=str(FARM_DIR),
                env=env,
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:
            logf.close()
            return {"ok": False, "error": f"spawn failed: {e}"}

        RUN_PID.write_text(str(proc.pid) + "\n", encoding="utf-8")
        meta = {
            "pid": proc.pid,
            "started_at": _utc_now(),
            "n": n,
            "concurrent": concurrent,
            "mode": mode,
            "cmd": popen_cmd,
        }
        write_run_meta(meta)
        return {
            "ok": True,
            "pid": proc.pid,
            "n": n,
            "concurrent": concurrent,
            "mode": mode,
        }


def stop_farm() -> dict[str, Any]:
    with _lock:
        pid = farm_pid()
        if not pid:
            return {"ok": True, "stopped": False, "message": "not running"}
        killed = []
        # kill process group if possible
        try:
            os.killpg(pid, signal.SIGTERM)
            killed.append(f"pg:{pid}")
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(str(pid))
            except Exception as e:
                return {"ok": False, "error": str(e)}
        # also pkill farm workers (browser + HTTP) under farm user
        for pattern in ("python farm_http.py", "python farm.py"):
            try:
                subprocess.run(
                    ["pkill", "-f", pattern],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
        try:
            subprocess.run(
                ["pkill", "-f", "camoufox-bin"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        time.sleep(0.5)
        still = farm_running()
        if still:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
            for pattern in ("python farm_http.py", "python farm.py"):
                subprocess.run(
                    ["pkill", "-9", "-f", pattern],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        return {
            "ok": True,
            "stopped": True,
            "killed": killed,
            "still_running": farm_running(),
        }


def status_payload() -> dict[str, Any]:
    running = farm_running()
    log = read_log_tail(RUN_LOG, 200)
    prog = progress_from_log(log)
    return {
        "time": _utc_now(),
        "farm_dir": str(FARM_DIR),
        "running": running,
        "pid": farm_pid(),
        "run_meta": read_run_meta(),
        "progress": prog,
        "latest_batch": latest_batch_summary(),
        "batches": list_batches(10),
        "g2a": g2a_health(),
        "g2a_accounts": g2a_account_count(),
        "env": {
            "email_mode": _env("GROK_EMAIL_MODE"),
            "email_domain": _env("GROK_EMAIL_DOMAIN"),
            "headless": _env("GROK_HEADLESS"),
            "auto_import": _env("GROK2API_AUTO_IMPORT"),
            "max_default": _env("GROK_MAX_ACCOUNTS", "5"),
            "conc_default": _env("GROK_CONCURRENT", "1"),
            "solver_url": _env("SOLVER_URL"),
            "http_available": (FARM_DIR / "farm_http.py").is_file()
            or (FARM_DIR / "run_http.sh").is_file(),
        },
    }


# ── HTML UI ──────────────────────────────────────────────────────────────────
UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Grok Farm Panel</title>
<style>
  :root {
    --bg: #0b0f14;
    --panel: #121821;
    --panel2: #182131;
    --border: #243044;
    --text: #e7eef9;
    --muted: #8b9bb4;
    --accent: #5b9dff;
    --ok: #3ddc97;
    --bad: #ff6b6b;
    --warn: #f0c14b;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: var(--sans); background: radial-gradient(1200px 600px at 10% -10%, #152238, var(--bg));
    color: var(--text); min-height: 100vh;
  }
  header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 16px 22px; border-bottom: 1px solid var(--border);
    background: rgba(10,14,20,.75); backdrop-filter: blur(8px); position: sticky; top: 0; z-index: 5;
  }
  header h1 { font-size: 16px; margin: 0; letter-spacing: .3px; }
  header .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 18px; }
  .grid { display: grid; gap: 14px; grid-template-columns: 1.1fr .9fr; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  .card {
    background: linear-gradient(180deg, var(--panel), var(--panel2));
    border: 1px solid var(--border); border-radius: 14px; padding: 16px;
    box-shadow: 0 10px 30px rgba(0,0,0,.25);
  }
  .card h2 { margin: 0 0 12px; font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  .stat { flex: 1; min-width: 120px; background: rgba(255,255,255,.03); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
  .stat .k { font-size: 11px; color: var(--muted); }
  .stat .v { font-size: 18px; font-weight: 650; margin-top: 4px; font-variant-numeric: tabular-nums; }
  .pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px; font-size: 12px; border: 1px solid var(--border); }
  .pill.ok { color: var(--ok); border-color: rgba(61,220,151,.35); background: rgba(61,220,151,.08); }
  .pill.bad { color: var(--bad); border-color: rgba(255,107,107,.35); background: rgba(255,107,107,.08); }
  .pill.idle { color: var(--muted); }
  label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  input[type=number], input[type=text], input[type=password] {
    width: 100%; background: #0d131c; border: 1px solid var(--border); color: var(--text);
    border-radius: 10px; padding: 10px 12px; font-size: 14px; outline: none;
  }
  input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(91,157,255,.15); }
  button {
    border: 0; border-radius: 10px; padding: 10px 14px; font-weight: 600; cursor: pointer;
    background: var(--accent); color: #041018;
  }
  button.secondary { background: transparent; color: var(--text); border: 1px solid var(--border); }
  button.danger { background: var(--bad); color: #1a0505; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .field { margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
  pre#log {
    margin: 0; max-height: 360px; overflow: auto; background: #070b10; border: 1px solid var(--border);
    border-radius: 10px; padding: 12px; font-family: var(--mono); font-size: 11.5px; line-height: 1.45; color: #cfe0ff;
    white-space: pre-wrap; word-break: break-word;
  }
  .login-wrap { min-height: 100vh; display: grid; place-items: center; padding: 20px; }
  .login-card { width: min(380px, 100%); }
  .err { color: var(--bad); font-size: 13px; margin-top: 8px; min-height: 1.2em; }
  .muted { color: var(--muted); font-size: 12px; }
  a { color: var(--accent); }
  .hidden { display: none !important; }
</style>
</head>
<body>
<div id="loginView" class="login-wrap">
  <div class="card login-card">
    <h1 style="margin:0 0 4px;font-size:18px">Grok Farm Panel</h1>
    <p class="muted" style="margin:0 0 16px">Control account farming on this VPS</p>
    <div class="field">
      <label>Username</label>
      <input id="user" type="text" autocomplete="username" value="admin"/>
    </div>
    <div class="field">
      <label>Password</label>
      <input id="pass" type="password" autocomplete="current-password"/>
    </div>
    <button id="loginBtn" style="width:100%">Sign in</button>
    <div class="err" id="loginErr"></div>
  </div>
</div>

<div id="appView" class="hidden">
  <header>
    <div>
      <h1>Grok Farm Control</h1>
      <div class="sub" id="hdrSub">—</div>
    </div>
    <div class="row">
      <span id="runPill" class="pill idle">idle</span>
      <button class="secondary" id="refreshBtn">Refresh</button>
      <button class="secondary" id="logoutBtn">Logout</button>
    </div>
  </header>
  <div class="wrap">
    <div class="grid">
      <div class="card">
        <h2>Status</h2>
        <div class="row" style="margin-bottom:12px">
          <div class="stat"><div class="k">Farm</div><div class="v" id="stRunning">—</div></div>
          <div class="stat"><div class="k">Mode</div><div class="v" id="stMode">—</div></div>
          <div class="stat"><div class="k">PID</div><div class="v" id="stPid">—</div></div>
          <div class="stat"><div class="k">g2a</div><div class="v" id="stG2a">—</div></div>
          <div class="stat"><div class="k">Pool accounts</div><div class="v" id="stPool">—</div></div>
        </div>
        <div class="muted" id="stLast" style="margin-bottom:10px">—</div>
        <div class="muted" id="stBatch">Latest batch: —</div>
      </div>

      <div class="card">
        <h2>Start farm</h2>
        <div class="field">
          <label>Mode</label>
          <div class="row" style="gap:14px">
            <label class="muted" style="display:flex;gap:6px;align-items:center;margin:0;cursor:pointer">
              <input type="radio" name="farmMode" id="modeHttp" value="http" checked/>
              HTTP <span class="muted">(no browser · recommended)</span>
            </label>
            <label class="muted" style="display:flex;gap:6px;align-items:center;margin:0;cursor:pointer">
              <input type="radio" name="farmMode" id="modeBrowser" value="browser"/>
              Browser <span class="muted">(Camoufox)</span>
            </label>
          </div>
        </div>
        <div class="row">
          <div class="field" style="flex:1">
            <label>Accounts (-n)</label>
            <input id="nAcc" type="number" min="1" max="1000" value="1"/>
          </div>
          <div class="field" style="flex:1">
            <label>Concurrency (-c)</label>
            <input id="nConc" type="number" min="1" max="20" value="1"/>
          </div>
        </div>
        <div class="row">
          <button id="startBtn">Start</button>
          <button class="danger" id="stopBtn">Stop</button>
        </div>
        <div class="err" id="jobErr"></div>
        <p class="muted" style="margin:12px 0 0" id="startHint">
          HTTP mode uses <code>./run_http.sh -n N -c C -y</code> (curl_cffi + Turnstile solver).
          Browser mode uses <code>./run.sh -- -n N -c C -y</code> (Camoufox).
          Auto-import follows <code>GROK2API_AUTO_IMPORT</code> in <code>.env</code>.
        </p>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <div class="row" style="justify-content:space-between;margin-bottom:8px">
        <h2 style="margin:0">Live log</h2>
        <label class="muted" style="display:flex;gap:6px;align-items:center;margin:0">
          <input type="checkbox" id="autoScroll" checked/> auto-scroll
        </label>
      </div>
      <pre id="log">(no log yet)</pre>
    </div>

    <div class="card" style="margin-top:14px">
      <h2>Recent batches</h2>
      <table>
        <thead><tr><th>Batch</th><th>Mode</th><th>Created</th><th>Failed</th><th>Elapsed</th><th>g2a</th></tr></thead>
        <tbody id="batchBody"><tr><td colspan="6" class="muted">—</td></tr></tbody>
      </table>
    </div>

    <p class="muted" style="margin-top:16px">
      g2a dashboard:
      <a id="g2aLink" href="http://127.0.0.1:8000" target="_blank" rel="noreferrer">open</a>
    </p>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let timer = null;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!res.ok) {
    const err = new Error(data.error || data.message || res.statusText || 'request failed');
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

function showApp(on) {
  $('loginView').classList.toggle('hidden', on);
  $('appView').classList.toggle('hidden', !on);
}

function setPill(running) {
  const el = $('runPill');
  el.className = 'pill ' + (running ? 'ok' : 'idle');
  el.textContent = running ? 'running' : 'idle';
}

async function refresh() {
  try {
    const st = await api('/api/status');
    setPill(!!st.running);
    $('stRunning').textContent = st.running ? 'running' : 'idle';
    const activeMode = (st.run_meta && st.run_meta.mode) || (st.running ? '—' : 'idle');
    $('stMode').textContent = st.running ? activeMode : '—';
    $('stPid').textContent = st.pid || '—';
    $('stG2a').textContent = st.g2a && st.g2a.ok ? 'up' : 'down';
    $('stG2a').style.color = st.g2a && st.g2a.ok ? 'var(--ok)' : 'var(--bad)';
    if (st.g2a_accounts && st.g2a_accounts.ok) {
      $('stPool').textContent = `${st.g2a_accounts.active}/${st.g2a_accounts.total}`;
    } else {
      $('stPool').textContent = '—';
    }
    $('stLast').textContent = (st.progress && st.progress.last_line) || '—';
    const lb = st.latest_batch;
    $('stBatch').textContent = lb
      ? `Latest batch: ${lb.id} · mode=${lb.mode || '—'} · ok=${lb.created} fail=${lb.failed}`
      : 'Latest batch: —';
    $('hdrSub').textContent = st.farm_dir || '';
    if (st.env) {
      if (!$('nAcc').dataset.touched) $('nAcc').value = st.env.max_default || 1;
      if (!$('nConc').dataset.touched) $('nConc').value = st.env.conc_default || 1;
      // Disable HTTP mode if not installed
      if (st.env.http_available === false) {
        $('modeHttp').disabled = true;
        if ($('modeHttp').checked) {
          $('modeBrowser').checked = true;
        }
      }
    }
    // show active mode from running job
    const rm = st.run_meta || {};
    if (st.running && rm.mode) {
      if (rm.mode === 'http') $('modeHttp').checked = true;
      if (rm.mode === 'browser') $('modeBrowser').checked = true;
    }
    // batches
    const tb = $('batchBody');
    tb.innerHTML = '';
    (st.batches || []).forEach(b => {
      const tr = document.createElement('tr');
      const mode = b.mode || 'browser';
      tr.innerHTML = `<td><code>${b.id}</code></td><td>${mode}</td><td>${b.created}</td><td>${b.failed}</td><td>${b.elapsed_s ?? '—'}</td><td>${b.has_g2a_import ? 'yes' : '—'}</td>`;
      tb.appendChild(tr);
    });
    if (!(st.batches || []).length) {
      tb.innerHTML = '<tr><td colspan="6" class="muted">No batches yet</td></tr>';
    }
    // g2a link uses host
    const host = location.hostname;
    $('g2aLink').href = `http://${host}:8000`;
    $('g2aLink').textContent = `http://${host}:8000`;
    $('startBtn').disabled = !!st.running;
    $('stopBtn').disabled = !st.running;
    $('modeHttp').disabled = !!st.running || (st.env && st.env.http_available === false);
    $('modeBrowser').disabled = !!st.running;
  } catch (e) {
    if (e.status === 401) {
      showApp(false);
      stopPoll();
    }
  }
  try {
    const lg = await api('/api/log?lines=150');
    const pre = $('log');
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40;
    pre.textContent = lg.text || '(empty)';
    if ($('autoScroll').checked && atBottom) pre.scrollTop = pre.scrollHeight;
  } catch {}
}

function startPoll() {
  stopPoll();
  refresh();
  timer = setInterval(refresh, 2500);
}
function stopPoll() {
  if (timer) clearInterval(timer);
  timer = null;
}

$('loginBtn').onclick = async () => {
  $('loginErr').textContent = '';
  try {
    await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({ username: $('user').value, password: $('pass').value }),
    });
    showApp(true);
    startPoll();
  } catch (e) {
    $('loginErr').textContent = e.message || 'login failed';
  }
};
$('pass').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('loginBtn').click(); });
$('logoutBtn').onclick = async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch {}
  showApp(false);
  stopPoll();
};
$('refreshBtn').onclick = () => refresh();
$('nAcc').addEventListener('input', () => { $('nAcc').dataset.touched = '1'; });
$('nConc').addEventListener('input', () => { $('nConc').dataset.touched = '1'; });
function selectedMode() {
  return ($('modeHttp').checked ? 'http' : 'browser');
}
$('startBtn').onclick = async () => {
  $('jobErr').textContent = '';
  try {
    await api('/api/start', {
      method: 'POST',
      body: JSON.stringify({
        n: Number($('nAcc').value || 1),
        concurrent: Number($('nConc').value || 1),
        mode: selectedMode(),
      }),
    });
    await refresh();
  } catch (e) {
    $('jobErr').textContent = e.message || 'start failed';
  }
};
$('stopBtn').onclick = async () => {
  $('jobErr').textContent = '';
  try {
    await api('/api/stop', { method: 'POST', body: '{}' });
    await refresh();
  } catch (e) {
    $('jobErr').textContent = e.message || 'stop failed';
  }
};

// session probe
(async () => {
  try {
    await api('/api/status');
    showApp(true);
    startPoll();
  } catch {
    showApp(false);
  }
})();
</script>
</body>
</html>
"""


# ── HTTP handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "GrokFarmPanel/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _cors(self) -> None:
        # same-origin primarily; keep simple
        self.send_header("Cache-Control", "no-store")

    def _json(self, code: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, code: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1_000_000))
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _token_from_request(self) -> str | None:
        # cookie
        cookie_header = self.headers.get("Cookie") or ""
        if cookie_header:
            c = SimpleCookie()
            try:
                c.load(cookie_header)
                if "farm_panel_token" in c:
                    return c["farm_panel_token"].value
            except Exception:
                pass
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def _require_auth(self) -> str | None:
        user = verify_token(self._token_from_request())
        if not user:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return None
        return user

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._html(200, UI_HTML)
            return
        if path == "/api/health":
            self._json(200, {"ok": True, "time": _utc_now()})
            return
        if path == "/api/status":
            if not self._require_auth():
                return
            self._json(200, status_payload())
            return
        if path == "/api/log":
            if not self._require_auth():
                return
            qs = parse_qs(parsed.query or "")
            try:
                lines = max(20, min(500, int((qs.get("lines") or ["120"])[0])))
            except ValueError:
                lines = 120
            self._json(
                200,
                {
                    "path": str(RUN_LOG),
                    "text": read_log_tail(RUN_LOG, lines),
                    "running": farm_running(),
                },
            )
            return
        if path == "/api/batches":
            if not self._require_auth():
                return
            self._json(200, {"batches": list_batches(30)})
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json()

        if path == "/api/login":
            if not PANEL_PASSWORD:
                self._json(
                    500,
                    {
                        "error": "FARM_PANEL_PASSWORD (or GROK2API_ADMIN_PASS) not configured"
                    },
                )
                return
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "")
            if username != PANEL_USER or not hmac.compare_digest(password, PANEL_PASSWORD):
                time.sleep(0.4)
                self._json(401, {"error": "invalid credentials"})
                return
            token = issue_token(username)
            raw = json.dumps({"ok": True, "user": username}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            cookie = (
                f"farm_panel_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={TOKEN_TTL}"
            )
            self.send_header("Set-Cookie", cookie)
            self._cors()
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/logout":
            raw = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header(
                "Set-Cookie",
                "farm_panel_token=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0",
            )
            self._cors()
            self.end_headers()
            self.wfile.write(raw)
            return

        if path == "/api/start":
            if not self._require_auth():
                return
            n = body.get("n", body.get("count", 1))
            concurrent = body.get("concurrent", body.get("c", 1))
            mode = str(body.get("mode") or "browser").strip().lower()
            try:
                result = start_farm(int(n), int(concurrent), yes=True, mode=mode)
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
                return
            code = 200 if result.get("ok") else 409
            self._json(code, result)
            return

        if path == "/api/stop":
            if not self._require_auth():
                return
            try:
                result = stop_farm()
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
                return
            self._json(200, result)
            return

        self._json(404, {"error": "not found"})


def main() -> int:
    if not PANEL_PASSWORD:
        print(
            "ERROR: set FARM_PANEL_PASSWORD or GROK2API_ADMIN_PASS in env/.env",
            flush=True,
        )
        return 1
    RESULTS.mkdir(parents=True, exist_ok=True)
    print(f"Grok Farm Panel", flush=True)
    print(f"  dir  : {FARM_DIR}", flush=True)
    print(f"  bind : http://{HOST}:{PORT}", flush=True)
    print(f"  user : {PANEL_USER}", flush=True)
    print(f"  g2a  : {G2A_URL}", flush=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
