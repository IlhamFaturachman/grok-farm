#!/usr/bin/env python3
"""Standalone Castle.io probe for x.ai signup.

Launches Camoufox (same engine as farm.py), navigates to accounts.x.ai/sign-up,
intercepts the CreateEmailValidationCode gRPC-Web call, and checks whether
the Castle request token (protobuf field 3) is populated.

Usage (on server with Camoufox installed):
  python3 castle_probe.py [--proxy http://host:port]

Exit 0 = Castle token present in gRPC call
Exit 1 = Castle token absent or error
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
    print("ERROR: camoufox not installed. Run: pip install camoufox", file=sys.stderr)
    sys.exit(1)


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


def _decode_grpc_web(data: bytes) -> dict:
    """Decode gRPC-Web frame and check for Castle token (protobuf field 3).

    gRPC-Web frame: [1 byte flags] [4 bytes length] [protobuf payload]
    Protobuf: field 1 (email) = tag 0x0a, field 3 (castleToken) = tag 0x1a
    """
    result = {
        "frame_len": len(data),
        "has_email_field": False,
        "has_castle_token": False,
        "email_value": "",
        "castle_token_preview": "",
        "castle_token_len": 0,
        "raw_hex_head": data[:60].hex(),
    }
    if len(data) < 5:
        return result
    # Skip 5-byte gRPC-Web header (1 flag + 4 length)
    payload = data[5:]
    i = 0
    while i < len(payload):
        tag = payload[i]
        field_num = tag >> 3
        wire_type = tag & 0x07
        i += 1
        if wire_type != 2:  # length-delimited
            if wire_type == 0:  # varint — skip
                while i < len(payload) and payload[i] & 0x80:
                    i += 1
                i += 1
            continue
        if i >= len(payload):
            break
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
        if field_num == 1:
            result["has_email_field"] = True
            try:
                result["email_value"] = value.decode("utf-8", errors="replace")
            except Exception:
                pass
        elif field_num == 3:
            result["has_castle_token"] = True
            result["castle_token_len"] = len(value)
            try:
                tok = value.decode("utf-8", errors="replace")
                result["castle_token_preview"] = tok[:80]
            except Exception:
                pass
    return result


async def probe(proxy_url: str | None) -> dict:
    """Launch Camoufox, navigate to signup, intercept gRPC, return findings."""
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

    captured: list[dict] = []
    castle_findings: dict = {}

    manager = AsyncCamoufox(**kwargs)
    browser = await manager.__aenter__()

    try:
        page = await browser.new_page()
        page.set_default_timeout(60000)

        # ── Request interceptor: capture CreateEmailValidationCode gRPC call ──
        async def on_request(request):
            url = request.url
            if "CreateEmailValidationCode" in url or "AuthManagement" in url:
                post = request.post_data_buffer
                info: dict = {
                    "url": url,
                    "method": request.method,
                    "content_type": request.headers.get("content-type", ""),
                    "post_len": len(post) if post else 0,
                }
                if post:
                    decoded = _decode_grpc_web(post)
                    info["decoded"] = decoded
                    castle_findings.update(decoded)
                captured.append(info)
                print(f"[CASTLE] Intercepted: {url}", flush=True)
                print(f"[CASTLE]   post_len={info['post_len']}", flush=True)
                if info.get("decoded"):
                    d = info["decoded"]
                    print(f"[CASTLE]   email={d.get('email_value','?')}", flush=True)
                    print(f"[CASTLE]   has_castle_token={d.get('has_castle_token')}", flush=True)
                    print(f"[CASTLE]   castle_token_len={d.get('castle_token_len',0)}", flush=True)
                    if d.get("castle_token_preview"):
                        print(f"[CASTLE]   token_preview={d['castle_token_preview'][:60]}...", flush=True)

        page.on("request", lambda req: asyncio.create_task(on_request(req)))

        # ── Navigate to signup ──
        print("[PROBE] Navigating to accounts.x.ai/sign-up...", flush=True)
        await page.goto("https://accounts.x.ai/sign-up", wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(2.0)

        # ── Check Castle SDK presence on page ──
        castle_cfg = await page.evaluate(
            """() => {
            const html = document.documentElement.innerHTML;
            const pkMatch = html.match(/pk_[a-zA-Z0-9]+/);
            const enableMatch = html.match(/"enableCastle":(true|false)/);
            const improvedMatch = html.match(/"improvedCastleFlow":(true|false)/);
            return {
                castlePk: pkMatch ? pkMatch[0] : null,
                enableCastle: enableMatch ? enableMatch[1] : null,
                improvedCastleFlow: improvedMatch ? improvedMatch[1] : null,
            };
        }"""
        )
        print(f"[CASTLE] Page config: {castle_cfg}", flush=True)

        # ── Click "Sign up with email" ──
        print("[PROBE] Clicking Sign up with email...", flush=True)
        try:
            btn = page.get_by_role("button", name="Sign up with email")
            await btn.click(timeout=10000)
        except Exception:
            # Fallback: any button with email text
            await page.locator("button:has-text('email')").first.click(timeout=10000)
        await asyncio.sleep(1.5)

        # ── Fill email ──
        email_addr = f"castle_probe_{int(time.time())}@aduhteh.my.id"
        print(f"[PROBE] Filling email: {email_addr}", flush=True)
        email_input = page.locator('input[name="email"], input[type="email"]').first
        await email_input.wait_for(state="visible", timeout=10000)
        await email_input.fill(email_addr)
        await asyncio.sleep(0.5)

        # ── Submit ──
        print("[PROBE] Submitting signup...", flush=True)
        try:
            await page.locator('button[type="submit"]').filter(has_text="Sign up").click(timeout=5000)
        except Exception:
            await page.locator('button[type="submit"]').first.click(timeout=5000)

        # ── Wait for gRPC call to fire ──
        print("[PROBE] Waiting for gRPC CreateEmailValidationCode call...", flush=True)
        for _ in range(20):
            if captured:
                break
            await asyncio.sleep(1.0)

        result = {
            "castle_page_config": castle_cfg,
            "grpc_calls_captured": len(captured),
            "findings": castle_findings,
            "captured": captured,
        }

        if castle_findings.get("has_castle_token"):
            print("[RESULT] ✅ Castle token IS present in gRPC call", flush=True)
            print(f"[RESULT]   token_len={castle_findings.get('castle_token_len',0)}", flush=True)
            result["status"] = "castle_token_present"
        else:
            print("[RESULT] ❌ Castle token NOT present in gRPC call (field 3 empty)", flush=True)
            result["status"] = "castle_token_absent"

        return result

    finally:
        await manager.__aexit__(None, None, None)


def main():
    parser = argparse.ArgumentParser(description="Castle.io probe for x.ai signup")
    parser.add_argument("--proxy", default=os.environ.get("GROK_PROXY", ""), help="Proxy URL")
    args = parser.parse_args()

    result = asyncio.run(probe(args.proxy or None))
    status = result.get("status", "unknown")
    print(f"\n=== FINAL: {status} ===", flush=True)
    sys.exit(0 if status == "castle_token_present" else 1)


if __name__ == "__main__":
    main()
