#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from farm import do_email_login, launch_browser, obtain_oidc_tokens
from g2a_export import convert_accounts, import_to_grok2api, write_import_json
from playwright.async_api import Error as PlaywrightError
from sso_device_mint import (
    CLIENT_ID,
    SCOPE,
    MintError,
    mint_from_cookies,
    poll_token,
    request_device_code,
)

CHAT_URL = "https://cli-chat-proxy.grok.com/v1/chat/completions"
CHAT_CLIENT_VERSION = os.environ.get("GROK_CLI_VERSION", "0.2.112")
CHAT_UA = f"grok-shell/{CHAT_CLIENT_VERSION} (linux; x86_64)"
CHAT_MARKER = "ACTIVE"
DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"


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
        password = str(row.get("password") or row.get("pass") or "").strip()
        if isinstance(row.get("pass"), bool):
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
            "max_tokens": 16,
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
            "scope": tokens.get("scope") or "",
            "token_type": "Bearer",
        },
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if not record["tokens"]["access_token"] or not record["tokens"]["refresh_token"]:
        return False
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = email.replace("@", "_at_").replace(".", "_")
    batch = out_dir / f"relogin_{stamp}_{safe}"
    batch.mkdir(parents=True, exist_ok=True)
    accounts_path = batch / "accounts.json"
    accounts_path.write_text(json.dumps([record], indent=2) + "\n", encoding="utf-8")
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
        ok_path = out_dir / "relogin_ok.jsonl"
        with ok_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "email": email,
                        "at": stamp,
                        "batch": str(batch),
                        "result": result if isinstance(result, dict) else str(result),
                    }
                )
                + "\n"
            )
        return True
    except Exception as e:
        print(f"[import] {email} FAIL {type(e).__name__}: {e}", flush=True)
        fail_path = out_dir / "relogin_import_fail.jsonl"
        with fail_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"email": email, "error": str(e), "at": stamp}) + "\n")
        return False


async def _cookies_from_page(page) -> list[dict]:
    try:
        raw = await page.context.cookies()
    except Exception:
        return []
    out: list[dict] = []
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        out.append(
            {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain"),
                "path": c.get("path") or "/",
            }
        )
    return out


async def browser_device_mint(page, cookies: list[dict], attempt: int) -> dict:
    """Open device verification in the already-logged-in browser and click Allow."""
    from farm import click_text_button, dismiss_cookie_banner, handle_turnstile

    dc = await asyncio.to_thread(request_device_code)
    user_code = str(dc["user_code"])
    device_code = str(dc["device_code"])
    verify_url = str(dc["verification_uri_complete"])
    interval = int(dc.get("interval") or 5)
    print(f"[mass] #{attempt} browser_device user_code={user_code}", flush=True)

    await page.goto(verify_url, wait_until="domcontentloaded", timeout=45000)
    await asyncio.sleep(1.0)
    await dismiss_cookie_banner(page)
    await handle_turnstile(page, attempt, max_wait=10)

    # Continue / next on device page, then exact Allow on consent.
    for _ in range(8):
        cur = ""
        try:
            cur = page.url or ""
        except Exception:
            pass
        await dismiss_cookie_banner(page)
        if "device/done" in cur or "设备已授权" in (await page.content()):
            break
        # Prefer exact Allow over "Allow all" cookie banners (already dismissed).
        clicked = await click_text_button(
            page,
            ["允许", "Allow", "Authorize", "Approve", "Continue", "继续", "Next"],
            exclude=["Google", "Deny", "Cancel", "全部允许", "Allow all", "Reject"],
        )
        if not clicked:
            # try role=button exact
            for label in ("允许", "Allow", "Continue", "继续"):
                loc = page.get_by_role("button", name=label, exact=True)
                if await loc.count() > 0:
                    try:
                        await loc.first.click(timeout=3000)
                        clicked = True
                        break
                    except Exception:
                        pass
        await asyncio.sleep(1.2)

    tr = await asyncio.to_thread(
        poll_token,
        device_code,
        interval=interval,
        expires_in=min(int(dc.get("expires_in") or 90), 90),
    )
    if not tr.get("access_token") or not tr.get("refresh_token"):
        raise MintError("browser_device poll missing tokens")
    print(f"[mass] #{attempt} browser_device mint OK", flush=True)
    return {
        "access_token": tr["access_token"],
        "refresh_token": tr["refresh_token"],
        "id_token": tr.get("id_token") or "",
        "token_type": tr.get("token_type") or "Bearer",
        "expires_in": int(tr.get("expires_in") or 21600),
        "scope": tr.get("scope") or SCOPE,
        "mint_method": "browser_device",
        "client_id": CLIENT_ID,
    }


