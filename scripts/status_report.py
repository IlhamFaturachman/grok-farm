#!/usr/bin/env python3
"""Periodic Telegram status: farm counts, pool accounts, token health.

Usage:
  python scripts/status_report.py           # send Telegram report
  python scripts/status_report.py --print   # print only
  python scripts/status_report.py --force   # ignore NOTIFY_ENABLED
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def load_env_file() -> None:
    p = _ROOT / ".env"
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

from notify import dispatch, public_ip  # noqa: E402


def _http_json(method: str, url: str, *, token: str | None = None, body: dict | None = None) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=25) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else {}


def admin_token(base: str, user: str, password: str) -> str:
    data = _http_json(
        "POST",
        f"{base}/api/admin/v1/auth/login",
        body={"username": user, "password": password},
    )
    return str(data["data"]["tokens"]["accessToken"])


def list_accounts(base: str, token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while page <= 50:
        body = _http_json(
            "GET",
            f"{base}/api/admin/v1/accounts?page={page}&pageSize=100",
            token=token,
        )
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        chunk: list = []
        if isinstance(data, dict):
            chunk = data.get("items") or data.get("accounts") or []
            total = data.get("total")
        elif isinstance(data, list):
            chunk = data
            total = None
        else:
            break
        items.extend([x for x in chunk if isinstance(x, dict)])
        if total is not None and len(items) >= int(total):
            break
        if len(chunk) < 100:
            break
        page += 1
    return items


def disk_stats() -> dict[str, Any]:
    results = _ROOT / "results"
    batches = sorted(results.glob("batch_*"))
    ok = 0
    fail = 0
    emails: set[str] = set()
    with_tokens = 0
    for b in batches:
        acc = b / "accounts.json"
        fj = b / "failed.json"
        if acc.is_file():
            try:
                data = json.loads(acc.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    ok += len(data)
                    for row in data:
                        e = (row.get("email") or "").lower()
                        if e:
                            emails.add(e)
                        tok = row.get("tokens") or {}
                        if tok.get("access_token") and tok.get("refresh_token"):
                            with_tokens += 1
            except Exception:
                pass
        if fj.is_file():
            try:
                data = json.loads(fj.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    fail += len(data)
            except Exception:
                pass
    used = results / "used_emails.txt"
    used_n = 0
    if used.is_file():
        used_n = sum(1 for ln in used.read_text(encoding="utf-8").splitlines() if ln.strip())
    return {
        "batches": len(batches),
        "accounts_ok": ok,
        "with_tokens": with_tokens,
        "unique_emails": len(emails),
        "failed_rows": fail,
        "used_index": used_n,
    }


def pool_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    by_auth: dict[str, int] = {}
    active = 0
    disabled = 0
    reauth = 0
    refreshable = 0
    expiring_1h = 0
    expiring_6h = 0
    expired = 0
    quota_remaining_sum = 0
    quota_known = 0
    samples_active: list[str] = []
    samples_bad: list[str] = []

    for it in items:
        auth = str(it.get("authStatus") or it.get("status") or "unknown").lower()
        by_auth[auth] = by_auth.get(auth, 0) + 1
        enabled = it.get("enabled", True) is not False
        email = str(it.get("email") or it.get("name") or it.get("id") or "?")

        if not enabled:
            disabled += 1
            if len(samples_bad) < 3:
                samples_bad.append(f"{email} (disabled)")
            continue

        if auth in ("reauth", "reauth_required", "invalid", "expired", "dead"):
            reauth += 1
            if len(samples_bad) < 3:
                samples_bad.append(f"{email} ({auth})")
        elif auth in ("active", "ok", "ready", "healthy", "unknown") or enabled:
            if auth == "active" or (enabled and auth not in ("reauth", "invalid", "expired")):
                active += 1
                if len(samples_active) < 3:
                    samples_active.append(email)

        if it.get("refreshable"):
            refreshable += 1

        exp_raw = it.get("expiresAt") or it.get("expires_at")
        if exp_raw:
            try:
                exp = datetime.fromisoformat(str(exp_raw).replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                delta = (exp - now).total_seconds()
                if delta <= 0:
                    expired += 1
                elif delta <= 3600:
                    expiring_1h += 1
                elif delta <= 6 * 3600:
                    expiring_6h += 1
            except Exception:
                pass

        q = it.get("quota") if isinstance(it.get("quota"), dict) else {}
        rem = q.get("remaining")
        if rem is not None:
            try:
                quota_remaining_sum += int(rem)
                quota_known += 1
            except Exception:
                pass

    return {
        "total": len(items),
        "active": active,
        "disabled": disabled,
        "reauth": reauth,
        "refreshable": refreshable,
        "expiring_1h": expiring_1h,
        "expiring_6h": expiring_6h,
        "expired_access": expired,
        "by_auth": by_auth,
        "quota_accounts": quota_known,
        "quota_remaining_sum": quota_remaining_sum,
        "samples_active": samples_active,
        "samples_bad": samples_bad,
    }


def service_active(name: str) -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (r.stdout or r.stderr or "unknown").strip()
    except Exception:
        return "unknown"


def farm_loop_state() -> dict[str, Any]:
    p = _ROOT / "results" / "farm_loop_state.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_report() -> str:
    disk = disk_stats()
    base = (os.environ.get("GROK2API_URL") or "http://127.0.0.1:8000").rstrip("/")
    user = os.environ.get("GROK2API_ADMIN_USER") or "admin"
    password = os.environ.get("GROK2API_ADMIN_PASS") or ""

    pool: dict[str, Any] = {"total": 0, "active": 0, "error": None}
    if password:
        try:
            token = admin_token(base, user, password)
            items = list_accounts(base, token)
            pool = pool_stats(items)
        except Exception as e:
            pool = {"total": 0, "active": 0, "error": str(e)[:120]}
    else:
        pool["error"] = "no admin pass"

    loop = farm_loop_state()
    ip = public_ip()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "📊 Grok Farm — status report",
        f"Time: {now}",
        f"IP: {ip}",
        "",
        "── Farm (disk) ──",
        f"Batches        : {disk['batches']}",
        f"Accounts OK    : {disk['accounts_ok']}",
        f"With tokens    : {disk['with_tokens']}",
        f"Unique emails  : {disk['unique_emails']}",
        f"Failed rows    : {disk['failed_rows']}",
        "",
        "── Token pool (grok2api) ──",
    ]

    if pool.get("error"):
        lines.append(f"Error: {pool['error']}")
    else:
        lines.extend(
            [
                f"Total accounts : {pool['total']}",
                f"✅ Active       : {pool['active']}",
                f"🔄 Refreshable  : {pool['refreshable']}",
                f"⚠️  Reauth       : {pool['reauth']}",
                f"⛔ Disabled     : {pool['disabled']}",
                f"⏳ Access expiring <1h  : {pool['expiring_1h']}",
                f"⏳ Access expiring <6h  : {pool['expiring_6h']}",
                f"💀 Access expired       : {pool['expired_access']}",
                f"Auth status    : {pool.get('by_auth') or {}}",
            ]
        )
        if pool.get("quota_accounts"):
            lines.append(
                f"Quota remaining (sum est.): {pool['quota_remaining_sum']:,} "
                f"across {pool['quota_accounts']} accounts"
            )
        if pool.get("samples_active"):
            lines.append("Sample active:")
            for e in pool["samples_active"]:
                lines.append(f"  • {e}")
        if pool.get("samples_bad"):
            lines.append("Sample problem:")
            for e in pool["samples_bad"]:
                lines.append(f"  • {e}")

    lines.extend(
        [
            "",
            "── Services ──",
            f"farm-loop   : {service_active('farm-loop')}",
            f"g2a-pool    : {service_active('g2a-pool')}",
            f"cloudflared : {service_active('cloudflared')}",
        ]
    )
    if loop:
        lines.append(
            f"loop last   : created={loop.get('last_created')} failed={loop.get('last_failed')} "
            f"status={loop.get('status')} @ {loop.get('updated_at', '?')}"
        )

    lines.extend(
        [
            "",
            "API: https://api.liamnevalackin.my.id/v1",
            "Note: access_token ~6h, auto-refresh by grok2api if refresh_token OK.",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Grok Farm 6h status report → Telegram")
    p.add_argument("--print", action="store_true", help="print only, do not send")
    p.add_argument("--force", action="store_true", help="send even if NOTIFY_ENABLED=false")
    args = p.parse_args(argv)

    report = build_report()
    print(report)

    if args.print:
        return 0

    if not args.force and (os.environ.get("NOTIFY_ENABLED") or "").lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        print("NOTIFY_ENABLED=false — use --force to send")
        return 1

    sent = dispatch(report, subject="Grok Farm status report")
    print(f"sent={sent}")
    return 0 if any(sent.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
