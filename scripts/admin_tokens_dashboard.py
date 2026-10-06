#!/usr/bin/env python3
"""
Liam Farm — Admin Tokens Dashboard (clean, self-contained).

  http://0.0.0.0:8789/

Shows:
  - Total pool quota remaining (est.)
  - Active / refreshable / reauth / disabled accounts
  - Shop prepaid balances (all users)
  - Hermes / admin key note
  - Services + last farm loop
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# load .env like status_report
try:
    from status_report import (  # type: ignore
        load_env_file,
        disk_stats,
        pool_stats,
        admin_token,
        list_accounts,
        service_active,
        farm_loop_state,
        public_ip,
    )
    load_env_file()
except Exception:
    def load_env_file() -> None:
        p = ROOT / ".env"
        if not p.is_file():
            return
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k:
                os.environ[k] = v

    load_env_file()
    from status_report import (  # type: ignore
        disk_stats,
        pool_stats,
        admin_token,
        list_accounts,
        service_active,
        farm_loop_state,
    )
    try:
        from notify import public_ip  # type: ignore
    except Exception:
        def public_ip() -> str:
            return "unknown"

PORT = int(os.environ.get("FARM_TOKENS_DASH_PORT", "8789"))
SHOP_DB = Path(os.environ.get("SHOP_DB", str(ROOT / "shop" / "backend" / "data" / "shop.db")))
# Optional: show which key Hermes uses (prefix only)
HERMES_KEY_HINT = os.environ.get("HERMES_G2A_KEY_PREFIX", "g2a_f4ce4ab1d866")

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Liam Farm · Tokens Admin</title>
<style>
  :root {
    --bg:#07090f; --card:#10141d; --card2:#161b27; --border:#273044;
    --text:#eef2ff; --muted:#93a0b8; --dim:#64708a;
    --green:#22c55e; --red:#ef4444; --yellow:#eab308; --blue:#60a5fa; --purple:#a78bfa; --cyan:#22d3ee;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.45;padding:18px}
  .wrap{max-width:1200px;margin:0 auto}
  header{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;align-items:flex-start;margin-bottom:18px}
  h1{font-size:1.4rem;font-weight:900;letter-spacing:.02em}
  .sub{color:var(--muted);font-size:.9rem;margin-top:4px}
  .pill{display:inline-flex;align-items:center;gap:8px;padding:7px 12px;border-radius:999px;background:var(--card);border:1px solid var(--border);font-size:.8rem;color:var(--muted)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green)}
  .dot.stale{background:var(--red);box-shadow:0 0 8px var(--red)}
  .hero{background:linear-gradient(135deg,#12182a,#0d1220 55%,#14102a);border:1px solid var(--border);border-radius:18px;padding:20px;margin-bottom:14px}
  .hero .label{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:8px}
  .hero .big{font-size:2.2rem;font-weight:900;color:var(--cyan);letter-spacing:-.02em}
  .hero .hint{color:var(--muted);font-size:.85rem;margin-top:8px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
  .kpi{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px}
  .kpi .l{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:6px}
  .kpi .v{font-size:1.25rem;font-weight:800}
  .kpi .s{font-size:.75rem;color:var(--dim);margin-top:4px}
  .v.g{color:var(--green)} .v.r{color:var(--red)} .v.y{color:var(--yellow)} .v.b{color:var(--blue)} .v.c{color:var(--cyan)}
  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px;margin-bottom:14px}
  .card h2{font-size:.95rem;font-weight:700;margin-bottom:10px;display:flex;justify-content:space-between;gap:8px}
  .card h2 span{font-weight:500;color:var(--muted);font-size:.8rem}
  table{width:100%;border-collapse:collapse;font-size:.84rem}
  th,td{padding:9px 8px;text-align:left;border-bottom:1px solid var(--border);vertical-align:top}
  th{color:var(--muted);font-weight:600;font-size:.72rem;text-transform:uppercase;letter-spacing:.04em}
  tr:hover td{background:rgba(255,255,255,.02)}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.8rem}
  .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.72rem;font-weight:700}
  .badge.ok{background:rgba(34,197,94,.12);color:var(--green)}
  .badge.bad{background:rgba(239,68,68,.12);color:var(--red)}
  .badge.warn{background:rgba(234,179,8,.12);color:var(--yellow)}
  .badge.info{background:rgba(96,165,250,.15);color:var(--blue)}
  .two{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}
  @media(max-width:900px){.two{grid-template-columns:1fr}}
  .foot{color:var(--dim);font-size:.75rem;margin-top:10px}
  .empty{color:var(--dim);padding:12px 0}
  .note{background:var(--card2);border:1px solid var(--border);border-radius:12px;padding:12px;font-size:.85rem;color:var(--muted)}
  .note b{color:var(--text)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Liam Farm · Tokens Admin</h1>
      <div class="sub">Pool quota + shop balances · auto-refresh 15s</div>
    </div>
    <div class="pill"><span class="dot" id="dot"></span><span id="live">loading…</span></div>
  </header>

  <div class="hero">
    <div class="label">Total pool quota remaining (est.)</div>
    <div class="big" id="totalQuota">—</div>
    <div class="hint" id="totalHint">Sum of remaining quota across active pool accounts</div>
  </div>

  <div class="grid" id="kpis"></div>

  <div class="two">
    <div class="card">
      <h2>Shop balances <span id="shopTotal"></span></h2>
      <div style="overflow:auto">
        <table>
          <thead><tr><th>User</th><th>Email</th><th>Balance tokens</th><th>Updated</th></tr></thead>
          <tbody id="shopBody"><tr><td colspan="4" class="empty">loading…</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <h2>Your / Hermes key</h2>
      <div class="note" id="hermesNote">—</div>
      <div style="height:12px"></div>
      <h2>Services</h2>
      <table>
        <thead><tr><th>Service</th><th>Status</th></tr></thead>
        <tbody id="svcBody"></tbody>
      </table>
      <div class="foot" id="loopNote"></div>
    </div>
  </div>

  <div class="card">
    <h2>Pool health <span id="poolMeta"></span></h2>
    <div class="grid" id="poolGrid"></div>
    <div class="two" style="margin-top:8px">
      <div>
        <div class="sub" style="margin-bottom:6px">Sample active</div>
        <div class="mono" id="sampleActive" style="color:var(--muted)"></div>
      </div>
      <div>
        <div class="sub" style="margin-bottom:6px">Sample problem</div>
        <div class="mono" id="sampleBad" style="color:var(--muted)"></div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Disk farm (batches)</h2>
    <div class="grid" id="diskGrid"></div>
  </div>

  <div class="foot" id="foot">—</div>
</div>
<script>
function fmt(n){
  if(n==null||n===''||Number.isNaN(Number(n))) return '—';
  const x=Number(n);
  if(Math.abs(x)>=1e9) return (x/1e9).toFixed(2)+'B';
  if(Math.abs(x)>=1e6) return (x/1e6).toFixed(2)+'M';
  if(Math.abs(x)>=1e3) return (x/1e3).toFixed(1)+'K';
  return x.toLocaleString();
}
function fmtFull(n){
  if(n==null||n===''||Number.isNaN(Number(n))) return '—';
  return Number(n).toLocaleString();
}
function kpi(label, value, sub, cls){
  return `<div class="kpi"><div class="l">${label}</div><div class="v ${cls||''}">${value}</div><div class="s">${sub||''}</div></div>`;
}
function badge(ok){ return ok ? '<span class="badge ok">active</span>' : '<span class="badge bad">down</span>'; }

async function refresh(){
  const dot=document.getElementById('dot');
  try{
    const r=await fetch('/api/status?t='+Date.now(),{cache:'no-store'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    dot.classList.remove('stale');
    document.getElementById('live').textContent = 'live · '+new Date(d.ts*1000).toLocaleTimeString();

    const pool=d.pool||{};
    const disk=d.disk||{};
    const shop=d.shop||{};
    const svc=d.services||{};

    document.getElementById('totalQuota').textContent = fmtFull(pool.quota_remaining_sum);
    document.getElementById('totalHint').textContent =
      `Across ${fmt(pool.quota_accounts||pool.active)} accounts with known quota · active ${fmt(pool.active)} / total ${fmt(pool.total)}`;

    document.getElementById('kpis').innerHTML = [
      kpi('Active accounts', fmt(pool.active), 'pool ready', 'g'),
      kpi('Refreshable', fmt(pool.refreshable), 'can renew access', 'b'),
      kpi('Reauth needed', fmt(pool.reauth), 'problem', pool.reauth>0?'y':'g'),
      kpi('Disabled', fmt(pool.disabled), 'off', pool.disabled>0?'r':''),
      kpi('Access <1h', fmt(pool.expiring_1h), 'expiring soon', 'y'),
      kpi('Access <6h', fmt(pool.expiring_6h), 'will refresh', ''),
      kpi('Shop total', fmt(shop.total_balance), shop.users_n+' users', 'c'),
      kpi('Batches', fmt(disk.batches), 'disk farm', ''),
    ].join('');

    // shop table
    const rows=shop.balances||[];
    document.getElementById('shopTotal').textContent = 'sum '+fmtFull(shop.total_balance);
    document.getElementById('shopBody').innerHTML = rows.length ? rows.map(u=>`
      <tr>
        <td><b>${u.name||'—'}</b></td>
        <td class="mono muted">${u.email||''}</td>
        <td class="mono"><b>${fmtFull(u.balance_tokens)}</b></td>
        <td class="dim">${u.updated_at||'—'}</td>
      </tr>`).join('') : '<tr><td colspan="4" class="empty">no shop balances</td></tr>';

    // hermes
    const h=d.hermes||{};
    document.getElementById('hermesNote').innerHTML =
      `<b>Key prefix:</b> <span class="mono">${h.key_prefix||'—'}</span><br/>
       <b>Type:</b> ${h.kind||'—'}<br/>
       <b>Balance debit:</b> ${h.debit||'—'}<br/>
       <span class="dim">${h.note||''}</span>`;

    // services
    document.getElementById('svcBody').innerHTML = Object.keys(svc).map(k=>{
      const v=String(svc[k]||'');
      const ok=v==='active'||v==='running';
      return `<tr><td>${k}</td><td>${ok?badge(true):('<span class="badge warn">'+v+'</span>')}</td></tr>`;
    }).join('') || '<tr><td colspan="2" class="empty">—</td></tr>';
    const loop=d.farm_loop||{};
    document.getElementById('loopNote').textContent =
      loop.last ? `Loop last: created=${loop.last.created??'—'} failed=${loop.last.failed??'—'} @ ${loop.last.at||loop.last.ts||'—'}` : '';

    document.getElementById('poolMeta').textContent = 'total '+fmt(pool.total);
    document.getElementById('poolGrid').innerHTML = [
      kpi('Total pool', fmt(pool.total), 'accounts'),
      kpi('Active', fmt(pool.active), '', 'g'),
      kpi('Refreshable', fmt(pool.refreshable), '', 'b'),
      kpi('Expired access', fmt(pool.expired_access), '', pool.expired_access>0?'r':''),
      kpi('Quota accounts', fmt(pool.quota_accounts), 'with remaining field'),
      kpi('Auth map', Object.entries(pool.by_auth||{}).map(([k,v])=>k+':'+v).join(' · ')||'—', 'raw statuses'),
    ].join('');
    document.getElementById('sampleActive').textContent = (pool.samples_active||[]).join('\n')||'—';
    document.getElementById('sampleBad').textContent = (pool.samples_bad||[]).join('\n')||'—';

    document.getElementById('diskGrid').innerHTML = [
      kpi('Batches', fmt(disk.batches)),
      kpi('Accounts OK', fmt(disk.accounts_ok)),
      kpi('With tokens', fmt(disk.with_tokens), '', 'g'),
      kpi('Unique emails', fmt(disk.unique_emails)),
      kpi('Failed rows', fmt(disk.failed_rows), '', disk.failed_rows>0?'y':''),
    ].join('');

    document.getElementById('foot').textContent =
      `IP ${d.ip||'—'} · API ${d.api_public||'—'} · updated ${new Date(d.ts*1000).toISOString()}`;
  }catch(e){
    dot.classList.add('stale');
    document.getElementById('live').textContent='error: '+e.message;
  }
}
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
"""


