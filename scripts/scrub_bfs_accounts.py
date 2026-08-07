#!/usr/bin/env python3
"""
Scrub xAI OAuth accounts that carry bot flags in the access JWT.

Flag signals (any → quarantine):
  - claim "bfs": 1            (current)
  - claim "bot_flag_source": 1 (legacy)

Sources:
  1) live grok2api pool via admin API export + PATCH enabled=false
  2) optional local farm batch accounts.json scan (report only)

Usage:
  # dry-run report (default)
  python scripts/scrub_bfs_accounts.py

  # disable flagged accounts in grok2api
  python scripts/scrub_bfs_accounts.py --apply

  # also scan recent farm batches
  python scripts/scrub_bfs_accounts.py --batches 30

Env:
  GROK2API_URL, GROK2API_ADMIN_USER, GROK2API_ADMIN_PASS
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_path = _ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _http_json(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    data = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                return {}
            return json.loads(raw)
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {err_body[:500]}") from e
    except URLError as e:
        raise RuntimeError(f"request failed {url}: {e}") from e


def admin_login(base_url: str, username: str, password: str) -> str:
    base = base_url.rstrip("/")
    data = _http_json(
        "POST",
        f"{base}/api/admin/v1/auth/login",
        body={"username": username, "password": password},
        timeout=30.0,
    )
    root = data.get("data") if isinstance(data.get("data"), dict) else data
    tokens = root.get("tokens") if isinstance(root.get("tokens"), dict) else root
    access = (
        (tokens or {}).get("accessToken")
        or (tokens or {}).get("access_token")
        or (root or {}).get("accessToken")
        or data.get("accessToken")
    )
    if not access:
        raise RuntimeError(f"login missing accessToken: {list(data.keys())}")
    return str(access)


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    if not token or token.count(".") < 2:
        return None
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
    except Exception:
        return None


def flag_info(claims: dict[str, Any] | None) -> tuple[bool, str]:
    if not claims:
        return False, "no_claims"
    if claims.get("bfs") == 1 or claims.get("bfs") is True:
        return True, "bfs=1"
    if claims.get("bot_flag_source") == 1 or claims.get("bot_flag_source") is True:
        return True, "bot_flag_source=1"
    return False, "clean"


def list_accounts(base: str, token: str, *, page_size: int = 100) -> list[dict[str, Any]]:
    """Paginate admin account list (has numeric id + email)."""
    out: list[dict[str, Any]] = []
    page = 1
    while page <= 200:
        data = _http_json(
            "GET",
            f"{base}/api/admin/v1/accounts?page={page}&pageSize={page_size}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=60.0,
        )
        root = data.get("data") if isinstance(data.get("data"), dict) else data
        items = root.get("items") if isinstance(root, dict) else None
        if not isinstance(items, list) or not items:
            break
        out.extend(items)
        total = 0
        try:
            total = int(root.get("total") or 0) if isinstance(root, dict) else 0
        except (TypeError, ValueError):
            total = 0
        if total and len(out) >= total:
            break
        if len(items) < page_size:
            break
        page += 1
    return out


def export_accounts(base: str, token: str) -> list[dict[str, Any]]:
    data = _http_json(
        "GET",
        f"{base}/api/admin/v1/accounts/export",
        headers={"Authorization": f"Bearer {token}"},
        timeout=120.0,
    )
    accs = data.get("accounts")
    if isinstance(accs, list):
        return accs
    root = data.get("data")
    if isinstance(root, dict) and isinstance(root.get("accounts"), list):
        return root["accounts"]
    return []


def set_enabled(base: str, token: str, account_id: str, enabled: bool) -> dict[str, Any]:
    return _http_json(
        "PATCH",
        f"{base}/api/admin/v1/accounts/{account_id}",
        body={"enabled": bool(enabled)},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


def scan_batches(results_root: Path, limit: int) -> dict[str, Any]:
    files = sorted(
        results_root.glob("batch_*/accounts.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: max(0, limit)]
    c = Counter()
    flagged_emails: list[str] = []
    for f in files:
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            c["batch_read_err"] += 1
            continue
        rows = raw if isinstance(raw, list) else []
        for acc in rows:
            if not isinstance(acc, dict):
                continue
            tok = acc.get("access_token")
            if not tok and isinstance(acc.get("tokens"), dict):
                tok = acc["tokens"].get("access_token")
            claims = decode_jwt_payload(str(tok or ""))
            flagged, reason = flag_info(claims)
            c["batch_tokens"] += 1
            c[f"batch_{reason}"] += 1
            if flagged:
                email = str(acc.get("email") or "").lower()
                if email:
                    flagged_emails.append(email)
    return {
        "batches_scanned": len(files),
        "counts": dict(c),
        "flagged_emails_sample": flagged_emails[:20],
        "flagged_unique": len(set(flagged_emails)),
    }


def main() -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(description="Scrub bfs/bot_flag JWT accounts from grok2api")
    ap.add_argument("--apply", action="store_true", help="disable flagged accounts (default: dry-run)")
    ap.add_argument("--batches", type=int, default=0, help="also scan N recent farm batches")
    ap.add_argument("--limit-disable", type=int, default=0, help="max accounts to disable (0=all)")
    ap.add_argument("--sleep", type=float, default=0.05, help="pause between PATCH calls")
    ap.add_argument(
        "--report",
        type=Path,
        default=_ROOT / "results" / f"bfs_scrub_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json",
    )
    args = ap.parse_args()

    base = _env("GROK2API_URL", "http://127.0.0.1:8000").rstrip("/")
    user = _env("GROK2API_ADMIN_USER", "admin")
    pw = _env("GROK2API_ADMIN_PASS")
    if not pw:
        print("GROK2API_ADMIN_PASS missing", file=sys.stderr)
        return 2

    print(f"[scrub] login {base} as {user}")
    token = admin_login(base, user, pw)

    print("[scrub] list accounts…")
    listed = list_accounts(base, token)
    by_email: dict[str, dict[str, Any]] = {}
    for a in listed:
        email = str(a.get("email") or a.get("name") or "").strip().lower()
        if email:
            by_email[email] = a
    print(f"[scrub] listed={len(listed)} email_index={len(by_email)}")

    print("[scrub] export tokens…")
    exported = export_accounts(base, token)
    print(f"[scrub] exported={len(exported)}")

    counts: Counter[str] = Counter()
    flagged_rows: list[dict[str, Any]] = []
    clean_rows: list[dict[str, Any]] = []

    for ent in exported:
        if not isinstance(ent, dict):
            continue
        email = str(ent.get("email") or ent.get("name") or "").strip().lower()
        access = str(ent.get("access_token") or ent.get("accessToken") or "")
        claims = decode_jwt_payload(access)
        flagged, reason = flag_info(claims)
        counts["exported"] += 1
        counts[reason] += 1
        meta = by_email.get(email) or {}
        row = {
            "email": email,
            "id": meta.get("id"),
            "enabled": meta.get("enabled"),
            "authStatus": meta.get("authStatus") or meta.get("auth_status"),
            "reason": reason,
            "referrer": (claims or {}).get("referrer"),
            "bfs": (claims or {}).get("bfs"),
            "bot_flag_source": (claims or {}).get("bot_flag_source"),
            "scope": (claims or {}).get("scope"),
            "sub": (claims or {}).get("sub"),
            "iat": (claims or {}).get("iat"),
            "exp": (claims or {}).get("exp"),
        }
        if flagged:
            flagged_rows.append(row)
        else:
            clean_rows.append(row)

    print(
        f"[scrub] pool flagged={counts.get('bfs=1', 0) + counts.get('bot_flag_source=1', 0)} "
        f"clean={counts.get('clean', 0)} nodecode/other={counts.get('no_claims', 0)}"
    )
    print(f"[scrub] referrer sample clean={[r.get('referrer') for r in clean_rows[:5]]}")

    disabled: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if args.apply:
        todo = [r for r in flagged_rows if r.get("id")]
        if args.limit_disable > 0:
            todo = todo[: args.limit_disable]
        print(f"[scrub] APPLY disable n={len(todo)}")
        for i, row in enumerate(todo, 1):
            aid = str(row["id"])
            try:
                set_enabled(base, token, aid, False)
                disabled.append({"id": aid, "email": row.get("email"), "reason": row.get("reason")})
                if i % 25 == 0 or i == len(todo):
                    print(f"  disabled {i}/{len(todo)}")
            except Exception as e:
                errors.append({"id": aid, "email": row.get("email"), "error": str(e)[:300]})
            if args.sleep > 0:
                time.sleep(args.sleep)
    else:
        print("[scrub] dry-run (pass --apply to disable)")

    batch_report = None
    if args.batches > 0:
        batch_report = scan_batches(_ROOT / "results", args.batches)
        print(f"[scrub] batches: {batch_report}")

    report = {
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "base": base,
        "apply": bool(args.apply),
        "counts": dict(counts),
        "listed": len(listed),
        "exported": len(exported),
        "flagged": len(flagged_rows),
        "clean": len(clean_rows),
        "disabled": len(disabled),
        "errors": errors,
        "flagged_sample": flagged_rows[:30],
        "clean_sample": clean_rows[:20],
        "disabled_sample": disabled[:30],
        "batches": batch_report,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"[scrub] report → {args.report}")
    print(
        json.dumps(
            {
                "flagged": len(flagged_rows),
                "clean": len(clean_rows),
                "disabled": len(disabled),
                "errors": len(errors),
                "apply": bool(args.apply),
            }
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
