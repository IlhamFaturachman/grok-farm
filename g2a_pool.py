#!/usr/bin/env python3
"""
Continuous import pooler: watch farm results and push NEW accounts into grok2api.

Unlike end-of-batch auto-import, this runs as a daemon and:
  1. Scans results/batch_*/accounts.json (and legacy results/accounts.json)
  2. Dedups by email (local state + optional live g2a pool check)
  3. Imports only accounts not yet known
  4. Sleeps and repeats

Usage:
  python g2a_pool.py                 # loop forever
  python g2a_pool.py --once          # single scan
  python g2a_pool.py --interval 30

Env:
  GROK2API_URL=http://127.0.0.1:8000
  GROK2API_ADMIN_USER=admin
  GROK2API_ADMIN_PASS=...
  G2A_POOL_INTERVAL=20          # seconds between scans
  G2A_POOL_STATE=./results/g2a_pool_state.json
  GROK_RESULTS_DIR=./results
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    pass

from g2a_export import (  # noqa: E402
    convert_accounts,
    farm_record_to_g2a,
    import_to_grok2api,
    write_import_json,
    _env,
    _env_bool,
    admin_login,
    _http_json,
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


RESULTS_DIR = Path(_env("GROK_RESULTS_DIR", str(_ROOT / "results")))
STATE_PATH = Path(_env("G2A_POOL_STATE", str(RESULTS_DIR / "g2a_pool_state.json")))
INTERVAL = max(5, int(_env("G2A_POOL_INTERVAL", "20") or "20"))
G2A_URL = _env("GROK2API_URL", "http://127.0.0.1:8000").rstrip("/")
CHECK_LIVE = _env_bool("G2A_POOL_CHECK_LIVE", True)


def load_state() -> dict[str, Any]:
    if STATE_PATH.is_file():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("imported_emails", [])
                data.setdefault("last_scan", None)
                data.setdefault("stats", {"scans": 0, "imported": 0, "skipped": 0})
                return data
        except Exception:
            pass
    return {
        "imported_emails": [],
        "last_scan": None,
        "stats": {"scans": 0, "imported": 0, "skipped": 0},
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def iter_account_files() -> list[Path]:
    files: list[Path] = []
    if not RESULTS_DIR.is_dir():
        return files
    # per-batch
    for p in sorted(RESULTS_DIR.glob("batch_*/accounts.json")):
        files.append(p)
    # legacy root
    legacy = RESULTS_DIR / "accounts.json"
    if legacy.is_file():
        files.append(legacy)
    return files


def load_all_farm_records() -> list[dict[str, Any]]:
    """Load every success record that has tokens; newest batch last (stable)."""
    by_email: dict[str, dict[str, Any]] = {}
    for path in iter_account_files():
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "[]")
        except Exception as e:
            _log(f"WARN read {path}: {e}")
            continue
        rows = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            # must convert to g2a entry with tokens
            entry = farm_record_to_g2a(rec)
            if not entry:
                continue
            email = (entry.get("email") or rec.get("email") or "").strip().lower()
            if not email:
                # still importable via tokens alone — use access token hash-ish name
                email = "notokenemail:" + (entry.get("access_token") or "")[:24]
            # keep latest occurrence (overwrite)
            entry["_source"] = str(path)
            entry["_email_key"] = email
            by_email[email] = entry
    return list(by_email.values())


def fetch_live_emails(base_url: str) -> set[str]:
    """Best-effort: list emails already in g2a admin accounts API."""
    user = _env("GROK2API_ADMIN_USER", "admin")
    pw = _env("GROK2API_ADMIN_PASS")
    if not pw:
        return set()
    try:
        token = admin_login(base_url, user, pw)
        data = _http_json(
            "GET",
            f"{base_url.rstrip('/')}/api/admin/v1/accounts",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        root = data.get("data") if isinstance(data.get("data"), dict) else data
        items = root.get("items") if isinstance(root, dict) else root
        if not isinstance(items, list):
            return set()
        out: set[str] = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            em = (it.get("email") or it.get("name") or "").strip().lower()
            if em:
                out.add(em)
        return out
    except Exception as e:
        _log(f"live pool check failed (continuing with local state): {e}")
        return set()


def scan_and_import(*, check_live: bool = True) -> dict[str, Any]:
    state = load_state()
    known: set[str] = {e.lower() for e in state.get("imported_emails") or []}

    live: set[str] = set()
    if check_live and CHECK_LIVE:
        live = fetch_live_emails(G2A_URL)
        # merge live into known so we don't re-import accounts already in g2a
        # (e.g. imported manually or before state file existed)
        known |= live

    records = load_all_farm_records()
    new_entries: list[dict[str, Any]] = []
    for entry in records:
        key = (entry.get("_email_key") or entry.get("email") or "").lower()
        if not key:
            continue
        if key in known:
            continue
        # strip internal fields before import
        clean = {k: v for k, v in entry.items() if not k.startswith("_")}
        new_entries.append(clean)
        # mark tentatively so concurrent failures can still retry next cycle if needed
        # (we only persist after successful import)

    result: dict[str, Any] = {
        "scanned_accounts": len(records),
        "known": len(known),
        "live_pool": len(live),
        "new": len(new_entries),
        "imported": 0,
        "import_result": None,
        "error": None,
    }

    state["stats"]["scans"] = int(state["stats"].get("scans") or 0) + 1
    state["last_scan"] = _utc()

    if not new_entries:
        state["stats"]["skipped"] = int(state["stats"].get("skipped") or 0) + 1
        save_state(state)
        return result

    # write staging import file
    staging = RESULTS_DIR / "g2a_pool_pending.json"
    write_import_json({"accounts": new_entries}, staging)
    _log(f"importing {len(new_entries)} new account(s) → {G2A_URL}")

    try:
        complete = import_to_grok2api(staging, base_url=G2A_URL)
        result["import_result"] = complete
        result["imported"] = len(new_entries)
        # mark all as imported on success
        for e in new_entries:
            em = (e.get("email") or "").strip().lower()
            if em and em not in state["imported_emails"]:
                state["imported_emails"].append(em)
        # also keep any keys without email (rare)
        state["stats"]["imported"] = int(state["stats"].get("imported") or 0) + len(
            new_entries
        )
        state["last_import"] = {
            "at": _utc(),
            "count": len(new_entries),
            "emails": [e.get("email") for e in new_entries],
            "result": complete,
        }
        save_state(state)
        _log(
            f"OK imported={len(new_entries)} "
            f"created={complete.get('created')} updated={complete.get('updated')} "
            f"synced={complete.get('synced')} syncFailed={complete.get('syncFailed')}"
        )
    except Exception as e:
        result["error"] = str(e)
        state["last_error"] = {"at": _utc(), "error": str(e)}
        save_state(state)
        _log(f"IMPORT FAILED: {e}")
        raise

    return result


def bootstrap_state_from_live() -> None:
    """Seed local state with emails already in g2a so first run doesn't re-import all."""
    state = load_state()
    live = fetch_live_emails(G2A_URL)
    if not live:
        _log("bootstrap: no live emails (or g2a down)")
        return
    before = len(state["imported_emails"])
    have = {e.lower() for e in state["imported_emails"]}
    for em in sorted(live):
        if em not in have:
            state["imported_emails"].append(em)
    save_state(state)
    _log(f"bootstrap: live={len(live)} state {before} → {len(state['imported_emails'])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pool new farm accounts into grok2api")
    parser.add_argument("--once", action="store_true", help="single scan then exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help=f"seconds between scans (default {INTERVAL})",
    )
    parser.add_argument(
        "--no-live-check",
        action="store_true",
        help="only use local state file (skip g2a list)",
    )
    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help="seed state from current g2a pool then exit",
    )
    args = parser.parse_args(argv)

    if not _env("GROK2API_ADMIN_PASS"):
        print("ERROR: set GROK2API_ADMIN_PASS", file=sys.stderr)
        return 1

    global CHECK_LIVE
    if args.no_live_check:
        CHECK_LIVE = False

    interval = args.interval if args.interval is not None else INTERVAL

    _log(f"g2a pooler  results={RESULTS_DIR}  url={G2A_URL}  interval={interval}s")
    _log(f"state={STATE_PATH}")

    if args.bootstrap:
        bootstrap_state_from_live()
        return 0

    # On first start, seed from live pool so we don't re-import existing accounts
    if not STATE_PATH.is_file():
        _log("no state file — bootstrapping from live g2a pool")
        bootstrap_state_from_live()

    while True:
        try:
            r = scan_and_import(check_live=not args.no_live_check)
            if r["new"] == 0:
                _log(
                    f"scan: accounts={r['scanned_accounts']} known={r['known']} "
                    f"live={r['live_pool']} new=0"
                )
            else:
                _log(f"scan result: {r}")
        except Exception:
            traceback.print_exc()
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
