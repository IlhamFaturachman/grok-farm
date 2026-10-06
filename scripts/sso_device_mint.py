#!/usr/bin/env python3
"""SSO cookie jar → OIDC tokens via xAI device-code grant (no PKCE browser consent).

Port of the grok-regkit cpa_xai protocol mint path:
  device/code → open verification_uri → device/verify → device/approve → poll token
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
ISSUER = "https://auth.x.ai"
DEVICE_CODE_URL = f"{ISSUER}/oauth2/device/code"
TOKEN_URL = f"{ISSUER}/oauth2/token"
VERIFY_URL = f"{ISSUER}/oauth2/device/verify"
APPROVE_URL = f"{ISSUER}/oauth2/device/approve"
# Match CLIProxyAPI internal/auth/xai Scope exactly (extra scopes can trigger Access denied).
SCOPE = "openid profile email offline_access grok-cli:access api:access"

LogFn = Callable[[str], None]


class MintError(RuntimeError):
    pass


def _log_noop(_: str) -> None:
    return None


def _b64url_json(segment: str) -> dict[str, Any]:
    import base64

    pad = "=" * (-len(segment) % 4)
    raw = base64.urlsafe_b64decode(segment + pad)
    return json.loads(raw.decode("utf-8", errors="replace"))


def jwt_claims(token: str) -> dict[str, Any]:
    parts = (token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        return _b64url_json(parts[1])
    except Exception:
        return {}


def principal_from_cookies(cookies: Any) -> str:
    sso = extract_sso(cookies)
    claims = jwt_claims(sso)
    for key in ("sub", "principal_id", "user_id", "uid", "id"):
        v = claims.get(key)
        if v:
            return str(v).strip()
    return ""


def extract_sso(cookies: Any) -> str:
    if not cookies:
        return ""
    if isinstance(cookies, str):
        return cookies.strip()
    if isinstance(cookies, dict):
        for name in ("sso", "sso-rw"):
            v = cookies.get(name)
            if v:
                return str(v).strip()
        return ""
    found_rw = ""
    if isinstance(cookies, (list, tuple)):
        for c in cookies:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "")
            value = c.get("value")
            if not value:
                continue
            if name == "sso":
                return str(value).strip()
            if name == "sso-rw" and not found_rw:
                found_rw = str(value).strip()
    return found_rw


def _cf_session(proxy: str | None = None):
    try:
        from curl_cffi import requests as cf_requests
    except ImportError as e:
        raise MintError("curl_cffi required for sso device mint") from e
    s = cf_requests.Session()
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _inject_cookies(session: Any, cookies: Any) -> int:
    n = 0
    items: list[dict] = []
    if isinstance(cookies, dict):
        items = [{"name": k, "value": v} for k, v in cookies.items()]
    elif isinstance(cookies, (list, tuple)):
        items = [c for c in cookies if isinstance(c, dict)]
    for c in items:
        name = str(c.get("name") or "").strip()
        value = c.get("value")
        if not name or value is None:
            continue
        domain = str(c.get("domain") or ".x.ai").strip() or ".x.ai"
        path = str(c.get("path") or "/").strip() or "/"
        for d in (domain, ".x.ai", "accounts.x.ai", "auth.x.ai"):
            try:
                session.cookies.set(name, str(value), domain=d, path=path)
                n += 1
                break
            except Exception:
                try:
                    session.cookies.set(name, str(value), domain=d)
                    n += 1
                    break
                except Exception:
                    continue
    return n


def _post_form_urllib(url: str, form: dict[str, str], timeout: float = 30.0) -> tuple[int, Any]:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "grok-farm-sso-mint/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200) or 200
            try:
                return int(status), json.loads(body)
            except json.JSONDecodeError:
                return int(status), body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return int(e.code), json.loads(body)
        except json.JSONDecodeError:
            return int(e.code), body


def request_device_code(*, timeout: float = 30.0) -> dict[str, Any]:
    status, body = _post_form_urllib(
        DEVICE_CODE_URL,
        {"client_id": CLIENT_ID, "scope": SCOPE},
        timeout=timeout,
    )
    if status != 200 or not isinstance(body, dict):
        raise MintError(f"device/code status={status} body={str(body)[:200]}")
    for k in ("device_code", "user_code", "verification_uri_complete"):
        if not body.get(k):
            raise MintError(f"device/code missing {k}: {body}")
    return body


def poll_token(device_code: str, *, interval: int = 5, expires_in: int = 90, timeout: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + max(expires_in, 30)
    sleep_for = max(interval, 2)
    while time.monotonic() < deadline:
        status, body = _post_form_urllib(
            TOKEN_URL,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": CLIENT_ID,
            },
            timeout=timeout,
        )
        if status == 200 and isinstance(body, dict) and body.get("access_token"):
            return body
        if isinstance(body, dict):
            err = str(body.get("error") or "")
            if err in ("authorization_pending", "slow_down"):
                if err == "slow_down":
                    sleep_for = min(sleep_for + 2, 15)
                time.sleep(sleep_for)
                continue
            if err:
                raise MintError(f"token poll error={err} desc={body.get('error_description')}")
        time.sleep(sleep_for)
    raise MintError("token poll timeout")


def mint_from_cookies(
    cookies: Any,
    *,
    proxy: str | None = None,
    timeout: float = 30.0,
    poll_timeout_sec: float = 90.0,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Return OIDC token dict: access_token, refresh_token, id_token, expires_in, …"""
    log = log or _log_noop
    sso = extract_sso(cookies)
    if not sso:
        raise MintError("no sso/sso-rw cookie after login")
    principal_id = principal_from_cookies(cookies)
    claims = jwt_claims(sso)
    log(
        f"sso_mint sso_len={len(sso)} principal={principal_id[:36] or '-'} "
        f"claims_keys={sorted(claims.keys())[:12]}"
    )

    session = _cf_session(proxy)
    n = _inject_cookies(session, cookies)
    log(f"sso_mint cookies_injected={n}")

    imp = "chrome131"
    try:
        r = session.get(
            "https://accounts.x.ai/",
            impersonate=imp,
            timeout=timeout,
            allow_redirects=True,
        )
    except TypeError:
        r = session.get("https://accounts.x.ai/", timeout=timeout, allow_redirects=True)
    except Exception as e:
        raise MintError(f"accounts.x.ai error: {e}") from e

    final_url = str(getattr(r, "url", "") or "")
    if "sign-in" in final_url or "sign-up" in final_url:
        raise MintError(f"sso invalid landed={final_url[:120]}")
    log(f"sso_mint session_ok url={final_url[:100]}")

    dc = request_device_code(timeout=timeout)
    user_code = str(dc["user_code"])
    device_code = str(dc["device_code"])
    verify_complete = str(dc["verification_uri_complete"])
    interval = int(dc.get("interval") or 5)
    log(f"sso_mint user_code={user_code}")

    try:
        try:
            session.get(verify_complete, impersonate=imp, timeout=timeout, allow_redirects=True)
        except TypeError:
            session.get(verify_complete, timeout=timeout, allow_redirects=True)
    except Exception as e:
        raise MintError(f"open verification_uri failed: {e}") from e

    try:
        try:
            r = session.post(
                VERIFY_URL,
                data={"user_code": user_code},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                impersonate=imp,
                timeout=timeout,
                allow_redirects=True,
            )
        except TypeError:
            r = session.post(
                VERIFY_URL,
                data={"user_code": user_code},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=timeout,
                allow_redirects=True,
            )
    except Exception as e:
        raise MintError(f"device/verify failed: {e}") from e

    verify_url = str(getattr(r, "url", "") or "")
    try:
        body_snip = (r.text or "")[:300]
    except Exception:
        body_snip = ""
    if "consent" not in verify_url.lower() and "consent" not in body_snip.lower():
        if "authorize" not in body_snip.lower() and "授权" not in body_snip:
            raise MintError(
                f"device/verify unexpected status={getattr(r, 'status_code', '?')} "
                f"url={verify_url[:160]} body={body_snip[:120]!r}"
            )
    log(f"sso_mint verify_ok url={verify_url[:120]}")

    approve_forms = [
        {
            "user_code": user_code,
            "action": "allow",
            "principal_type": "User",
            "principal_id": principal_id,
        },
        {
            "user_code": user_code,
            "action": "allow",
            "principal_type": "User",
            "principal_id": "",
        },
        {"user_code": user_code, "action": "allow"},
    ]
    r = None
    last_approve_err: Exception | None = None
    for form in approve_forms:
        try:
            try:
                r = session.post(
                    APPROVE_URL,
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate=imp,
                    timeout=timeout,
                    allow_redirects=True,
                )
            except TypeError:
                r = session.post(
                    APPROVE_URL,
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=timeout,
                    allow_redirects=True,
                )
            log(
                f"sso_mint approve try principal_id_len={len(str(form.get('principal_id') or ''))} "
                f"status={getattr(r, 'status_code', '?')} url={str(getattr(r, 'url', '') or '')[:100]}"
            )
            break
        except Exception as e:
            last_approve_err = e
            continue
    if r is None:
        raise MintError(f"device/approve failed: {last_approve_err}")

    approve_url = str(getattr(r, "url", "") or "")
    try:
        text = r.text or ""
    except Exception:
        text = ""
    if (
        "done" not in approve_url
        and "device/done" not in approve_url
        and "设备已授权" not in text
        and "device authorized" not in text.lower()
        and "done" not in text.lower()
    ):
        # soft continue — some builds still poll OK
        log(f"sso_mint approve soft status={getattr(r, 'status_code', '?')} url={approve_url[:120]}")
    else:
        log(f"sso_mint approve_ok url={approve_url[:120]}")

    tr = poll_token(
        device_code,
        interval=interval,
        expires_in=min(int(dc.get("expires_in") or 90), int(poll_timeout_sec)),
        timeout=timeout,
    )
    if not tr.get("refresh_token"):
        raise MintError("token response missing refresh_token")
    log(f"sso_mint token_ok expires_in={tr.get('expires_in')}")
    return {
        "access_token": tr["access_token"],
        "refresh_token": tr["refresh_token"],
        "id_token": tr.get("id_token") or "",
        "token_type": tr.get("token_type") or "Bearer",
        "expires_in": int(tr.get("expires_in") or 21600),
        "scope": tr.get("scope") or SCOPE,
        "mint_method": "sso_device",
    }
