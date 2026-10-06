#!/usr/bin/env python3
"""
Multi-provider temporary email module.

Supports round-robin rotation across multiple temp mail services:
  1. mail.tm       — API-based, reliable, domains rotate
  2. guerrillamail — API-based, well-known
  3. tempmail.io   — API-based
  4. 1secmail      — API-based, simple

Each provider implements:
  - create_account() → {email, password, token, provider}
  - read_otp(token, timeout) → OTP code string or None

Round-robin strategy:
  - Farm starts with 8 custom domains (GROK_EMAIL_DOMAINS)
  - If BFS detected on a domain, switch to next temp mail provider
  - Rotate through all temp mail providers before retrying custom domains
"""
import json
import secrets
import string
import threading
import time
import urllib.error
import urllib.request
import re
from typing import Any

# ── Provider interface ──────────────────────────────────────────────────────

class TempMailProvider:
    """Base interface for temp mail providers."""
    name: str = "base"

    def create_account(self) -> dict[str, Any] | None:
        """Create a new temp mailbox. Returns {email, password, token, provider}."""
        raise NotImplementedError

    def read_otp(self, token: str, timeout: int = 180) -> str | None:
        """Poll inbox for xAI confirmation code (XXX-XXX format)."""
        raise NotImplementedError


# ── Mail.tm ──────────────────────────────────────────────────────────────────

