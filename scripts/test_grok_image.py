"""
Test script: Generate images via Grok CLI free path using tokens from a.json.

Free grok-cli accounts: image gen goes through chat Responses API + tool
(not /v1/images/generations which needs SuperGrok / prepaid credits):

  POST https://cli-chat-proxy.grok.com/v1/responses
  tools: [{"type": "image_generation"}]
  model: grok-4.5

Response output items include type "image_generation_call" with base64 JPEG result.

Auth / token refresh / a.json persistence match test_grok.py.

Usage:
  python test_grok_image.py
  python test_grok_image.py --prompt "A cat astronaut on Mars"
  python test_grok_image.py --account 0 --out-dir ./generated
  python test_grok_image.py --effort medium
  python test_grok_image.py --stream
  python test_grok_image.py --direct   # paid Imagine API /images/generations
  python test_grok_image.py --billing
"""

import argparse
import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Config ───────────────────────────────────────────────────────────────────

# Default: latest batch accounts.json from the farm. Override with --accounts.
# Accepts farm's accounts.json (list of {email, password, tokens:{access_token, refresh_token, ...}})
# or a standalone a.json with the same structure.
DEFAULT_ACCOUNTS_PATH = str(Path(__file__).resolve().parent.parent / "results" / "accounts.json")
GROK_JSON_PATH = os.environ.get("GROK_ACCOUNTS_PATH", DEFAULT_ACCOUNTS_PATH)

XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"

GROK_RESPONSES_URL = "https://cli-chat-proxy.grok.com/v1/responses"
GROK_MODELS_URL = "https://cli-chat-proxy.grok.com/v1/models"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_USER_URL = "https://cli-chat-proxy.grok.com/v1/user?include=subscription"
XAI_IMAGES_URL = "https://api.x.ai/v1/images/generations"

CLIENT_VERSION = "0.2.93"
USER_AGENT = f"grok-pager/{CLIENT_VERSION} grok-shell/{CLIENT_VERSION} (linux; x86_64)"

DEFAULT_MODEL = "grok-4.5"
DEFAULT_EFFORT = "high"
DEFAULT_PROMPT = "A cat astronaut on Mars, cinematic lighting"
DEFAULT_OUT_DIR = "generated_images"
EFFORT_LEVELS = ["low", "medium", "high"]


# ─── Token Refresh ────────────────────────────────────────────────────────────

def decode_jwt_exp(token):
    try:
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("exp")
    except Exception:
        return None


def is_token_valid(access_token, leeway_seconds=60):
    exp = decode_jwt_exp(access_token)
    if not exp:
        return False
    return time.time() < (exp - leeway_seconds)


