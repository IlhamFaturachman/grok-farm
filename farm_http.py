#!/usr/bin/env python3
"""
Grok / xAI Standalone Farmer — HTTP-only edition.

Pure HTTP registration: no browser, no Camoufox, no Playwright.
Uses curl_cffi + gRPC-web + Next.js server actions + external Turnstile solver.

Reuses email generation, IMAP OTP, results saving, token exchange, and g2a
export from farm.py — same output format, same batch structure.

Usage:
  python farm_http.py -n 10 -c 3 -y
  python farm_http.py --count 5 --concurrent 2
"""
from __future__ import annotations

import asyncio
import builtins
import os
import random
import sys
import time
from datetime import datetime, timezone

# Import shared utilities from farm.py (camoufox is now lazy-imported)
from farm import (
    ACCOUNT_PASSWORD,
    CONCURRENT,
    FIRST_NAMES,
    LAST_NAMES,
    MAX_ACCOUNTS,
    OTP_TIMEOUT_S,
    PROXY_POOL,
    REJECT_BFS,
    SPAWN_DELAY,
    _env,
    _env_bool,
    _load_used_emails,
    exchange_code_for_tokens,
    generate_email,
    init_batch,
    next_proxy,
    read_otp_from_imap_sync,
    save_failed_to_file,
    save_result_to_file,
    BATCH_DIR,
    RESULTS_JSON,
)

from xconsole_client import XConsoleAuthClient, ProtocolOAuthClient, TurnstileSolver
from xconsole_client.client import SIGNUP_URL, SIGNIN_URL

# ── Config ──────────────────────────────────────────────────────────────────

ACCOUNT_TIMEOUT_S = max(120, int(_env("GROK_ACCOUNT_TIMEOUT", "600") or "600"))
IMPERSONATE = _env("GROK_IMPERSONATE", "chrome131")
GROK2API_EXPORT = _env_bool("GROK2API_EXPORT", default=False)
GROK2API_AUTO_IMPORT = _env_bool("GROK2API_AUTO_IMPORT", False)
GROK2API_URL = _env("GROK2API_URL")

# Progress tracking
created = 0
failed = 0
next_attempt = 1
counter_lock = asyncio.Lock()


def emit(attempt: int, stage: str, msg: str, email: str = ""):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] [{attempt}] {stage:<20s} {msg}"
    if email:
        line += f"  <{email}>"
    print(line, flush=True)


def random_name() -> tuple[str, str]:
    return random.choice(FIRST_NAMES), random.choice(LAST_NAMES)


# ── Core registration ───────────────────────────────────────────────────────

async def register_one_http(attempt_num: int, semaphore: asyncio.Semaphore) -> dict | None:
    async with semaphore:
        return await _do_register_http(attempt_num)


async def _do_register_http(attempt_num: int) -> dict | None:
    email_addr = await generate_email()
    password = ACCOUNT_PASSWORD
    proxy_url, proxy_id = await next_proxy()

    emit(attempt_num, "start", "Starting HTTP farm", email_addr)

    try:
        result = await asyncio.wait_for(
            _do_register_body_http(attempt_num, email_addr, password, proxy_url),
            timeout=ACCOUNT_TIMEOUT_S,
        )
        await save_result_to_file(result)
        emit(attempt_num, "OK", "Account farmed (tokens saved)", email_addr)
        return result
    except asyncio.TimeoutError:
        msg = f"account timeout after {ACCOUNT_TIMEOUT_S}s"
        emit(attempt_num, "FAIL", msg, email_addr)
        await save_failed_to_file(attempt_num, email_addr, msg)
        return None
    except Exception as e:
        emit(attempt_num, "FAIL", str(e)[:200], email_addr)
        await save_failed_to_file(attempt_num, email_addr, str(e)[:400])
        return None


