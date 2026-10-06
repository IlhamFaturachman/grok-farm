#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from farm import exchange_code_for_tokens
from g2a_export import convert_accounts, import_to_grok2api, write_import_json
from xconsole_client import ProtocolOAuthClient, TurnstileSolver, XConsoleAuthClient
from xconsole_client.client import SIGNIN_URL
from xconsole_client.oauth import TURNSTILE_SITEKEY

CHAT_URL = "https://cli-chat-proxy.grok.com/v1/chat/completions"
CHAT_CLIENT_VERSION = os.environ.get("GROK_CLI_VERSION", "0.2.112")
CHAT_UA = f"grok-shell/{CHAT_CLIENT_VERSION} (linux; x86_64)"
CHAT_MARKER = "ACTIVE"
DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_SCOPE = (
    "openid profile email offline_access "
    "grok-cli:access api:access conversations:read conversations:write"
)


@dataclass(frozen=True, slots=True)
class Account:
    email: str
    password: str
    priority: str = ""


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            os.environ.setdefault(k, v)


def load_accounts_file(path: Path) -> list[Account]:
    out: list[Account] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        email = (row.get("email") or "").strip().lower()
        password = str(row.get("password") or "").strip()
        if not email or not password or password in ("True", "False"):
            continue
        out.append(
            Account(
                email=email,
                password=password,
                priority=str(row.get("priority") or ""),
            )
        )
    return out


