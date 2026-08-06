#!/usr/bin/env python3
"""Castle.io request token extractor for x.ai signup.

Launches Camoufox (same as farm.py), navigates to accounts.x.ai/sign-up,
waits for Castle SDK to initialize, then calls Castle.createRequestToken()
to extract the token that x.ai's frontend would include in gRPC calls.

Usage:
  castle_mint.py [--proxy http://host:port] [--timeout 60]

Prints the Castle request token to stdout on success; errors to stderr, exit 1.

The token can then be passed to Grok-Register's CreateEmailCodeCastle(email, token)
as protobuf field 3 in the gRPC-Web CreateEmailValidationCode call.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
from urllib.parse import urlparse

try:
    from camoufox.async_api import AsyncCamoufox
except ImportError:
    print("ERROR: camoufox not installed", file=sys.stderr)
    sys.exit(1)

# x.ai Castle publishable key (extracted from page config)
CASTLE_PK = "pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz"
SIGNUP_URL = "https://accounts.x.ai/sign-up"


def _parse_proxy(url: str) -> dict | None:
    if not url:
        return None
    p = urlparse(url)
    out: dict = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        out["username"] = p.username
    if p.password:
        out["password"] = p.password
    return out


# JS to inject Castle SDK and generate request token
# x.ai already loads Castle in their Next.js chunks, but the SDK object
# may not be directly accessible on window. We load the CDN version with
# the same publishable key and call createRequestToken().
CASTLE_INJECT_JS = """
(async () => {
    // Check if Castle is already loaded by x.ai
    if (typeof window.__castle === 'object' && window.__castle) {
        try {
            const t = await window.__castle.createRequestToken();
            return {token: t, source: 'xai_native'};
        } catch(e) {}
    }
    // Check window.Castle (CDN SDK)
    if (typeof window.Castle === 'object' && window.Castle.createRequestToken) {
        try {
            await window.Castle.configure({pk: '%s'});
            const t = await window.Castle.createRequestToken();
            return {token: t, source: 'cdn_castle'};
        } catch(e) {
            return {error: 'createRequestToken failed: ' + e.message, source: 'cdn_castle'};
        }
    }
    // Load Castle SDK from CDN
    return new Promise((resolve) => {
        const s = document.createElement('script');
        s.src = 'https://cdn.castle.io/v2/castle.js';
        s.onload = async () => {
            try {
                if (typeof window.Castle === 'undefined') {
                    // Castle may attach under different name
                    const keys = Object.keys(window).filter(k => k.toLowerCase().includes('castle'));
                    resolve({error: 'Castle SDK loaded but not on window. Keys: ' + keys.join(','), source: 'cdn_load'});
                    return;
                }
                await window.Castle.configure({pk: '%s'});
                const t = await window.Castle.createRequestToken();
                resolve({token: t, source: 'cdn_loaded'});
            } catch(e) {
                resolve({error: 'CDN Castle createRequestToken failed: ' + e.message, source: 'cdn_loaded'});
            }
        };
        s.onerror = () => resolve({error: 'Failed to load castle.js from CDN', source: 'cdn_fail'});
        document.head.appendChild(s);
    });
})()
""" % (CASTLE_PK, CASTLE_PK)


async def mint_castle_token(proxy_url: str | None, timeout: float = 60.0) -> dict:
    """Launch Camoufox, navigate to x.ai signup, extract Castle request token."""
    kwargs: dict = {
        "headless": False,
        "humanize": 0.5,
        "os": random.choice(["windows", "macos", "linux"]),
        "locale": "en-US",
        "geoip": False,
        "block_webrtc": True,
    }
    proxy = _parse_proxy(proxy_url)
    if proxy:
        kwargs["proxy"] = proxy

    manager = AsyncCamoufox(**kwargs)
    browser = await manager.__aenter__()

    try:
        page = await browser.new_page()
        page.set_default_timeout(int(timeout * 1000))

        # Navigate to signup page — x.ai loads Castle SDK in their Next.js chunks
        print("[CASTLE] Navigating to x.ai signup...", file=sys.stderr, flush=True)
        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3.0)  # Let Next.js hydrate + Castle SDK init

        # Check page config first
        castle_cfg = await page.evaluate(
            """() => {
            const html = document.documentElement.innerHTML;
            const pkMatch = html.match(/pk_[a-zA-Z0-9]+/);
            const enableMatch = html.match(/"enableCastle":(true|false)/);
            return {
                castlePk: pkMatch ? pkMatch[0] : null,
                enableCastle: enableMatch ? enableMatch[1] : null,
            };
        }"""
        )
        print(f"[CASTLE] Page config: {castle_cfg}", file=sys.stderr, flush=True)

        if castle_cfg.get("enableCastle") == "false":
            print("[CASTLE] WARNING: enableCastle=false on page — Castle may not be active", file=sys.stderr, flush=True)

        # Simulate human behavior to feed Castle behavioral signals
        print("[CASTLE] Simulating human behavior (mouse moves)...", file=sys.stderr, flush=True)
        await page.mouse.move(100, 100)
        await asyncio.sleep(0.3)
        await page.mouse.move(300, 200, steps=10)
        await asyncio.sleep(0.2)
        await page.mouse.move(200, 400, steps=5)
        await asyncio.sleep(1.0)

        # Also set up request interceptor to capture the gRPC call
        captured_grpc: list[dict] = []

        async def on_request(request):
            url = request.url
            if "CreateEmailValidationCode" in url or "auth_mgmt" in url.lower():
                post = request.post_data_buffer
                info: dict = {
                    "url": url,
                    "post_len": len(post) if post else 0,
                }
                if post and len(post) > 5:
                    # Check protobuf field 3 (Castle token) presence
                    payload = post[5:]  # Skip gRPC-Web header
                    has_field_3 = False
                    i = 0
                    while i < len(payload):
                        tag = payload[i]
                        field_num = tag >> 3
                        wire_type = tag & 0x07
                        i += 1
                        if wire_type != 2:
                            if wire_type == 0:
                                while i < len(payload) and payload[i] & 0x80:
                                    i += 1
                                i += 1
                            continue
                        # Read varint length
                        length = 0
                        shift = 0
                        while i < len(payload):
                            b = payload[i]
                            i += 1
                            length |= (b & 0x7F) << shift
                            if not (b & 0x80):
                                break
                            shift += 7
                        if i + length > len(payload):
                            break
                        value = payload[i : i + length]
                        i += length
                        if field_num == 3 and len(value) > 10:
                            has_field_3 = True
                            info["castle_token_len"] = len(value)
                            info["castle_token_preview"] = value[:80].decode("utf-8", errors="replace")
                info["has_castle_field"] = has_field_3
                captured_grpc.append(info)
                print(f"[CASTLE] gRPC intercepted: {url}", file=sys.stderr, flush=True)
                print(f"[CASTLE]   has_castle_field={has_field_3}", file=sys.stderr, flush=True)

        page.on("request", lambda req: asyncio.create_task(on_request(req)))

        # Try to extract Castle token via JS
        print("[CASTLE] Extracting Castle request token...", file=sys.stderr, flush=True)
        result = await page.evaluate(CASTLE_INJECT_JS)

        if result.get("token"):
            token = result["token"]
            print(f"[CASTLE] ✅ Token extracted! source={result.get('source')} len={len(token)}", file=sys.stderr, flush=True)
            print(f"[CASTLE] preview: {token[:60]}...", file=sys.stderr, flush=True)
            return {
                "status": "success",
                "token": token,
                "source": result.get("source"),
                "token_len": len(token),
            }
        elif result.get("error"):
            print(f"[CASTLE] ❌ Token extraction failed: {result['error']}", file=sys.stderr, flush=True)

            # Fallback: try clicking "Sign up with email" to trigger x.ai's own Castle call
            print("[CASTLE] Fallback: triggering x.ai native Castle flow...", file=sys.stderr, flush=True)
            try:
                btn = page.get_by_role("button", name="Sign up with email")
                await btn.click(timeout=5000)
                await asyncio.sleep(1.5)

                email_input = page.locator('input[name="email"], input[type="email"]').first
                await email_input.wait_for(state="visible", timeout=5000)
                await email_input.fill(f"castle_mint_{int(time.time())}@aduhteh.my.id")
                await asyncio.sleep(0.5)

                submit = page.locator('button[type="submit"]').first
                await submit.click(timeout=5000)

                # Wait for gRPC call
                for _ in range(15):
                    if captured_grpc:
                        break
                    await asyncio.sleep(1.0)

                if captured_grpc:
                    grpc = captured_grpc[0]
                    return {
                        "status": "grpc_captured" if grpc.get("has_castle_field") else "grpc_no_castle",
                        "grpc_info": grpc,
                    }
            except Exception as e:
                print(f"[CASTLE] Fallback click failed: {e}", file=sys.stderr, flush=True)

            return {
                "status": "failed",
                "error": result.get("error", "unknown"),
                "page_config": castle_cfg,
                "grpc_captured": len(captured_grpc),
            }

    finally:
        await manager.__aexit__(None, None, None)


def main() -> int:
    ap = argparse.ArgumentParser(description="Castle.io request token extractor")
    ap.add_argument("--proxy", default=os.environ.get("GROK_PROXY", ""), help="Proxy URL")
    ap.add_argument("--timeout", type=float, default=60, help="Timeout in seconds")
    args = ap.parse_args()

    result = asyncio.run(mint_castle_token(args.proxy or None, args.timeout))

    if result.get("status") == "success":
        # Print only the token to stdout
        sys.stdout.write(result["token"])
        return 0
    else:
        print(f"\n[RESULT] {result}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