def _shop_balances() -> dict[str, Any]:
    if not SHOP_DB.is_file():
        return {"total_balance": 0, "users_n": 0, "balances": [], "error": f"missing {SHOP_DB}"}
    try:
        con = sqlite3.connect(str(SHOP_DB))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT u.id as user_id, u.name, u.email, b.balance_tokens, b.updated_at
            FROM user_balances b
            JOIN users u ON u.id = b.user_id
            ORDER BY b.balance_tokens DESC
            """
        ).fetchall()
        balances = [dict(r) for r in rows]
        total = sum(int(r["balance_tokens"] or 0) for r in balances)
        # usage total debit if table exists
        usage_sum = 0
        try:
            usage_sum = int(
                con.execute("SELECT COALESCE(SUM(tokens),0) FROM usage_events").fetchone()[0] or 0
            )
        except Exception:
            pass
        con.close()
        return {
            "total_balance": total,
            "users_n": len(balances),
            "balances": balances,
            "usage_events_sum": usage_sum,
        }
    except Exception as e:
        return {"total_balance": 0, "users_n": 0, "balances": [], "error": str(e)}


def build_status() -> dict[str, Any]:
    disk = disk_stats()
    base = (os.environ.get("GROK2API_URL") or "http://127.0.0.1:8000").rstrip("/")
    user = os.environ.get("GROK2API_ADMIN_USER") or "admin"
    password = os.environ.get("GROK2API_ADMIN_PASS") or ""
    pool: dict[str, Any] = {
        "total": 0,
        "active": 0,
        "disabled": 0,
        "reauth": 0,
        "refreshable": 0,
        "expiring_1h": 0,
        "expiring_6h": 0,
        "expired_access": 0,
        "by_auth": {},
        "quota_accounts": 0,
        "quota_remaining_sum": 0,
        "samples_active": [],
        "samples_bad": [],
        "error": None,
    }
    try:
        if not password:
            raise RuntimeError("GROK2API_ADMIN_PASS empty")
        token = admin_token(base, user, password)
        items = list_accounts(base, token)
        pool = pool_stats(items)
        pool["error"] = None
    except Exception as e:
        pool["error"] = str(e)

    shop = _shop_balances()
    services = {
        "farm-loop": service_active("farm-loop"),
        "g2a-pool": service_active("g2a-pool"),
        "cloudflared": service_active("cloudflared"),
    }
    loop = farm_loop_state()
    last = {}
    if isinstance(loop, dict):
        last = {
            "created": loop.get("created") or loop.get("last_created"),
            "failed": loop.get("failed") or loop.get("last_failed"),
            "at": loop.get("last_run") or loop.get("updated_at") or loop.get("ts"),
            "status": loop.get("status"),
        }

    try:
        ip = public_ip()
    except Exception:
        ip = "unknown"

    hermes = {
        "key_prefix": HERMES_KEY_HINT + "…",
        "kind": "not_shop_key (farm admin / Hermes g2a)",
        "debit": "does NOT debit shop user_balances",
        "note": "Unlimited relative to shop prepaid layer — uses pool quota. Shop users have separate prepaid balances below.",
    }

    return {
        "ts": time.time(),
        "ip": ip,
        "api_public": os.environ.get("PUBLIC_API_URL") or "https://api.liamnevalackin.my.id/v1",
        "disk": disk,
        "pool": pool,
        "shop": shop,
        "services": services,
        "farm_loop": {"last": last, "raw_keys": list(loop.keys()) if isinstance(loop, dict) else []},
        "hermes": hermes,
        "layers": {
            "pool_quota_remaining_est": pool.get("quota_remaining_sum") or 0,
            "shop_prepaid_remaining": shop.get("total_balance") or 0,
            "hermes_key": "unlimited_vs_shop (pool-backed)",
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/status":
            try:
                payload = build_status()
                return self._send(200, json.dumps(payload, default=str).encode())
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}).encode())
        return self._send(404, b'{"error":"not found"}')


def main() -> None:
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Liam Farm tokens admin dashboard on http://0.0.0.0:{PORT}/", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