def verify_chat(access_token: str) -> bool:
    body = json.dumps(
        {
            "model": "grok-4.5",
            "messages": [
                {"role": "user", "content": f"Reply with exactly {CHAT_MARKER}"}
            ],
            "max_tokens": 8192,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        CHAT_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": CHAT_UA,
            "x-grok-client-version": CHAT_CLIENT_VERSION,
            "X-Grok-Client-Version": CHAT_CLIENT_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else {}
            choices = data.get("choices") or []
            if not choices:
                return False
            msg = choices[0].get("message") or {}
            content = str(msg.get("content") or "").strip()
            return resp.status == 200 and (
                content == CHAT_MARKER or CHAT_MARKER in content
            )
    except Exception as e:
        print(f"[chat] verify failed: {type(e).__name__}: {e}", flush=True)
        return False


def import_tokens(email: str, tokens: dict, out_dir: Path) -> bool:
    record = {
        "email": email,
        "tokens": {
            "access_token": tokens.get("access_token") or "",
            "refresh_token": tokens.get("refresh_token") or "",
            "id_token": tokens.get("id_token") or "",
            "expires_at": tokens.get("expires_at") or "",
            "expires_in": tokens.get("expires_in") or 21600,
            "client_id": tokens.get("client_id") or DEFAULT_CLIENT_ID,
            "scope": tokens.get("scope") or DEFAULT_SCOPE,
            "token_type": "Bearer",
        },
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if not record["tokens"]["access_token"] or not record["tokens"]["refresh_token"]:
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = email.replace("@", "_at_").replace(".", "_")[:80]
    batch = out_dir / f"http_{stamp}_{safe}"
    batch.mkdir(parents=True, exist_ok=True)
    (batch / "accounts.json").write_text(
        json.dumps([record], indent=2) + "\n", encoding="utf-8"
    )
    doc = convert_accounts([record])
    import_path = batch / "g2a_import.json"
    write_import_json(doc, import_path)
    base = (os.environ.get("GROK2API_URL") or "http://127.0.0.1:8000").rstrip("/")
    user = os.environ.get("GROK2API_ADMIN_USER") or "admin"
    password = os.environ.get("GROK2API_ADMIN_PASS") or ""
    if not password:
        print("[import] GROK2API_ADMIN_PASS missing", flush=True)
        return False
    try:
        result = import_to_grok2api(
            import_path,
            base_url=base,
            username=user,
            password=password,
        )
        print(f"[import] {email} -> {result}", flush=True)
        with (out_dir / "relogin_ok.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "email": email,
                        "at": stamp,
                        "mode": "http",
                        "result": result if isinstance(result, dict) else str(result),
                    }
                )
                + "\n"
            )
        return True
    except Exception as e:
        print(f"[import] {email} FAIL {type(e).__name__}: {e}", flush=True)
        with (out_dir / "relogin_import_fail.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"email": email, "error": str(e), "at": stamp}) + "\n")
        return False


def relogin_http_one(account: Account, attempt: int, out_dir: Path) -> bool:
    print(f"[http] #{attempt} start {account.email}", flush=True)
    client = XConsoleAuthClient(
        impersonate=os.environ.get("GROK_IMPERSONATE", "chrome131"),
        debug=True,
    )
    solver = TurnstileSolver(debug=True)
    try:
        sso = None
        last_err = ""
        for try_i in range(1, 4):
            print(f"[http] #{attempt} turnstile try={try_i}", flush=True)
            ts = solver.solve_turnstile(SIGNIN_URL, TURNSTILE_SITEKEY, proxy="")
            if not ts:
                last_err = "turnstile solve failed"
                print(f"[http] #{attempt} {last_err}", flush=True)
                time.sleep(2)
                continue
            print(f"[http] #{attempt} turnstile ok len={len(ts)}", flush=True)
            sso = client.obtain_session_via_password(
                email=account.email,
                password=account.password,
                turnstile_token=ts,
                referer=SIGNIN_URL,
            )
            if sso:
                break
            last_err = "CreateSession SSO empty"
            print(f"[http] #{attempt} {last_err}", flush=True)
            time.sleep(1.5)
        if not sso:
            raise RuntimeError(f"SSO failed: {last_err}")
        client.set_sso_cookie(sso)
        print(f"[http] #{attempt} SSO ok — OAuth protocol", flush=True)
        oauth = ProtocolOAuthClient(client.session, debug=True)
        oauth_result = oauth.login(
            sso_token=sso,
            email=account.email,
            password=account.password,
            scopes=DEFAULT_SCOPE,
        )
        code = oauth_result["code"]
        verifier = oauth_result["verifier"]
        print(f"[http] #{attempt} code captured — exchange", flush=True)
        tokens = exchange_code_for_tokens(code, verifier)
        if not tokens.get("email"):
            tokens["email"] = account.email
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise RuntimeError("token exchange incomplete")
        if not verify_chat(tokens["access_token"]):
            raise RuntimeError("chat verify failed")
        print(f"[http] #{attempt} chat OK — import", flush=True)
        return import_tokens(account.email, tokens, out_dir)
    except Exception as e:
        print(
            f"[http] #{attempt} FAIL {type(e).__name__}: {e}",
            flush=True,
        )
        with (out_dir / "relogin_fail.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "email": account.email,
                        "error": f"{type(e).__name__}: {e}",
                        "mode": "http",
                        "at": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    }
                )
                + "\n"
            )
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(description="HTTP mass relogin → grok2api")
    parser.add_argument(
        "--source-accounts",
        type=Path,
        default=ROOT / "results" / "relogin_candidates.jsonl",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--concurrent", type=int, default=2)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "relogin_http_runs",
    )
    parser.add_argument("--priority", choices=["reauth", "new", "all"], default="reauth")
    parser.add_argument("--count", action="store_true")
    args = parser.parse_args()

    if not args.source_accounts.is_file():
        print(f"missing {args.source_accounts}", file=sys.stderr)
        return 1

    accounts = load_accounts_file(args.source_accounts)
    if args.priority != "all":
        accounts = [a for a in accounts if a.priority == args.priority or not a.priority]
    if args.offset:
        accounts = accounts[args.offset :]
    if args.limit is not None:
        accounts = accounts[: args.limit]
    if args.count:
        print(len(accounts))
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    total = len(accounts)
    workers = max(1, args.concurrent)
    print(
        f"[http-mass] n={total} concurrent={workers} delay={args.delay}s out={args.out_dir}",
        flush=True,
    )
    ok = failed = 0
    t0 = time.monotonic()

    def work(item: tuple[int, Account]) -> bool:
        idx, acc = item
        success = relogin_http_one(acc, idx, args.out_dir)
        if args.delay > 0:
            time.sleep(args.delay)
        return success

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(work, (i, acc)): i for i, acc in enumerate(accounts, start=1)
        }
        for fut in as_completed(futs):
            try:
                if fut.result():
                    ok += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                print(f"[http-mass] worker error: {e}", flush=True)
            done = ok + failed
            print(
                f"[http-mass] totals ok={ok} failed={failed} completed={done}/{total}",
                flush=True,
            )

    elapsed = int(time.monotonic() - t0)
    summary = {
        "ok": ok,
        "failed": failed,
        "total": total,
        "elapsed_s": elapsed,
        "mode": "http",
        "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    (args.out_dir / "relogin_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[http-mass] done {summary}", flush=True)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
