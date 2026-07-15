# -*- coding: utf-8 -*-
"""XConsoleAuthClient — HTTP-only x.ai account signup/login client.

Reconstructs the accounts.x.ai signup protocol without a browser:
  GET  console.x.ai/home                              -> seeds cf_clearance cookie
  POST AuthManagement/CreateEmailValidationCode       (gRPC-web)  emails OTP code
  POST AuthManagement/VerifyEmailValidationCode       (gRPC-web)  validates OTP
  POST AuthManagement/ValidatePassword                (gRPC-web)  password check
  POST accounts.x.ai/sign-up  (Next.js server action) creates account + session

Uses curl_cffi with browser fingerprint impersonation to bypass Cloudflare TLS checks.
The next-action ID and router-state-tree are scraped live from the signup page JS.
"""
from __future__ import annotations

import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from . import grpcweb

# ── Constants ───────────────────────────────────────────────────────────────

ACCOUNTS_ORIGIN = "https://accounts.x.ai"
HOME_URL = "https://console.x.ai/home"
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
SIGNIN_URL = "https://accounts.x.ai/sign-in?redirect=grok-com"

GRPC_SERVICE = "auth_mgmt.AuthManagement"
RPC_CREATE_CODE = f"{ACCOUNTS_ORIGIN}/{GRPC_SERVICE}/CreateEmailValidationCode"
RPC_VERIFY_CODE = f"{ACCOUNTS_ORIGIN}/{GRPC_SERVICE}/VerifyEmailValidationCode"
RPC_VALIDATE_PW = f"{ACCOUNTS_ORIGIN}/{GRPC_SERVICE}/ValidatePassword"

TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CONNECT_ES_VERSION = "connect-es/2.1.1"


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class GrpcResult:
    ok: bool
    http_status: int
    grpc_status: Optional[int]
    messages: List[List[Dict[str, Any]]] = field(default_factory=list)
    trailers: Dict[str, str] = field(default_factory=dict)
    raw: bytes = b""

    @property
    def first_message(self) -> List[Dict[str, Any]]:
        return self.messages[0] if self.messages else []


@dataclass
class SignupResult:
    ok: bool
    http_status: int
    set_cookies: List[str] = field(default_factory=list)
    rsc_body: str = ""


# ── Client ──────────────────────────────────────────────────────────────────