async def _do_register_body_http(
    attempt_num: int, email_addr: str, password: str, proxy_url: str | None
) -> dict:
    """Core HTTP-only registration path."""

    # Init solver
    solver = TurnstileSolver(debug=True)

    # Init client
    emit(attempt_num, "init", "Creating HTTP client", email_addr)
    client = XConsoleAuthClient(
        impersonate=IMPERSONATE,
        debug=True,
        proxy=proxy_url,
    )

    try:
        # 1. Warm up + load signup page
        emit(attempt_num, "visit", "Visiting console.x.ai", email_addr)
        client.visit_home()

        emit(attempt_num, "signup_page", "Loading signup page", email_addr)
        client.load_signup_page()
        sitekey = client.turnstile_sitekey or "0x4AAAAAAAhr9JGVDZbrZOo0"

        # 2. Send OTP email
        emit(attempt_num, "send_otp", "Requesting email validation code", email_addr)
        r = client.create_email_validation_code(email_addr)
        if not r.ok:
            raise RuntimeError(f"CreateEmailValidationCode failed: grpc={r.grpc_status} trailers={r.trailers}")

        # 3. Wait for OTP via IMAP (reuse farm.py's reader)
        emit(attempt_num, "wait_otp", "Waiting for xAI confirmation code via IMAP", email_addr)
        loop = asyncio.get_event_loop()
        otp = await loop.run_in_executor(
            None, lambda: read_otp_from_imap_sync(email_addr, OTP_TIMEOUT_S)
        )
        if not otp:
            raise RuntimeError(f"OTP not received after {OTP_TIMEOUT_S}s")
        emit(attempt_num, "got_otp", f"Code: {otp}", email_addr)

        # 4. Verify OTP (retry once — empty grpc under concurrent load)
        emit(attempt_num, "verify_otp", "Verifying email code", email_addr)
        v = client.verify_email_validation_code(email_addr, otp)
        if not v.ok:
            alt = otp.replace("-", "").replace(" ", "")
            if alt != otp:
                v = client.verify_email_validation_code(email_addr, alt)
            if not v.ok:
                await asyncio.sleep(1.5)
                v = client.verify_email_validation_code(
                    email_addr, alt if alt != otp and alt else otp
                )
            if not v.ok:
                raise RuntimeError(
                    f"VerifyEmailValidationCode failed: grpc={v.grpc_status} trailers={v.trailers}"
                )

        # 5. Re-scrape signup action AFTER OTP wait (ids rotate with deploys;
        #    long IMAP waits are the main source of "Server action not found")
        emit(attempt_num, "refresh_action", "Refreshing signup server action", email_addr)
        try:
            client.refresh_signup_action()
            sitekey = client.turnstile_sitekey or sitekey
        except Exception as e:
            emit(attempt_num, "refresh_action", f"warn: {e}", email_addr)

        # 6. Solve Turnstile
        emit(attempt_num, "solve_turnstile", f"Solving Turnstile (sitekey={sitekey[:16]}...)", email_addr)
        turnstile_token = solver.solve_turnstile(SIGNUP_URL, sitekey, proxy=proxy_url or "")
        if not turnstile_token:
            raise RuntimeError("Turnstile solve failed — solver unavailable or timed out")
        emit(attempt_num, "turnstile_solved", f"Token len={len(turnstile_token)}", email_addr)

        # 7. Create account (create_account also refreshes action + retries 404)
        given, family = random_name()
        emit(attempt_num, "create_account", f"Profile: {given} {family}", email_addr)
        res = client.create_account(
            email=email_addr,
            given_name=given,
            family_name=family,
            password=password,
            email_validation_code=otp,
            turnstile_token=turnstile_token,
            castle_request_token="",
            refresh_action=False,  # already refreshed above; still retries on 404
        )
        if not res.ok and res.http_status == 404:
            # Turnstile token is single-use — re-solve after action refresh
            emit(attempt_num, "create_retry", "404 action missing — re-solve Turnstile", email_addr)
            try:
                client.refresh_signup_action()
            except Exception:
                pass
            turnstile_token = solver.solve_turnstile(SIGNUP_URL, sitekey, proxy=proxy_url or "")
            if not turnstile_token:
                raise RuntimeError("Turnstile re-solve failed after create 404")
            res = client.create_account(
                email=email_addr,
                given_name=given,
                family_name=family,
                password=password,
                email_validation_code=otp,
                turnstile_token=turnstile_token,
                castle_request_token="",
                refresh_action=False,
            )
        if not res.ok:
            raise RuntimeError(
                f"create_account failed: HTTP {res.http_status} body={(res.rsc_body or '')[:120]!r}"
            )

        # 8. Establish SSO session JWT (prefer CreateSession — reliable for OAuth)
        emit(attempt_num, "fetch_sso", "Establishing SSO session", email_addr)
        sso = client.fetch_sso_token(retries=2)
        # CreateSession is needed for a real session_id JWT; RSC often only embeds
        # unrelated eyJ blobs that fail OAuth consent.
        emit(attempt_num, "create_session", "CreateSession password login for SSO cookie", email_addr)
        for attempt in range(2):
            turnstile2 = solver.solve_turnstile(SIGNIN_URL, sitekey, proxy=proxy_url or "")
            if not turnstile2:
                continue
            sso2 = client.obtain_session_via_password(
                email=email_addr,
                password=password,
                turnstile_token=turnstile2,
            )
            if sso2:
                sso = sso2
                break
            if attempt == 0:
                emit(attempt_num, "create_session", "retry CreateSession", email_addr)
                await asyncio.sleep(1.0)
        if not sso:
            raise RuntimeError("SSO token not obtained after signup + CreateSession")
        client.set_sso_cookie(sso)

        # 9. OAuth PKCE → get authorization code
        emit(attempt_num, "oauth", "Starting OAuth PKCE flow", email_addr)
        oauth = ProtocolOAuthClient(client.session, debug=True)
        oauth_result = oauth.login(sso_token=sso, email=email_addr, password=password)
        code = oauth_result["code"]
        verifier = oauth_result["verifier"]

        emit(attempt_num, "token_exchange", "Exchanging code for tokens", email_addr)
        tokens = exchange_code_for_tokens(code, verifier)
        if not tokens.get("email"):
            tokens["email"] = email_addr
        if tokens.get("bot_flagged") and REJECT_BFS:
            reason = tokens.get("bot_flag_reason") or "bfs"
            ref = tokens.get("referrer") or "?"
            raise RuntimeError(
                f"access JWT bot-flagged ({reason}, referrer={ref}) — drop (GROK_REJECT_BFS=1)"
            )

        return {
            "email": email_addr,
            "password": password,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "attempt": attempt_num,
            "proxy": proxy_url or "direct",
            "tokens": tokens,
        }
    finally:
        client.close()