def refresh_access_token(refresh_token, client_id=None):
    cid = client_id or XAI_CLIENT_ID
    print(f"  ⏳ Refreshing access token... (client_id={cid[:12]}...)")
    resp = requests.post(
        XAI_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": cid,
            "refresh_token": refresh_token,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  ✗ Token refresh failed: HTTP {resp.status_code}")
        print(f"    {resp.text[:500]}")
        return None, None

    data = resp.json()
    access_token = data.get("access_token")
    new_refresh_token = data.get("refresh_token", "")
    if not access_token:
        print(f"  ✗ No access_token in refresh response: {json.dumps(data)[:300]}")
        return None, None

    print(f"  ✓ Token refreshed (expires in {data.get('expires_in', '?')}s)")
    return access_token, new_refresh_token


def save_tokens_to_file(accounts, account_idx, access_token, refresh_token, expires_in=None, accounts_path=None):
    path = accounts_path or GROK_JSON_PATH
    tokens = accounts[account_idx].setdefault("tokens", {})
    tokens["access_token"] = access_token
    if refresh_token:
        tokens["refresh_token"] = refresh_token
    if expires_in is not None:
        tokens["expires_in"] = expires_in
        tokens["expires_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with open(path, "w") as f:
        json.dump(accounts, f, indent=2)
    print(f"  💾 Tokens saved to {path}")


# ─── Account Loading ──────────────────────────────────────────────────────────

def load_accounts(path=None):
    path = path or GROK_JSON_PATH
    with open(path, "r") as f:
        accounts = json.load(f)
    print(f"  ✓ Loaded {len(accounts)} account(s) from {path}")
    return accounts


def get_access_token(accounts, account_idx, accounts_path=None):
    if account_idx >= len(accounts):
        print(f"  ✗ Account index {account_idx} out of range (have {len(accounts)} accounts)")
        sys.exit(1)

    acct = accounts[account_idx]
    email = acct.get("email") or acct.get("tokens", {}).get("email", "?")
    tokens = acct.get("tokens", {})
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    client_id = tokens.get("client_id")

    print(f"  📧 Account [{account_idx}]: {email}")

    if access_token and is_token_valid(access_token):
        print("  ✓ Access token still valid, skipping refresh")
        return access_token, email

    if not refresh_token:
        print("  ✗ No refresh_token available and access token is expired/missing")
        sys.exit(1)

    access_token, new_refresh_token = refresh_access_token(refresh_token, client_id=client_id)
    if not access_token:
        print("  ✗ Could not refresh token")
        sys.exit(1)

    exp = decode_jwt_exp(access_token)
    expires_in = int(exp - time.time()) if exp else None
    save_tokens_to_file(accounts, account_idx, access_token, new_refresh_token, expires_in, accounts_path=accounts_path)
    return access_token, email


# ─── Headers ──────────────────────────────────────────────────────────────────

def build_headers(access_token, email=None, model=None, accept="application/json"):
    session_id = str(uuid.uuid4())
    req_id = str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "Accept": accept,
        "Authorization": f"Bearer {access_token}",
        "User-Agent": USER_AGENT,
        "x-xai-token-auth": "xai-grok-cli",
        "x-grok-client-identifier": "grok-pager",
        "x-grok-client-version": CLIENT_VERSION,
        "x-authenticateresponse": "authenticate-response",
        "x-grok-session-id": session_id,
        "x-grok-conv-id": session_id,
        "x-grok-req-id": req_id,
        "x-grok-turn-idx": "1",
        "x-compaction-at": "400000",
    }
    if model:
        headers["x-grok-model-override"] = model
    if email and email != "?":
        headers["x-email"] = email
    return headers


# ─── CLI free path: /v1/responses + image_generation tool ─────────────────────

def build_tool_body(prompt, model, effort="high", stream=False):
    text = f"Generate an image: {prompt}. Use the image_generation tool."
    return {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": text,
                    }
                ],
            }
        ],
        "tools": [{"type": "image_generation"}],
        "stream": stream,
        "store": False,
        "reasoning": {
            "effort": effort,
            "summary": "concise",
        },
    }


def _strip_data_uri(b64_or_uri):
    if not isinstance(b64_or_uri, str):
        return None
    s = b64_or_uri.strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    return s


def _extract_b64_from_item(item):
    """Pull base64 JPEG from an image_generation_call (or nested) item."""
    if not isinstance(item, dict):
        return None

    for key in ("result", "image", "b64_json", "base64", "data"):
        val = item.get(key)
        if isinstance(val, str) and len(val) > 100:
            return _strip_data_uri(val)
        if isinstance(val, dict):
            for k2 in ("b64_json", "base64", "result", "data", "image"):
                v2 = val.get(k2)
                if isinstance(v2, str) and len(v2) > 100:
                    return _strip_data_uri(v2)

    for nest_key in ("output", "content", "results"):
        nested = item.get(nest_key)
        if isinstance(nested, list):
            for sub in nested:
                found = _extract_b64_from_item(sub)
                if found:
                    return found
        elif isinstance(nested, dict):
            found = _extract_b64_from_item(nested)
            if found:
                return found
    return None


def extract_images_from_response(data):
    """Find image_generation_call items and return list of base64 strings."""
    images = []
    if not isinstance(data, dict):
        return images

    candidates = []
    for key in ("output", "data"):
        block = data.get(key)
        if isinstance(block, list):
            candidates.extend(block)

    response_obj = data.get("response")
    if isinstance(response_obj, dict):
        out = response_obj.get("output")
        if isinstance(out, list):
            candidates.extend(out)

    for item in candidates:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "")
        if itype in ("image_generation_call", "image_generation", "image_gen_call"):
            b64 = _extract_b64_from_item(item)
            if b64:
                images.append(b64)
            continue
        b64 = _extract_b64_from_item(item)
        if b64 and itype and "image" in itype:
            images.append(b64)

    if not images:
        for item in candidates:
            b64 = _extract_b64_from_item(item) if isinstance(item, dict) else None
            if b64 and len(b64) > 500:
                images.append(b64)

    return images