class MailTmProvider(TempMailProvider):
    name = "mail_tm"
    API = "https://api.mail.tm"
    _domains: list[str] = []
    _domains_at = 0.0
    _lock = threading.Lock()

    def _fetch_domains(self) -> list[str]:
        with self._lock:
            now = time.time()
            if self._domains and (now - self._domains_at) < 3600:
                return self._domains
            try:
                req = urllib.request.Request(
                    f"{self.API}/domains",
                    headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    self._domains = [d["domain"] for d in data if d.get("isActive")]
                elif isinstance(data, dict):
                    self._domains = [d["domain"] for d in data.get("hydra:member", []) if d.get("isActive")]
                self._domains_at = now
            except Exception as e:
                print(f"[mail.tm] domain fetch failed: {e}", flush=True)
            return self._domains

    def create_account(self) -> dict[str, Any] | None:
        domains = self._fetch_domains()
        if not domains:
            return None
        local = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10))
        email = f"{local}@{domains[0]}"
        password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
        try:
            req = urllib.request.Request(
                f"{self.API}/accounts",
                data=json.dumps({"address": email, "password": password}).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status not in (200, 201):
                    return None
        except Exception:
            return None
        try:
            req2 = urllib.request.Request(
                f"{self.API}/token",
                data=json.dumps({"address": email, "password": password}).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                token = json.loads(resp2.read().decode()).get("token", "")
        except Exception:
            return None
        if not token:
            return None
        return {"email": email, "password": password, "token": token, "provider": self.name}

    def read_otp(self, token: str, timeout: int = 180) -> str | None:
        code_re = re.compile(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b")
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            try:
                req = urllib.request.Request(
                    f"{self.API}/messages?page=1",
                    headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                messages = data if isinstance(data, list) else data.get("hydra:member", [])
                for msg in messages:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    subject = msg.get("subject", "")
                    m = code_re.search(subject)
                    if m:
                        code = m.group(1).upper()
                        left, right = code.split("-", 1)
                        if not (re.fullmatch(r"[A-Z]+", left) and re.fullmatch(r"\d+", right)):
                            return code
                    # Read full message
                    try:
                        req2 = urllib.request.Request(
                            f"{self.API}/messages/{mid}",
                            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                        )
                        with urllib.request.urlopen(req2, timeout=10) as resp2:
                            full = json.loads(resp2.read().decode("utf-8"))
                        text = full.get("text", "") or full.get("html", "") or ""
                        if isinstance(text, list):
                            text = " ".join(str(t) for t in text)
                        m2 = code_re.search(text)
                        if m2:
                            code = m2.group(1).upper()
                            left, right = code.split("-", 1)
                            if not (re.fullmatch(r"[A-Z]+", left) and re.fullmatch(r"\d+", right)):
                                return code
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(3.0)
        return None


# ── GuerrillaMail ────────────────────────────────────────────────────────────

class GuerrillaMailProvider(TempMailProvider):
    name = "guerrillamail"
    API = "https://api.guerrillamail.com/ajax.php"

    def create_account(self) -> dict[str, Any] | None:
        # GuerrillaMail uses session-based email — get a random address
        try:
            params = "f=getemailaddress&lang=en"
            req = urllib.request.Request(
                f"{self.API}?{params}",
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            email = data.get("email_addr", "")
            sid = data.get("sid_token", "")
            if not email or not sid:
                return None
            # guerrillamail doesn't use passwords — sid_token is the auth
            return {
                "email": email,
                "password": sid,  # use sid as password placeholder
                "token": sid,
                "provider": self.name,
            }
        except Exception as e:
            print(f"[guerrillamail] create failed: {e}", flush=True)
            return None

    def read_otp(self, token: str, timeout: int = 180) -> str | None:
        code_re = re.compile(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b")
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            try:
                params = f"f=get_email_list&offset=0&sid_token={token}"
                req = urllib.request.Request(
                    f"{self.API}?{params}",
                    headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                for msg in data.get("list", []):
                    mid = str(msg.get("mail_id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    subject = msg.get("mail_subject", "")
                    m = code_re.search(subject)
                    if m:
                        code = m.group(1).upper()
                        left, right = code.split("-", 1)
                        if not (re.fullmatch(r"[A-Z]+", left) and re.fullmatch(r"\d+", right)):
                            return code
                    # Read full message
                    try:
                        params2 = f"f=fetch_email&email_id={mid}&sid_token={token}"
                        req2 = urllib.request.Request(
                            f"{self.API}?{params2}",
                            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                        )
                        with urllib.request.urlopen(req2, timeout=10) as resp2:
                            full = json.loads(resp2.read().decode("utf-8"))
                        body = full.get("mail_body", "")
                        m2 = code_re.search(body)
                        if m2:
                            code = m2.group(1).upper()
                            left, right = code.split("-", 1)
                            if not (re.fullmatch(r"[A-Z]+", left) and re.fullmatch(r"\d+", right)):
                                return code
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(3.0)
        return None


# ── 1secmail ─────────────────────────────────────────────────────────────────

class OneSecMailProvider(TempMailProvider):
    name = "1secmail"
    API = "https://www.1secmail.com/api/v1/"
    DOMAINS = ["1secmail.com", "1secmail.net", "1secmail.org", "wwjmp.com",
               "esiix.com", "xojxe.com", "yoggm.com"]

    def create_account(self) -> dict[str, Any] | None:
        local = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10))
        domain = secrets.choice(self.DOMAINS)
        email = f"{local}@{domain}"
        # 1secmail doesn't require account creation — just use the address
        # The "token" is the login+domain for polling
        token = f"{local}@{domain}"
        return {
            "email": email,
            "password": "",  # no password needed
            "token": token,
            "provider": self.name,
        }

    def read_otp(self, token: str, timeout: int = 180) -> str | None:
        code_re = re.compile(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b")
        login, domain = token.split("@", 1) if "@" in token else (token, "")
        deadline = time.time() + timeout
        seen: set[str] = set()
        while time.time() < deadline:
            try:
                url = f"{self.API}?action=getMessages&login={login}&domain={domain}"
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                for msg in data:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    subject = msg.get("subject", "")
                    m = code_re.search(subject)
                    if m:
                        code = m.group(1).upper()
                        left, right = code.split("-", 1)
                        if not (re.fullmatch(r"[A-Z]+", left) and re.fullmatch(r"\d+", right)):
                            return code
                    # Read full message
                    try:
                        url2 = f"{self.API}?action=readMessage&login={login}&domain={domain}&id={mid}"
                        req2 = urllib.request.Request(url2, headers={"Accept": "application/json"})
                        with urllib.request.urlopen(req2, timeout=10) as resp2:
                            full = json.loads(resp2.read().decode("utf-8"))
                        body = full.get("body", "") or full.get("textBody", "")
                        m2 = code_re.search(body)
                        if m2:
                            code = m2.group(1).upper()
                            left, right = code.split("-", 1)
                            if not (re.fullmatch(r"[A-Z]+", left) and re.fullmatch(r"\d+", right)):
                                return code
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(3.0)
        return None


# ── Round-robin manager ──────────────────────────────────────────────────────

class TempMailRotator:
    """
    Round-robin temp mail provider rotation.

    Strategy:
      1. Start with custom domains (GROK_EMAIL_DOMAINS) — best for avoiding BFS
      2. If BFS detected, rotate to temp mail providers
      3. Cycle through all providers in order
    """

    def __init__(self, custom_domains: list[str] | None = None):
        self.custom_domains = custom_domains or []
        self.providers: list[TempMailProvider] = [
            MailTmProvider(),
            GuerrillaMailProvider(),
            OneSecMailProvider(),
        ]
        self._provider_index = 0
        self._bfs_streak = 0
        self._lock = threading.Lock()

    def get_next_provider(self) -> TempMailProvider | None:
        """Get the next temp mail provider in round-robin order."""
        with self._lock:
            if not self.providers:
                return None
            provider = self.providers[self._provider_index % len(self.providers)]
            self._provider_index += 1
            return provider

    def report_bfs(self) -> None:
        """Report a BFS detection — triggers switch to temp mail."""
        with self._lock:
            self._bfs_streak += 1

    def report_clean(self) -> None:
        """Report a clean (non-BFS) account — resets BFS streak."""
        with self._lock:
            self._bfs_streak = 0

    @property
    def should_use_temp_mail(self) -> bool:
        """True if we should switch to temp mail (BFS streak detected)."""
        return self._bfs_streak >= 2

    def create_account(self) -> dict[str, Any] | None:
        """Create account using current strategy."""
        provider = self.get_next_provider()
        if not provider:
            return None
        return provider.create_account()

    def read_otp(self, token: str, provider: str, timeout: int = 180) -> str | None:
        """Read OTP using the specified provider."""
        for p in self.providers:
            if p.name == provider:
                return p.read_otp(token, timeout)
        return None


if __name__ == "__main__":
    # Test all providers
    rotator = TempMailRotator()
    for p in rotator.providers:
        print(f"\nTesting {p.name}...")
        acct = p.create_account()
        if acct:
            print(f"  Email: {acct['email']}")
            print(f"  Provider: {acct['provider']}")
            print(f"  Token: {acct['token'][:30]}...")
        else:
            print(f"  FAILED")