class XConsoleAuthClient:
    """HTTP-only x.ai account registration client."""

    def __init__(
        self,
        *,
        impersonate: str = "chrome131",
        debug: bool = False,
        timeout: float = 40.0,
        proxy: Optional[str] = None,
        signup_url: Optional[str] = None,
    ):
        self.debug = debug
        self.timeout = timeout
        self.signup_url = signup_url or SIGNUP_URL

        try:
            from curl_cffi import requests as creq
        except ImportError as exc:
            raise RuntimeError(
                "curl_cffi is required. Install: pip install curl_cffi"
            ) from exc

        kwargs: Dict[str, Any] = {"impersonate": impersonate}
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        self._s = creq.Session(**kwargs)

        # Scraped per-session
        self._next_action_id: Optional[str] = None
        self._next_router_state_tree: Optional[str] = None
        self._last_rsc_body: str = ""
        self._last_create_set_cookies: List[str] = []
        self.turnstile_sitekey: Optional[str] = None
        self._cf_user_agent: Optional[str] = None  # UA from cf_clearance solve

    # ── transport ───────────────────────────────────────────────────────────

    @property
    def session(self):
        """Expose underlying curl_cffi session for reuse by OAuth client."""
        return self._s

    @property
    def cookies(self):
        return self._s.cookies

    def _base_headers(self) -> Dict[str, str]:
        return {
            "user-agent": self._cf_user_agent or USER_AGENT,
            "accept-language": "en-US,en;q=0.9",
        }

    def _grpc_headers(self, referer: str) -> Dict[str, str]:
        h = self._base_headers()
        h.update({
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": CONNECT_ES_VERSION,
            "accept": "*/*",
            "origin": ACCOUNTS_ORIGIN,
            "referer": referer,
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        })
        return h

    # ── entry point ─────────────────────────────────────────────────────────

    def visit_home(self) -> int:
        """GET console.x.ai/home to seed Cloudflare cookies.

        curl_cffi's TLS impersonation is sufficient for accounts.x.ai —
        no cf_clearance needed (the server action works without it).
        """
        h = self._base_headers()
        h.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-fetch-site": "none",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "upgrade-insecure-requests": "1",
        })
        resp = self._s.get(HOME_URL, headers=h, timeout=self.timeout)
        return resp.status_code

    def _fetch_cf_clearance(self) -> None:
        """Fetch cf_clearance cookie via Boterdrop-Solver /clearance endpoint."""
        import os
        import time

        solver_url = (os.environ.get("SOLVER_URL") or "").rstrip("/")
        if not solver_url:
            if self.debug:
                print("  [cf_clearance] SOLVER_URL not set, skipping")
            return

        try:
            import requests as _req

            # Submit clearance task
            resp = _req.get(
                f"{solver_url}/clearance",
                params={"url": self.signup_url},
                timeout=15,
            )
            if resp.status_code not in (200, 202):
                if self.debug:
                    print(f"  [cf_clearance] Boterdrop submit failed: HTTP {resp.status_code}")
                return

            data = resp.json()
            task_id = data.get("task_id") or data.get("id")
            if not task_id:
                if self.debug:
                    print("  [cf_clearance] No task_id in response")
                return

            if self.debug:
                print(f"  [cf_clearance] Boterdrop task: {task_id}")

            # Poll for result
            start = time.time()
            while time.time() - start < 60:
                time.sleep(3)
                r = _req.get(
                    f"{solver_url}/result",
                    params={"id": task_id},
                    timeout=15,
                )
                if r.status_code != 200:
                    continue
                result = r.json()
                status = result.get("status", "")
                if status in ("success", "done", "ready"):
                    cf = result.get("cf_clearance") or ""
                    ua = result.get("user_agent") or ""
                    if cf:
                        # Inject cf_clearance cookie into the session
                        for domain in (".x.ai", "accounts.x.ai"):
                            try:
                                self._s.cookies.set("cf_clearance", cf, domain=domain)
                            except Exception:
                                pass
                        if self.debug:
                            print(f"  [cf_clearance] Obtained (len={len(cf)})")
                        # Also update user-agent if provided (must match the one CF issued)
                        if ua:
                            self._cf_user_agent = ua
                    else:
                        if self.debug:
                            print("  [cf_clearance] No cf_clearance in result")
                    return
                if status in ("error", "failed"):
                    if self.debug:
                        print(f"  [cf_clearance] Boterdrop failed: {result.get('message', '')}")
                    return
        except Exception as e:
            if self.debug:
                print(f"  [cf_clearance] Error: {str(e)[:80]}")

    def load_signup_page(self) -> int:
        """GET the signup page and scrape next-action ID + Turnstile sitekey."""
        h = self._base_headers()
        h.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "referer": "https://console.x.ai/",
        })
        resp = self._s.get(self.signup_url, headers=h, timeout=self.timeout)
        html = resp.text

        # Scrape Next.js build-specific values
        self._scrape_rsc_payload(html)
        self.turnstile_sitekey = self._scrape_turnstile_sitekey(html) or TURNSTILE_SITEKEY

        if self.debug:
            print(f"  [scrape] next-action={self._next_action_id[:16]}... "
                  f"({len(self._next_action_id or '')} chars)")
            print(f"  [scrape] turnstile_sitekey={self.turnstile_sitekey}")

        return resp.status_code

    # ── dynamic scraping ────────────────────────────────────────────────────

    @staticmethod
    def _scrape_turnstile_sitekey(html: str) -> Optional[str]:
        if not html:
            return None
        patterns = (
            r'sitekey["\']\s*[:=]\s*["\'](0x4[0-9A-Za-z_-]{10,})["\']',
            r'data-sitekey=["\'](0x4[0-9A-Za-z_-]{10,})["\']',
            r'(0x4AAAAA[0-9A-Za-z_-]{8,})',
        )
        for pat in patterns:
            m = re.search(pat, html, flags=re.IGNORECASE)
            if m:
                return m.group(1)
        return None

    _RSC_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')

    def _scrape_rsc_payload(self, html: str) -> None:
        """Extract next-action and next-router-state-tree from the live page."""
        rsc_segments = self._RSC_PUSH_RE.findall(html)

        # Extract router state tree
        router_tree = None
        for seg in rsc_segments:
            unescaped = seg.replace('\\"', '"')
            m = re.search(r'"f":\[(\[.*?\])', unescaped)
            if m:
                flight_seg = m.group(1)
                if flight_seg.startswith('[["",{"children"'):
                    depth = 0
                    tree_end = 0
                    for i, ch in enumerate(flight_seg):
                        if ch == '[':
                            depth += 1
                        elif ch == ']':
                            depth -= 1
                            if depth == 0:
                                tree_end = i + 1
                                break
                    if tree_end > 0:
                        tree_json = flight_seg[:tree_end]
                        try:
                            parsed = json.loads(tree_json)
                            if isinstance(parsed, list) and len(parsed) >= 1:
                                router_tree = json.dumps(parsed[0], separators=(",", ":"))
                        except (json.JSONDecodeError, IndexError):
                            pass

        if router_tree is None:
            router_tree = json.dumps(
                ["", {"children": ["(app)", {"children": ["(auth)", {"children":
                  ["sign-up", {"children": ["__PAGE__?{\"redirect\":\"grok-com\"}", {}]}]}]}]}],
                separators=(",", ":"),
            )

        self._next_router_state_tree = quote(router_tree, safe="")

        # Extract action ID from JS chunks
        self._next_action_id = self._scrape_action_id(html)

    def _scrape_action_id(self, html: str) -> str:
        """Find the Next.js server action ID from JS chunks.

        Prefer createServerReference("…","default") in the signup chunk that
        contains createUserAndSessionRequest — bare 42-char hex scans can pick
        the wrong id under concurrent deploys.
        """
        js_urls = list(set(re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', html)))

        def _fetch_and_search(path: str) -> Tuple[Optional[str], int]:
            """Return (action_id, priority). Higher priority wins."""
            try:
                full = f"{ACCOUNTS_ORIGIN}{path}"
                resp = self._s.get(full, headers=self._base_headers(), timeout=20)
                text = resp.text
            except Exception:
                return (None, 0)

            is_signup = any(
                kw in text
                for kw in (
                    "createUserAndSessionRequest",
                    "emailValidationCode",
                    "clearTextPassword",
                )
            )
            # Best: named server reference used by the signup mutation
            m = re.search(
                r'createServerReference\)\("([a-f0-9]{40,44})"[^)]{0,200}"default"',
                text,
            )
            if m and is_signup:
                return (m.group(1), 100)
            if m:
                return (m.group(1), 40)

            m = re.search(r'createServerReference\)\("([a-f0-9]{40,44})"', text)
            if m and is_signup:
                return (m.group(1), 80)
            if m:
                return (m.group(1), 20)

            # Last resort: any 42-char hex in a signup-related chunk
            if is_signup:
                hashes = re.findall(r'"([a-f0-9]{42})"', text)
                if hashes:
                    return (hashes[0], 10)
            return (None, 0)

        best_hash: Optional[str] = None
        best_prio = 0

        with ThreadPoolExecutor(max_workers=min(8, len(js_urls) or 1)) as ex:
            futures = {ex.submit(_fetch_and_search, url): url for url in js_urls}
            for f in as_completed(futures):
                h, prio = f.result()
                if h is None:
                    continue
                if prio > best_prio:
                    best_prio = prio
                    best_hash = h

        if best_hash is None:
            raise RuntimeError(
                "Could not find server action ID in JS chunks. "
                "The page structure may have changed."
            )
        return best_hash

    def refresh_signup_action(self) -> str:
        """Re-scrape next-action + router tree (call after long OTP waits)."""
        status = self.load_signup_page()
        if self.debug:
            print(
                f"  [scrape] refreshed next-action="
                f"{(self._next_action_id or '')[:16]}... (page_http={status})"
            )
        if not self._next_action_id:
            raise RuntimeError("Failed to refresh signup server action ID")
        return self._next_action_id

    # ── gRPC-web RPCs ───────────────────────────────────────────────────────

    def _grpc_call(self, url: str, fields: List[Tuple[int, str]], referer: str) -> GrpcResult:
        message = grpcweb.encode_message(fields)
        body = grpcweb.frame_request(message)
        headers = self._grpc_headers(referer)
        resp = self._s.post(url, headers=headers, data=body, timeout=self.timeout)

        raw = resp.content
        if not raw:
            return GrpcResult(ok=False, http_status=resp.status_code,
                              grpc_status=None, messages=[], trailers={}, raw=raw)

        parsed = grpcweb.parse_response(raw)
        ok = resp.status_code == 200 and parsed["grpc_status"] == 0
        return GrpcResult(
            ok=ok, http_status=resp.status_code, grpc_status=parsed["grpc_status"],
            messages=parsed["messages"], trailers=parsed["trailers"], raw=raw,
        )

    def create_email_validation_code(self, email: str) -> GrpcResult:
        return self._grpc_call(RPC_CREATE_CODE, [(1, email)], self.signup_url)

    def verify_email_validation_code(self, email: str, code: str) -> GrpcResult:
        return self._grpc_call(RPC_VERIFY_CODE, [(1, email), (2, code)], self.signup_url)

    def validate_password(self, email: str, password: str) -> GrpcResult:
        # Field numbers 4 and 5 (observed in capture, not 1/2)
        return self._grpc_call(RPC_VALIDATE_PW, [(4, email), (5, password)], self.signup_url)

    # ── account creation ────────────────────────────────────────────────────

    @staticmethod
    def _flight_escape_string(value: str) -> str:
        """Escape a string for Next.js React Flight server-action bodies.

        Flight treats model strings that start with ``$`` as special references
        (e.g. ``$undefined``, ``$T``). A real password like ``$ExamplePass1`` must
        be sent as ``$$ExamplePass1`` so the decoder yields a literal leading ``$``.
        """
        if value.startswith("$"):
            return "$" + value
        return value

    def create_account(
        self,
        *,
        email: str,
        given_name: str,
        family_name: str,
        password: str,
        email_validation_code: str,
        turnstile_token: str,
        castle_request_token: str = "",
        conversion_id: Optional[str] = None,
        refresh_action: bool = True,
    ) -> SignupResult:
        # Action IDs rotate with Next deploys; OTP waits are long enough that a
        # stale id scraped at page load becomes "Server action not found" (404).
        if refresh_action or not self._next_action_id:
            try:
                self.refresh_signup_action()
            except Exception as e:
                if self.debug:
                    print(f"  [create_account] action refresh failed: {e}")

        create_req = {
            "email": email,
            "givenName": given_name,
            "familyName": family_name,
            # Escape for React Flight; account password remains the unescaped value.
            "clearTextPassword": self._flight_escape_string(password),
            # Grok signup uses TOS_ACCEPTED_VERSION = 1 (see accounts.x.ai JS).
            "tosAcceptedVersion": 1,
        }
        # Server action takes a single object arg (useMutation options stay client-side).
        args = [
            {
                "emailValidationCode": email_validation_code,
                "createUserAndSessionRequest": create_req,
                "turnstileToken": turnstile_token,
                "conversionId": conversion_id or str(uuid.uuid4()),
                "castleRequestToken": castle_request_token,
            },
        ]
        body = json.dumps(args, separators=(",", ":")).encode("utf-8")

        def _post_once() -> SignupResult:
            h = self._base_headers()
            h.update({
                "accept": "text/x-component",
                "content-type": "text/plain;charset=UTF-8",
                "next-action": self._next_action_id,
                "next-router-state-tree": self._next_router_state_tree,
                "origin": ACCOUNTS_ORIGIN,
                "referer": self.signup_url,
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
            })
            resp = self._s.post(self.signup_url, headers=h, data=body, timeout=self.timeout)
            rsc_body = resp.text
            self._last_rsc_body = rsc_body
            self._last_create_set_cookies = list(resp.headers.get_list("set-cookie") or [])
            ok = resp.status_code == 200 and not self._is_hard_error(rsc_body)
            if self.debug:
                print(
                    f"  [create_account] HTTP {resp.status_code} ok={ok} "
                    f"action={(self._next_action_id or '')[:16]}... "
                    f"body_len={len(rsc_body)} set_cookies={len(self._last_create_set_cookies)}"
                )
                if not ok:
                    print(f"  [create_account] body: {rsc_body[:500]!r}")
            return SignupResult(
                ok=ok,
                http_status=resp.status_code,
                set_cookies=self._last_create_set_cookies,
                rsc_body=rsc_body,
            )

        result = _post_once()
        # Retry only on missing/rotated server action (not business-logic 500 digests)
        body_l = (result.rsc_body or "").lower()
        if result.http_status == 404 or "server action not found" in body_l:
            if self.debug:
                print("  [create_account] retrying with fresh action id...")
            try:
                self.refresh_signup_action()
            except Exception as e:
                if self.debug:
                    print(f"  [create_account] retry refresh failed: {e}")
                return result
            result = _post_once()

        return result

    @staticmethod
    def _is_hard_error(rsc_body: str) -> bool:
        """Check for explicit signup failure codes in RSC body."""
        if not rsc_body:
            return False
        text_l = rsc_body.lower()

        # Next.js server-action hard error flight
        if re.search(r"(?m)^\d+:E\{", rsc_body):
            return True

        error_codes = (
            "turnstile_failed", "account_signup_error", "rate_limited",
            "validation_error", "invalid_verification_code", "email_already_in_use",
            "user_already_exists", "invalid-credentials", "account_email_domain_rejected",
            "form_invalid_disposable_email", "account_email_malformed",
        )
        return any(code in text_l for code in error_codes)

    # ── SSO token extraction ────────────────────────────────────────────────

    def fetch_sso_token(self, *, retries: int = 4) -> Optional[str]:
        """Harvest the SSO session JWT after account creation."""
        # 1. From Set-Cookie headers
        token = self._parse_sso_from_cookies(self._last_create_set_cookies)
        if token:
            if self.debug:
                print("  [sso] found in Set-Cookie")
            return token

        # 2. From RSC body
        if self._last_rsc_body:
            token = self._parse_sso_from_text(self._last_rsc_body)
            if token:
                if self.debug:
                    print("  [sso] found in RSC body")
                return token

        # 3. Follow redirect hops
        for url in (
            "https://auth.x.ai/set-cookie",
            "https://auth.grokusercontent.com/set-cookie",
            "https://grok.com/",
            f"{ACCOUNTS_ORIGIN}/",
        ):
            token = self._fetch_sso_via_url(url)
            if token:
                return token

        # 4. From cookie jar
        token = self._read_sso_from_jar()
        if token:
            if self.debug:
                print("  [sso] found in cookie jar")

        return token

    def _fetch_sso_via_url(self, url: str) -> Optional[str]:
        try:
            h = self._base_headers()
            h.update({
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
                "referer": ACCOUNTS_ORIGIN + "/",
            })
            resp = self._s.get(url, headers=h, timeout=self.timeout, allow_redirects=True)
            set_cookies = resp.headers.get_list("set-cookie") or []
            token = self._parse_sso_from_cookies(set_cookies)
            if token:
                return token
            token = self._parse_sso_from_text(resp.text)
            if token:
                return token
        except Exception:
            pass
        return self._read_sso_from_jar()

    @staticmethod
    def _parse_sso_from_cookies(set_cookies: List[str]) -> Optional[str]:
        for sc in set_cookies or []:
            m = re.search(r'\bsso=([^\s;]+)', sc)
            if m and m.group(1).startswith("eyJ"):
                return m.group(1)
        return None

    @staticmethod
    def _parse_sso_from_text(text: str) -> Optional[str]:
        # Prefer explicit sso= cookie assignment
        m = re.search(r'sso=eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+', text)
        if m:
            return m.group(0).split("=", 1)[1]
        # Accept session JWTs only (CreateSession shape has "session_id").
        # RSC bodies also embed unrelated eyJ... blobs (e.g. config.token wrappers)
        # that must not be treated as SSO cookies.
        for m in re.finditer(r'eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+', text or ""):
            val = m.group(0)
            try:
                # JWT payload is middle segment, base64url
                import base64

                pad = "=" * (-len(val.split(".")[1]) % 4)
                payload = base64.urlsafe_b64decode(val.split(".")[1] + pad)
                if b"session_id" in payload:
                    return val
            except Exception:
                continue
        return None

    def _read_sso_from_jar(self) -> Optional[str]:
        try:
            for domain in (".grok.com", "grok.com", ".x.ai", "accounts.x.ai"):
                val = self._s.cookies.get("sso", domain=domain)
                if val and val.startswith("eyJ"):
                    return str(val)
        except Exception:
            pass
        try:
            val = self._s.cookies.get("sso")
            if val and val.startswith("eyJ"):
                return str(val)
        except Exception:
            pass
        return None

    def set_sso_cookie(self, jwt_token: str) -> None:
        if not jwt_token:
            return
        for domain in ("accounts.x.ai", ".x.ai", "auth.x.ai", ".grok.com", "grok.com"):
            try:
                self._s.cookies.set("sso", jwt_token, domain=domain)
            except Exception:
                pass

    # ── fallback: password login via CreateSession ──────────────────────────

    def obtain_session_via_password(
        self,
        *,
        email: str,
        password: str,
        turnstile_token: str,
        referer: Optional[str] = None,
    ) -> Optional[str]:
        """Fallback login via CreateSession gRPC-web."""
        from .oauth import encode_create_session_request

        ref = referer or SIGNIN_URL
        body = encode_create_session_request(
            email, password, turnstile_token=turnstile_token, castle_request_token="",
        )
        framed = grpcweb.frame_request(body)
        headers = self._grpc_headers(ref)

        try:
            resp = self._s.post(
                f"{ACCOUNTS_ORIGIN}/{GRPC_SERVICE}/CreateSession",
                headers=headers, data=framed, timeout=self.timeout,
            )
        except Exception:
            return None

        if not resp.content:
            return None

        try:
            parsed = grpcweb.parse_response(resp.content)
        except Exception:
            return None

        fields = parsed["messages"][0] if parsed.get("messages") else []
        for f in fields:
            if f.get("type") == "string":
                val = str(f.get("value") or "")
                if val.startswith("eyJ") and val.count(".") >= 2:
                    self.set_sso_cookie(val)
                    if self.debug:
                        print(f"  [sso] CreateSession OK jwt={val[:24]}...")
                    return val

        return self._read_sso_from_jar()

    # ── cleanup ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        try:
            self._s.close()
        except Exception:
            pass