def save_b64_images(b64_list, out_dir, prefix="grok"):
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved = []
    for i, b64 in enumerate(b64_list):
        try:
            raw = base64.b64decode(b64)
        except Exception as e:
            print(f"  ✗ Failed to decode image {i}: {e}")
            continue
        path = Path(out_dir) / f"{prefix}_{stamp}_{i}.jpg"
        path.write_bytes(raw)
        print(f"  💾 Saved → {path} ({len(raw)} bytes)")
        saved.append(str(path))
    return saved


def parse_sse_for_images(response, verbose=False):
    """Stream SSE; collect completed response payload for image extraction."""
    final_response = None
    event_count = 0
    text_parts = []

    for line in response.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type", "")
        event_count += 1

        if event_type == "response.output_text.delta":
            delta = event.get("delta", "")
            if delta:
                text_parts.append(delta)
                print(delta, end="", flush=True)
        elif event_type == "response.completed":
            final_response = event.get("response") or event
            usage = (event.get("response") or {}).get("usage") or event.get("usage")
            if usage:
                print(f"\n\n  📊 Usage: {json.dumps(usage, indent=2)}")
        elif verbose and event_type:
            print(f"\n  📡 {event_type}")

    if text_parts:
        print()
    return final_response, event_count


def call_via_tool(access_token, prompt, model, effort, email=None, stream=False, out_dir=DEFAULT_OUT_DIR, verbose=False):
    headers = build_headers(
        access_token,
        email=email,
        model=model,
        accept="text/event-stream" if stream else "application/json",
    )
    body = build_tool_body(prompt, model, effort=effort, stream=stream)

    print(f"\n  📤 POST {GROK_RESPONSES_URL}")
    print(f"     Mode:  image_generation tool (CLI free path)")
    print(f"     Model: {model} (effort: {effort})")
    print(f"     Prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")
    print()

    resp = requests.post(
        GROK_RESPONSES_URL,
        headers=headers,
        json=body,
        stream=stream,
        timeout=300,
    )

    if resp.status_code == 402:
        print(f"  💰 HTTP 402 — Credits exhausted")
        print(f"     {resp.text[:500]}")
        return None
    if resp.status_code != 200:
        print(f"  ✗ HTTP {resp.status_code}")
        print(f"     {resp.text[:1500]}")
        return None

    if stream:
        print("  💬 Stream:")
        data, event_count = parse_sse_for_images(resp, verbose=verbose)
        print(f"  📈 Events: {event_count}")
        if not data:
            print("  ✗ No completed response in stream")
            return None
    else:
        try:
            data = resp.json()
        except json.JSONDecodeError:
            print(f"  ✗ Non-JSON response: {resp.text[:500]}")
            return None

    usage = data.get("usage") if isinstance(data, dict) else None
    if usage:
        print(f"  📊 Usage: {json.dumps(usage, indent=2)}")

    images = extract_images_from_response(data)
    if not images and isinstance(data, dict):
        print("  ⚠ No image_generation_call found — dumping structure keys:")
        print(f"     top keys: {list(data.keys())}")
        out = data.get("output")
        if isinstance(out, list):
            for i, item in enumerate(out):
                if isinstance(item, dict):
                    preview = {k: (f"<{len(v)} chars>" if isinstance(v, str) and len(v) > 80 else v)
                               for k, v in item.items()}
                    print(f"     output[{i}] type={item.get('type')}: {json.dumps(preview)[:400]}")
        if verbose:
            dump = json.dumps(data)
            if len(dump) > 3000:
                dump = dump[:3000] + "..."
            print(f"  raw: {dump}")
        return data

    print(f"  ✓ Found {len(images)} image(s) in response")
    saved = save_b64_images(images, out_dir=out_dir)
    if saved:
        print(f"\n  ✓ Saved {len(saved)} file(s) to {out_dir}/")
    return data


# ─── Paid direct path: /v1/images/generations ─────────────────────────────────

