import json, secrets, string, time, sys, re, urllib.request, urllib.error

MAIL_TM_API = "https://api.mail.tm"

# 1. Create mail.tm account
local = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10))
email = f"{local}@emalupe.com"
password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))

print(f"[1] Creating mail.tm: {email}")
req = urllib.request.Request(
    f"{MAIL_TM_API}/accounts",
    data=json.dumps({"address": email, "password": password}).encode(),
    headers={"Content-Type": "application/json", "Accept": "application/json"},
    method="POST",
)
try:
    resp = urllib.request.urlopen(req, timeout=10)
    print(f"  Created: {resp.status}")
except urllib.error.HTTPError as e:
    print(f"  Error: {e.code} {e.read().decode()[:200]}")
    sys.exit(1)

req2 = urllib.request.Request(
    f"{MAIL_TM_API}/token",
    data=json.dumps({"address": email, "password": password}).encode(),
    headers={"Content-Type": "application/json", "Accept": "application/json"},
    method="POST",
)
resp2 = urllib.request.urlopen(req2, timeout=10)
mail_token = json.loads(resp2.read().decode()).get("token")
print(f"  Token OK")

# 2. Run farm.py with this email
# We need to pass the email to farm.py somehow. Let's use the env approach.
# Actually, let's just run farm.py with EMAIL_MODE override
import subprocess, os

env = os.environ.copy()
env["GROK_EMAIL_MODE"] = "mail_tm"  # This won't work yet — farm.py doesn't support it
# Instead, let's run the browser farm manually with this email

# Actually, let's just test the signup flow directly via curl_cffi
from curl_cffi import requests

proxy = "socks5://127.0.0.1:1080"
print(f"[2] Testing xAI signup with mail.tm email via WARP...")

# Check what xAI signup expects
r = requests.get("https://accounts.x.ai/sign-up",
    proxies={"https": proxy, "http": proxy},
    impersonate="chrome131",
    timeout=30)
print(f"  Signup page: {r.status_code} ({len(r.text)} bytes)")

# Look for the signup form action or API endpoint
# xAI uses a Next.js server action for signup
import re
# Find __NEXT_DATA__ or server action endpoints
next_data = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text)
if next_data:
    data = json.loads(next_data.group(1))
    print(f"  Next.js buildId: {data.get('buildId','?')[:20]}")
    # Look for server actions
    def find_actions(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if "action" in k.lower() or "api" in k.lower():
                    print(f"  Found: {path}.{k} = {str(v)[:80]}")
                find_actions(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                find_actions(v, f"{path}[{i}]")
    find_actions(data)

# Check if there's a Turnstile sitekey
turnstile = re.search(r'data-sitekey="([^"]+)"', r.text)
if turnstile:
    print(f"  Turnstile sitekey: {turnstile.group(1)}")

print(f"[3] Email ready: {email}")
print(f"[3] Mail.tm token: {mail_token[:30]}...")
print(f"\nTo test full signup, run farm.py with this email manually.")
print(f"Email for farm: {email}")
print(f"Password for farm: {mail_token}")  # Use mail token as password placeholder