async def relogin_once(account: Account, attempt: int, out_dir: Path) -> bool:
    manager = None
    try:
        manager, _browser, page = await launch_browser("")
        await asyncio.wait_for(
            do_email_login(page, account.email, account.password, attempt),
            timeout=150,
        )
        # Prefer SSO device-flow mint (bypasses broken PKCE consent "Access denied").
        tokens: dict = {}
        cookies = await _cookies_from_page(page)
        sso_names = [c.get("name") for c in cookies if c.get("name") in ("sso", "sso-rw")]
        print(
            f"[mass] #{attempt} post-login cookies={len(cookies)} sso={sso_names}",
            flush=True,
        )
        if cookies:
            try:
                tokens = await asyncio.wait_for(
                    asyncio.to_thread(
                        mint_from_cookies,
                        cookies,
                        log=lambda m: print(f"[mass] #{attempt} {m}", flush=True),
                    ),
                    timeout=150,
                )
                print(
                    f"[mass] #{attempt} sso_device mint OK method={tokens.get('mint_method')}",
                    flush=True,
                )
            except (MintError, asyncio.TimeoutError, OSError) as mint_err:
                print(
                    f"[mass] #{attempt} sso_device mint fail: {type(mint_err).__name__}: {mint_err}",
                    flush=True,
                )
                tokens = {}
                # Browser device-consent click path (real Allow button).
                try:
                    tokens = await asyncio.wait_for(
                        browser_device_mint(page, cookies, attempt),
                        timeout=180,
                    )
                except Exception as br_err:
                    print(
                        f"[mass] #{attempt} browser_device mint fail: "
                        f"{type(br_err).__name__}: {br_err}",
                        flush=True,
                    )
                    tokens = {}

        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            print(f"[mass] #{attempt} fallback PKCE obtain_oidc_tokens", flush=True)
            try:
                tokens = await asyncio.wait_for(
                    obtain_oidc_tokens(page, account.email, account.password, attempt),
                    timeout=180,
                )
            except Exception as pkce_err:
                print(
                    f"[mass] #{attempt} PKCE fail: {type(pkce_err).__name__}: {pkce_err}",
                    flush=True,
                )
                tokens = {}
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            print(f"[mass] #{attempt} incomplete tokens", flush=True)
            return False
        chat_ok = await asyncio.to_thread(verify_chat, tokens["access_token"])
        if not chat_ok:
            print(f"[mass] #{attempt} chat verify failed", flush=True)
            return False
        print(f"[mass] #{attempt} chat OK — importing {account.email}", flush=True)
        return await asyncio.to_thread(import_tokens, account.email, tokens, out_dir)
    except (asyncio.TimeoutError, PlaywrightError, RuntimeError, OSError) as error:
        print(
            f"[mass] #{attempt} failed: {type(error).__name__}: {error}",
            flush=True,
        )
        return False
    finally:
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass


async def run(
    accounts: list[Account],
    *,
    concurrent: int,
    delay: float,
    retries: int,
    retry_delay: float,
    out_dir: Path,
    limit: int | None,
) -> int:
    if limit is not None:
        accounts = accounts[:limit]
    workers = max(1, concurrent)
    total = len(accounts)
    print(
        f"[mass] start n={total} concurrent={workers} delay={delay}s out={out_dir}",
        flush=True,
    )
    ok = failed = completed = 0
    started = time.monotonic()
    lock = asyncio.Lock()
    sem = asyncio.Semaphore(workers)
    fail_log = out_dir / "relogin_fail.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)

    async def process_one(index: int, account: Account) -> None:
        nonlocal ok, failed, completed
        async with sem:
            print(
                f"[mass] progress={index}/{total} email={account.email} prio={account.priority}",
                flush=True,
            )
            account_ok = False
            for retry in range(1, max(1, retries) + 1):
                print(f"[mass] #{index} retry={retry}/{retries}", flush=True)
                account_ok = await relogin_once(account, index, out_dir)
                if account_ok:
                    break
                if retry < retries:
                    await asyncio.sleep(retry_delay * retry)
            async with lock:
                if account_ok:
                    ok += 1
                else:
                    failed += 1
                    with fail_log.open("a", encoding="utf-8") as f:
                        f.write(
                            json.dumps(
                                {
                                    "email": account.email,
                                    "priority": account.priority,
                                    "at": datetime.now(timezone.utc)
                                    .isoformat()
                                    .replace("+00:00", "Z"),
                                }
                            )
                            + "\n"
                        )
                completed += 1
                print(
                    f"[mass] totals ok={ok} failed={failed} completed={completed}/{total}",
                    flush=True,
                )
            if delay > 0:
                await asyncio.sleep(delay)

    tasks = [
        asyncio.create_task(process_one(i, acc))
        for i, acc in enumerate(accounts, start=1)
    ]
    if tasks:
        await asyncio.gather(*tasks)
    elapsed = int(time.monotonic() - started)
    print(f"[mass] done ok={ok} failed={failed} elapsed_s={elapsed}", flush=True)
    summary = {
        "ok": ok,
        "failed": failed,
        "total": total,
        "elapsed_s": elapsed,
        "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    (out_dir / "relogin_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return 0 if failed == 0 else 2


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(description="Mass relogin → grok2api import")
    parser.add_argument(
        "--source-accounts",
        type=Path,
        default=ROOT / "results" / "relogin_candidates.jsonl",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--concurrent", type=int, default=1)
    parser.add_argument("--delay", type=float, default=3.0)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--retry-delay", type=float, default=20.0)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "relogin_runs",
    )
    parser.add_argument("--priority", choices=["reauth", "new", "all"], default="reauth")
    parser.add_argument("--count", action="store_true")
    args = parser.parse_args()

    if not args.source_accounts.is_file():
        print(f"missing accounts file: {args.source_accounts}", file=sys.stderr)
        return 1

    accounts = load_accounts_file(args.source_accounts)
    if args.priority != "all":
        accounts = [a for a in accounts if a.priority == args.priority or not a.priority]
    if args.offset:
        accounts = accounts[args.offset :]
    if args.count:
        print(len(accounts))
        return 0

    return asyncio.run(
        run(
            accounts,
            concurrent=args.concurrent,
            delay=args.delay,
            retries=args.retries,
            retry_delay=args.retry_delay,
            out_dir=args.out_dir,
            limit=args.limit,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