def call_direct_images(access_token, prompt, email=None, out_dir=DEFAULT_OUT_DIR,
                       model="grok-imagine-image-quality", aspect_ratio="1:1", resolution="1k"):
    headers = build_headers(access_token, email=email, accept="application/json")
    body = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
        "response_format": "b64_json",
    }
    print(f"\n  📤 POST {XAI_IMAGES_URL}")
    print(f"     Mode:  direct Imagine API (needs SuperGrok/credits)")
    print(f"     Model: {model}")
    print()

    resp = requests.post(XAI_IMAGES_URL, headers=headers, json=body, timeout=180)
    if resp.status_code != 200:
        print(f"  ✗ HTTP {resp.status_code}")
        print(f"     {resp.text[:800]}")
        return None

    data = resp.json()
    items = data.get("data") or []
    b64_list = []
    for item in items:
        b64 = item.get("b64_json")
        if b64:
            b64_list.append(b64)
    if b64_list:
        save_b64_images(b64_list, out_dir=out_dir, prefix="imagine")
    return data


# ─── Misc ─────────────────────────────────────────────────────────────────────

def list_models(access_token, email=None):
    headers = build_headers(access_token, email=email, accept="application/json")
    print(f"\n  📤 GET {GROK_MODELS_URL}")
    resp = requests.get(GROK_MODELS_URL, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"  ✗ HTTP {resp.status_code}: {resp.text[:500]}")
        return
    print(json.dumps(resp.json(), indent=2)[:5000])


def check_billing(access_token, email=None):
    headers = build_headers(access_token, email=email, accept="application/json")
    print(f"\n  📤 GET {GROK_BILLING_URL}")
    resp = requests.get(GROK_BILLING_URL, headers=headers, timeout=30)
    if resp.status_code == 200:
        print(json.dumps(resp.json(), indent=2))
    else:
        print(f"  ✗ HTTP {resp.status_code}: {resp.text[:400]}")

    print(f"\n  📤 GET {GROK_USER_URL}")
    resp2 = requests.get(GROK_USER_URL, headers=headers, timeout=30)
    if resp2.status_code == 200:
        user = resp2.json()
        print(json.dumps(user, indent=2))
        print(f"\n  subscriptionTier={user.get('subscriptionTier')!r}  hasGrokCodeAccess={user.get('hasGrokCodeAccess')!r}")
    else:
        print(f"  ⚠ HTTP {resp2.status_code}: {resp2.text[:300]}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    global GROK_JSON_PATH
    parser = argparse.ArgumentParser(
        description="Grok CLI image gen via image_generation tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Use latest farm batch (default)
  python test_grok_image.py --prompt "A cat astronaut"

  # Use a specific accounts.json
  python test_grok_image.py --accounts results/batch_xxx/accounts.json --prompt "sunset"

  # Use account #5
  python test_grok_image.py --account 5 --prompt "cyberpunk city"

  # Stream mode
  python test_grok_image.py --stream --prompt "dragon"

  # Check billing/credits for an account
  python test_grok_image.py --billing --account 3
""",
    )
    parser.add_argument("--accounts", type=str, default=GROK_JSON_PATH,
                        help=f"Path to accounts.json (default: {DEFAULT_ACCOUNTS_PATH})")
    parser.add_argument("--account", type=int, default=0, help="Account index in the file")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL, help=f"Chat model for tool path (default: {DEFAULT_MODEL})")
    parser.add_argument("--effort", type=str, choices=EFFORT_LEVELS, default=DEFAULT_EFFORT)
    parser.add_argument("--prompt", type=str, default=DEFAULT_PROMPT)
    parser.add_argument("--out-dir", type=str, default=DEFAULT_OUT_DIR)
    parser.add_argument("--stream", action="store_true", help="SSE stream mode")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--direct", action="store_true", help="Use paid /v1/images/generations instead of tool")
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--billing", action="store_true")
    args = parser.parse_args()

    GROK_JSON_PATH = args.accounts

    print("Grok CLI — Image Generation (tool path)")
    print("=" * 60)
    print(f"  Token file: {GROK_JSON_PATH}")

    accounts = load_accounts(GROK_JSON_PATH)
    access_token, email = get_access_token(accounts, args.account, accounts_path=GROK_JSON_PATH)

    if args.billing:
        check_billing(access_token, email=email)
    elif args.list_models:
        list_models(access_token, email=email)
    elif args.direct:
        call_direct_images(access_token, prompt=args.prompt, email=email, out_dir=args.out_dir)
    else:
        call_via_tool(
            access_token,
            prompt=args.prompt,
            model=args.model,
            effort=args.effort,
            email=email,
            stream=args.stream,
            out_dir=args.out_dir,
            verbose=args.verbose,
        )

    print("\n" + "=" * 60)
    print("Done.")


if __name__ == "__main__":
    main()
