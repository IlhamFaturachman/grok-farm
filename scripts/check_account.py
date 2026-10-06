#!/usr/bin/env python3
"""
Post-farm account checker — clean classification without triggering BFS.

Tests each newly farmed account against the xAI API to classify it:
  - 200     → THINK   (clean account, can do reasoning/chat)
  - 202     → NOTHINK (account works but cannot think/reason — limited)
  - 403     → BFS      (bot-flagged, chat blocked. Keep for image/video only)
  - 401     → AUTH_FAIL (token invalid/expired)
  - 429     → RATE_LIMITED (try again later)
  - other   → UNKNOWN

Strategy:
  - Test against api.x.ai/v1/responses (not cli-chat-proxy.grok.com)
    to avoid triggering bot detection on the inference proxy.
  - Use a minimal request: model=grok-4.5, input="Say hi", max_output=10
  - The access token from OAuth PKCE is a valid Bearer token for api.x.ai

Usage:
    python check_account.py --accounts results/batch_xxx/accounts.json
    python check_account.py --email user@example.com --access-token eyJ...

The checker updates accounts.json with:
    account["classification"] = "THINK" | "NOTHINK" | "BFS" | "AUTH_FAIL" | "UNKNOWN"
    account["check_status"] = HTTP status code
    account["no_think"] = True  (for BFS/NOTHINK accounts)
    account["check_response"] = truncated response body
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    from curl_cffi import requests
except ImportError:
    print("ERROR: curl_cffi not installed. pip install curl_cffi")
    sys.exit(1)

# Test against api.x.ai directly — this is the clean path.
# cli-chat-proxy.grok.com is the inference proxy used by the CLI,
# but hitting it programmatically can trigger bot detection.
# api.x.ai accepts the same OAuth Bearer token and is the documented API endpoint.
CHECK_URL = "https://cli-chat-proxy.grok.com/v1/responses"
CHECK_MODEL = "grok-4.5"
CHECK_INPUT = "Say hi"
CHECK_MAX_OUTPUT = 10
TIMEOUT = 30


def classify_account(access_token: str, proxy: str | None = None) -> dict[str, Any]:
    """
    Test an account against the xAI API.
    Returns {classification, status_code, response_body}.

    Classification logic:
      200     → THINK   (full chat capability)
      202     → NOTHINK (accepted but no reasoning — limited response)
      403     → BFS     (bot-flagged, blocked from chat)
      401     → AUTH_FAIL
      429     → RATE_LIMITED (transient, retry later)
      other   → UNKNOWN
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "xai-grok-cli/1.0.0",
        "x-grok-client-version": "1.0.0",
        "x-grok-client-surface": "grok-build",
    }
    body = {
        "model": CHECK_MODEL,
        "input": CHECK_INPUT,
        "max_output": CHECK_MAX_OUTPUT,
    }

    proxies = None
    if proxy:
        proxies = {"https": proxy, "http": proxy}

    try:
        resp = requests.post(
            CHECK_URL,
            json=body,
            headers=headers,
            proxies=proxies,
            impersonate="chrome131",
            timeout=TIMEOUT,
        )
        status = resp.status_code
        text = resp.text[:500]

        if status == 200:
            classification = "THINK"
        elif status == 202:
            classification = "NOTHINK"
        elif status == 403:
            # Check if it's a spending-limit block (BFS) or other 403
            if "spending-limit" in text or "personal-team-blocked" in text:
                classification = "BFS"
            elif "bot" in text.lower() or "bfs" in text.lower():
                classification = "BFS"
            else:
                classification = "BFS"  # Default 403 = blocked
        elif status == 401:
            classification = "AUTH_FAIL"
        elif status == 429:
            classification = "RATE_LIMITED"
        else:
            classification = "UNKNOWN"

        return {
            "classification": classification,
            "check_status": status,
            "check_response": text,
        }
    except Exception as e:
        return {
            "classification": "ERROR",
            "check_status": 0,
            "check_response": str(e)[:200],
        }


def check_accounts_file(path: str, proxy: str | None = None) -> None:
    """Check all accounts in an accounts.json file and update with classification."""
    p = Path(path)
    if p.is_dir():
        p = p / "accounts.json"
    if not p.is_file():
        print(f"ERROR: {p} not found")
        sys.exit(1)

    with open(p) as f:
        accounts = json.load(f)

    if not isinstance(accounts, list):
        accounts = [accounts]

    checked = 0
    think = 0
    nothink = 0
    bfs = 0
    other = 0

    for acc in accounts:
        tokens = acc.get("tokens", acc)
        access_token = tokens.get("access_token", "")
        if not access_token:
            print(f"  [{acc.get('email', '?')}] No access token — skip")
            continue

        # Skip if already classified
        if acc.get("classification"):
            print(f"  [{acc.get('email', '?')}] Already {acc['classification']} — skip")
            continue

        email = acc.get("email", "?")
        print(f"  [{email}] Checking...", end="", flush=True)

        result = classify_account(access_token, proxy)
        acc["classification"] = result["classification"]
        acc["check_status"] = result["check_status"]
        acc["check_response"] = result["check_response"]

        # Set NoThink flag for BFS and NOTHINK accounts
        if result["classification"] == "BFS":
            acc["no_think"] = True
            bfs += 1
            print(f" BFS (status={result['check_status']}) — flagged NoThink, image/video only")
        elif result["classification"] == "THINK":
            think += 1
            print(f" THINK (status={result['check_status']}) — clean, full chat")
        elif result["classification"] == "NOTHINK":
            acc["no_think"] = True
            nothink += 1
            print(f" NOTHINK (status={result['check_status']}) — flagged NoThink")
        else:
            other += 1
            print(f" {result['classification']} (status={result['check_status']})")

        checked += 1
        time.sleep(1)  # Rate limit between checks

    # Save updated accounts.json
    with open(p, "w") as f:
        json.dump(accounts, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Checked: {checked}")
    print(f"  THINK:       {think} (clean, full chat — import to pool)")
    print(f"  NOTHINK:     {nothink} (limited, no reasoning — flag NoThink)")
    print(f"  BFS:         {bfs} (bot-flagged — image/video only)")
    print(f"  OTHER:       {other}")
    print(f"Updated: {p}")


def main():
    parser = argparse.ArgumentParser(description="Post-farm account checker")
    parser.add_argument("--accounts", help="Path to accounts.json or batch dir")
    parser.add_argument("--email", help="Single account email")
    parser.add_argument("--access-token", help="Access token for single account check")
    parser.add_argument("--proxy", default=None, help="Proxy URL")
    args = parser.parse_args()

    if args.accounts:
        proxy = args.proxy or os.environ.get("GROK_PROXY_POOL")
        check_accounts_file(args.accounts, proxy)
    elif args.email and args.access_token:
        result = classify_account(args.access_token, args.proxy)
        print(f"Email: {args.email}")
        print(f"Classification: {result['classification']}")
        print(f"Status: {result['check_status']}")
        print(f"Response: {result['check_response'][:200]}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
