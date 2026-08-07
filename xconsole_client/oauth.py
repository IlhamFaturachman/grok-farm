# -*- coding: utf-8 -*-
"""ProtocolOAuthClient — HTTP-only OAuth PKCE flow for x.ai/Grok.

After account signup (or with email/password), this module:
  1. Starts OAuth PKCE against auth.x.ai
  2. Uses SSO cookie from signup to establish session
  3. Calls CreateCookieSetterLink (gRPC-web) to mint cross-domain cookies
  4. Follows redirect chain to capture the authorization code
  5. Returns {code, verifier} for token exchange

CreateSessionRequest wire layout (reverse-engineered):

  field 1  Credentials {
      field 1  EmailAndPassword { email=1, clearTextPassword=2 }
  }
  field 4  AntiAbuseToken {
      field 1  turnstileToken
      field 2  castleRequestToken (may be empty)
  }
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

from . import grpcweb
from .client import ACCOUNTS_ORIGIN, SIGNIN_URL, CONNECT_ES_VERSION

# ── Constants ───────────────────────────────────────────────────────────────

TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
CREATE_SESSION_RPC = f"{ACCOUNTS_ORIGIN}/auth_mgmt.AuthManagement/CreateSession"
CREATE_COOKIE_SETTER_RPC = f"{ACCOUNTS_ORIGIN}/auth_mgmt.AuthManagement/CreateCookieSetterLink"
AUTHORIZE_URL = "https://auth.x.ai/oauth2/authorize"
CONSENT_URL = f"{ACCOUNTS_ORIGIN}/oauth2/consent"

# OAuth defaults (override via env — same knobs as farm.py, no import farm)
DEFAULT_SCOPES = (
    os.environ.get("XAI_SCOPE")
    or "openid profile email offline_access grok-cli:access api:access"
).strip()
DEFAULT_REFERRER = (os.environ.get("XAI_REFERRER") or "grok-build").strip() or "grok-build"
DEFAULT_PLAN = (os.environ.get("XAI_PLAN") or "generic").strip() or "generic"

# ── Protobuf encoding ───────────────────────────────────────────────────────

def encode_create_session_request(
    email: str,
    password: str,
    *,
    turnstile_token: str,
    castle_request_token: str = "",
) -> bytes:
    """Encode CreateSessionRequest protobuf body."""
    email_pw = grpcweb.encode_string(1, email) + grpcweb.encode_string(2, password)
    credentials = grpcweb.encode_bytes(1, email_pw)
    req = grpcweb.encode_bytes(1, credentials)
    anti = grpcweb.encode_string(1, turnstile_token)
    anti += grpcweb.encode_string(2, castle_request_token)
    req += grpcweb.encode_bytes(4, anti)
    return req


# ── PKCE helpers ────────────────────────────────────────────────────────────

def generate_pkce_pair() -> tuple[str, str]:
    raw = secrets.token_bytes(96)
    verifier = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ── OAuth client ────────────────────────────────────────────────────────────

class ProtocolOAuthClient:
    """HTTP-only OAuth PKCE client using curl_cffi."""

    def __init__(
        self,
        session,
        *,
        client_id: str = "b1a00492-073a-47ea-816f-4c329264a828",
        redirect_host: str = "127.0.0.1",
        redirect_port: int = 56121,
        debug: bool = False,
    ):
        self._s = session
        self.client_id = client_id
        self.redirect_uri = f"http://{redirect_host}:{redirect_port}/callback"
        self.debug = debug

    def _log(self, msg: str) -> None:
        if self.debug:
            print(f"  [oauth] {msg}")

    def _get(self, url: str, allow_redirects: bool = True):
        h = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "upgrade-insecure-requests": "1",
        }
        return self._s.get(url, headers=h, allow_redirects=allow_redirects, timeout=45)

    def _grpc_headers(self, referer: str) -> Dict[str, str]:
        return {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": CONNECT_ES_VERSION,
            "accept": "*/*",
            "origin": ACCOUNTS_ORIGIN,
            "referer": referer,
        }

    # ── cookie setter ───────────────────────────────────────────────────────

    def create_cookie_setter_link(self, success_url: str, referer: str) -> Optional[str]:
        """Call CreateCookieSetterLink; returns cookie_setter_url."""
        msg = grpcweb.encode_string(1, success_url) + grpcweb.encode_string(
            2, f"{ACCOUNTS_ORIGIN}/sign-in"
        )
        resp = self._s.post(
            CREATE_COOKIE_SETTER_RPC,
            headers=self._grpc_headers(referer),
            data=grpcweb.frame_request(msg),
            timeout=45,
        )
        try:
            parsed = grpcweb.parse_response(resp.content)
        except Exception:
            return None

        fields = parsed["messages"][0] if parsed.get("messages") else []
        for f in fields:
            if f.get("type") == "string":
                val = str(f.get("value") or "")
                if val.startswith("http") and "set-cookie" in val:
                    return val
            elif f.get("type") == "bytes" and f.get("hex"):
                try:
                    nested = grpcweb.decode_message(bytes.fromhex(f["hex"]))
                    for nf in nested:
                        if nf.get("type") == "string":
                            val = str(nf.get("value") or "")
                            if val.startswith("http") and "set-cookie" in val:
                                return val
                except Exception:
                    pass
        return None

    # ── consent server action ───────────────────────────────────────────────

    def _scrape_consent_action_id(self, page_html: str) -> str:
        """Find live submitOAuth2Consent server-action hash from consent page JS."""
        # Prefer named createServerReference(..., "submitOAuth2Consent")
        js_urls = list(set(re.findall(r'src="(/_next/static/chunks/[^"]+\.js)"', page_html or "")))
        for path in js_urls:
            try:
                text = self._s.get(
                    f"{ACCOUNTS_ORIGIN}{path}",
                    headers={"accept": "*/*"},
                    timeout=20,
                ).text
            except Exception:
                continue
            m = re.search(
                r'createServerReference\)\("([a-f0-9]{40,44})"[^)]{0,160}"submitOAuth2Consent"',
                text,
            )
            if m:
                return m.group(1)
            if "submitOAuth2Consent" in text:
                m = re.search(r'createServerReference\)\("([a-f0-9]{40,44})"', text)
                if m:
                    return m.group(1)
        # Inline / fallback (may drift across deploys)
        m = re.search(
            r'createServerReference\)\("([a-f0-9]{40,44})"[^)]{0,160}"submitOAuth2Consent"',
            page_html or "",
        )
        if m:
            return m.group(1)
        m = re.search(r'createServerReference\)\("([a-f0-9]{40,44})"', page_html or "")
        if m:
            return m.group(1)
        return "4005315a1d7e426de592990bb54bb37471f39dd6d2"

    def _submit_oauth2_consent(self, page_url: str, page_html: str, *, state: str,
                                challenge: str, nonce: str, scopes: str) -> Optional[str]:
        """POST Next.js submitOAuth2Consent server action; return authorization code."""
        action_id = self._scrape_consent_action_id(page_html)

        router_tree = json.dumps(
            ["", {"children": ["(app)", {"children": ["(auth)", {"children":
              ["oauth2", {"children": ["consent", {"children": ["__PAGE__", {}]}]}]}]}]}],
            separators=(",", ":"),
        )

        payload = [{
            "action": "allow",
            "clientId": self.client_id,
            "redirectUri": self.redirect_uri,
            "scope": scopes,
            "state": state,
            "codeChallenge": challenge,
            "codeChallengeMethod": "S256",
            "nonce": nonce,
            "principalType": "User",
            "principalId": "",
            "referrer": "",
        }]
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "accept": "text/x-component",
            "content-type": "text/plain;charset=UTF-8",
            "next-action": action_id,
            "next-router-state-tree": quote(router_tree, safe=""),
            "origin": ACCOUNTS_ORIGIN,
            "referer": page_url,
        }

        self._log(f"submitOAuth2Consent action={action_id[:16]}...")
        resp = self._s.post(page_url, headers=headers, data=body, timeout=45)
        text = resp.text or ""

        # Look for code in response
        m = re.search(r'"code"\s*:\s*"([^"]+)"', text)
        if m:
            return m.group(1)
        m = re.search(r'code=([A-Za-z0-9._~\-]+)', text)
        if m and "error" not in m.group(0):
            return m.group(1)

        # Check redirect headers (standard Location + Next action redirect)
        for hdr in ("location", "x-action-redirect"):
            loc = resp.headers.get(hdr) or ""
            if "code=" in loc:
                return self._code_from_url(urljoin(page_url, loc), state)

        return None

    # ── redirect chain follower ─────────────────────────────────────────────

    def _follow_for_code(self, start_url: str, *, state: str, max_hops: int = 25) -> str:
        """Follow redirects until redirect_uri?code=... is reached."""
        current = start_url
        visited: set[str] = set()

        for hop in range(max_hops):
            self._log(f"hop {hop}: {current[:120]}")

            # Check if we've reached the callback
            if current.startswith(self.redirect_uri) or (
                "code=" in current and "127.0.0.1" in current
            ):
                return self._code_from_url(current, state)

            if current in visited and hop > 2:
                raise RuntimeError(f"OAuth redirect loop at {current[:120]}")
            visited.add(current)

            resp = self._get(current, allow_redirects=False)
            status = resp.status_code
            loc = resp.headers.get("location") or ""

            if status in (301, 302, 303, 307, 308) and loc:
                nxt = urljoin(current, loc)
                if nxt.startswith(self.redirect_uri) or (
                    "code=" in nxt and "127.0.0.1" in nxt
                ):
                    return self._code_from_url(nxt, state)
                current = nxt
                continue

            # HTML page: try meta-refresh / JS redirect / consent links
            html = resp.text or ""

            m2 = re.search(
                r'https?://127\.0\.0\.1[^"\'\s<>]*code=[^"\'\s<>]+', html,
            )
            if m2:
                return self._code_from_url(m2.group(0).replace("&amp;", "&"), state)

            m = re.search(
                r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=([^"\'>\s]+)',
                html, re.I,
            )
            if m:
                current = urljoin(current, m.group(1))
                continue

            # Consent page: submit server action to mint authorization code
            if "oauth2/consent" in current and status == 200:
                qs = parse_qs(urlparse(current).query)
                challenge = (qs.get("code_challenge") or [""])[0]
                nonce = (qs.get("nonce") or [""])[0]
                scope = (qs.get("scope") or ["openid profile email offline_access"])[0]
                # state from chain may differ from outer state; use page query state
                page_state = (qs.get("state") or [state])[0]
                code = self._submit_oauth2_consent(
                    current,
                    html,
                    state=page_state,
                    challenge=challenge,
                    nonce=nonce,
                    scopes=scope.replace("+", " "),
                )
                if code:
                    return code
                raise RuntimeError(
                    f"OAuth consent submit failed at {current[:120]}"
                )

            # Consent-adjacent links
            for pat in (
                r'href=["\']([^"\']*oauth2[^"\']*)["\']',
                r'action=["\']([^"\']*oauth2[^"\']*)["\']',
            ):
                m = re.search(pat, html, re.I)
                if m:
                    candidate = urljoin(current, m.group(1).replace("&amp;", "&"))
                    if candidate != current and candidate not in visited:
                        current = candidate
                        break
            else:
                raise RuntimeError(
                    f"OAuth redirect chain stalled at HTTP {status} {current[:120]}"
                )
            continue

        raise TimeoutError("OAuth redirect chain exceeded max hops")

    @staticmethod
    def _code_from_url(url: str, expected_state: str) -> str:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if qs.get("error"):
            detail = (qs.get("error_description") or qs.get("error") or [""])[0]
            raise RuntimeError(f"authorization failed: {detail}")
        code = (qs.get("code") or [""])[0]
        if not code:
            raise RuntimeError(f"authorization failed: missing code in {url[:200]}")
        return code

    # ── main login flow ─────────────────────────────────────────────────────

    def login(
        self,
        *,
        sso_token: str = "",
        email: str = "",
        password: str = "",
        scopes: str | None = None,
        referrer: str | None = None,
        plan: str | None = None,
    ) -> Dict[str, str]:
        """Full PKCE OAuth flow. Returns {code, verifier} for token exchange.

        Requires sso_token from signup. Falls back to password CreateSession
        if SSO-based cookie-setter fails (needs turnstile_token — caller must
        handle re-solving).
        """
        scopes = (scopes or DEFAULT_SCOPES).strip() or DEFAULT_SCOPES
        referrer = (referrer or DEFAULT_REFERRER).strip() or DEFAULT_REFERRER
        plan = (plan or DEFAULT_PLAN).strip() or DEFAULT_PLAN
        verifier, challenge = generate_pkce_pair()
        state = secrets.token_hex(16)
        nonce = secrets.token_hex(16)

        auth_url = (
            f"{AUTHORIZE_URL}?"
            + urlencode({
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": scopes,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "nonce": nonce,
                "plan": plan,
                "referrer": referrer,
            })
        )

        consent_url = (
            f"{CONSENT_URL}?"
            + urlencode({
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": scopes,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "nonce": nonce,
            })
        )

        # Set SSO cookie on session
        if sso_token:
            for domain in ("accounts.x.ai", ".x.ai", "auth.x.ai", ".grok.com", "grok.com"):
                try:
                    self._s.cookies.set("sso", sso_token, domain=domain)
                except Exception:
                    pass

        # Strategy 1: session reuse via cookie-setter chain
        try:
            self._log("trying session-reuse via cookie-setter...")
            # Prime the authorize endpoint
            self._get(auth_url, allow_redirects=False)

            setter_url = self.create_cookie_setter_link(
                consent_url, referer=f"{ACCOUNTS_ORIGIN}/sign-in?redirect=oauth2-provider",
            )
            if setter_url:
                self._log(f"cookie_setter: {setter_url[:80]}...")
                current = setter_url
                for _ in range(6):
                    if "code=" in current and "127.0.0.1" in current:
                        code = self._code_from_url(current, state)
                        return {"code": code, "verifier": verifier}
                    if "set-cookie" in current:
                        resp = self._get(current, allow_redirects=False)
                        loc = resp.headers.get("location") or ""
                        if loc:
                            current = urljoin(current, loc)
                            continue
                    break

                # Try consent page
                if "consent" in current:
                    page = self._get(current, allow_redirects=False)
                    loc = page.headers.get("location") or ""
                    if loc and "code=" in loc:
                        code = self._code_from_url(urljoin(current, loc), state)
                        return {"code": code, "verifier": verifier}
                    if page.status_code == 200:
                        code = self._submit_oauth2_consent(
                            current, page.text, state=state,
                            challenge=challenge, nonce=nonce, scopes=scopes,
                        )
                        if code:
                            return {"code": code, "verifier": verifier}

                # Follow redirect chain
                code = self._follow_for_code(current, state=state)
                return {"code": code, "verifier": verifier}

        except Exception as e:
            self._log(f"session-reuse failed: {e}")

        # Strategy 2: follow authorize URL directly (may work if SSO is valid)
        try:
            self._log("trying direct authorize follow...")
            code = self._follow_for_code(auth_url, state=state)
            return {"code": code, "verifier": verifier}
        except Exception as e:
            self._log(f"direct authorize failed: {e}")

        raise RuntimeError(
            "OAuth code capture failed. SSO cookie may be invalid. "
            "Consider re-solving Turnstile for CreateSession fallback."
        )