# ── Worker ──────────────────────────────────────────────────────────────────

async def worker(target: int, semaphore: asyncio.Semaphore):
    global created, failed, next_attempt
    while True:
        async with counter_lock:
            if next_attempt > target:
                return
            num = next_attempt
            next_attempt += 1
        if SPAWN_DELAY > 0:
            await asyncio.sleep(SPAWN_DELAY * ((num - 1) % max(1, target)))
        res = await register_one_http(num, semaphore)
        async with counter_lock:
            if res:
                created += 1
            else:
                failed += 1


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    global created, failed, next_attempt

    if not os.environ.get("GROK_IMAP_USER") or not os.environ.get("GROK_IMAP_PASS"):
        print("ERROR: set GROK_IMAP_USER and GROK_IMAP_PASS in .env", flush=True)
        sys.exit(1)

    _load_used_emails()

    print("=" * 60, flush=True)
    print("  Grok / xAI HTTP-Only Farmer", flush=True)
    print("=" * 60, flush=True)
    print(f"  Mode     : HTTP (no browser)", flush=True)
    print(f"  Fingerprint: {IMPERSONATE}", flush=True)
    print(f"  Proxies  : {len(PROXY_POOL)} ({'direct' if not PROXY_POOL else 'pool'})", flush=True)
    print(f"  Solver   : {os.environ.get('SOLVER_URL', 'not set (CapSolver fallback)')}", flush=True)
    print("-" * 60, flush=True)

    # Parse CLI args (same as farm.py)
    arg_count: int | None = None
    arg_conc: int | None = None
    skip_prompt = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-n", "--count", "--max") and i + 1 < len(args):
            arg_count = int(args[i + 1]); i += 2; continue
        if a in ("-c", "--concurrent") and i + 1 < len(args):
            arg_conc = int(args[i + 1]); i += 2; continue
        if a in ("-y", "--yes", "--non-interactive"):
            skip_prompt = True; i += 1; continue
        if a in ("-h", "--help"):
            print("Usage: python farm_http.py [-n COUNT] [-c CONCURRENT] [-y]")
            return 0
        i += 1

    if skip_prompt:
        max_accounts = arg_count or MAX_ACCOUNTS
        concurrent = arg_conc or CONCURRENT
    else:
        def _prompt_int(label, default, mn=1, mx=1000):
            s = input(f"  {label} [{default}]: ").strip()
            return max(mn, min(mx, int(s) or default))
        max_accounts = arg_count or _prompt_int("How many accounts?", MAX_ACCOUNTS)
        concurrent = arg_conc or _prompt_int("Concurrent workers?", CONCURRENT, mx=20)

    max_accounts = max(1, min(1000, int(max_accounts)))
    concurrent = max(1, min(20, int(concurrent)))

    print(f"  Accounts : {max_accounts}", flush=True)
    print(f"  Workers  : {concurrent}", flush=True)
    print(f"  Timeout  : {ACCOUNT_TIMEOUT_S}s per account", flush=True)
    print("=" * 60, flush=True)

    batch_id = init_batch(max_accounts, concurrent)
    print(f"  Batch    : {batch_id}", flush=True)
    print(f"  Dir      : {BATCH_DIR}", flush=True)

    # Open log file
    log_path = BATCH_DIR / "farm.log"
    logf = open(log_path, "a", buffering=1)

    _orig_print = builtins.print

    def _log_print(*a, **kw):
        msg = " ".join(str(x) for x in a)
        _orig_print(msg, **kw)  # console
        try:
            logf.write(msg + "\n")
        except Exception:
            pass

    builtins.print = _log_print

    start = time.time()
    semaphore = asyncio.Semaphore(concurrent)

    try:
        workers = [asyncio.create_task(worker(max_accounts, semaphore)) for _ in range(concurrent)]
        await asyncio.gather(*workers)
    finally:
        builtins.print = _orig_print
        logf.close()

    elapsed = int(time.time() - start)

    # Update batch meta
    try:
        import json
        from pathlib import Path
        meta_path = BATCH_DIR / "batch_meta.json"
        meta = {}
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text())
        meta.update({
            "finished_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "created": created,
            "failed": failed,
            "elapsed_s": elapsed,
            "mode": "http",
        })
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    except Exception:
        pass

    # g2a export
    g2a_info = ""
    if GROK2API_EXPORT or GROK2API_AUTO_IMPORT:
        try:
            from g2a_export import export_and_maybe_import
            g2a = export_and_maybe_import(
                BATCH_DIR if BATCH_DIR.is_dir() else RESULTS_JSON,
                do_import=GROK2API_AUTO_IMPORT,
                base_url=GROK2API_URL or None,
            )
            g2a_info = f"export={g2a.get('export_path','')} converted={g2a.get('converted',0)} imported={g2a.get('imported',False)}"
        except Exception as e:
            g2a_info = f"g2a export error: {e}"

    # Summary
    print()
    print("=" * 60)
    print(f"  DONE: {created} created, {failed} failed in {elapsed}s")
    print(f"  Batch: {batch_id}")
    print(f"  Dir  : {BATCH_DIR}")
    print(f"  JSON : {BATCH_DIR}/accounts.json")
    print(f"  TXT  : {BATCH_DIR}/accounts.txt")
    print(f"  Log  : {log_path}")
    if g2a_info:
        print(f"  g2a  : {g2a_info}")
    print("=" * 60)

    try:
        from notify import maybe_alert_batch

        maybe_alert_batch(BATCH_DIR, created=created, failed=failed)
    except Exception as e:
        print(f"[notify] skipped: {e}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
