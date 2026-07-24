#!/usr/bin/env python3
"""Grok Farm alerts — Telegram / email when batch fails or IP looks blocked.

Usage:
  # after a batch (called automatically from farm.py / farm_http.py)
  from notify import maybe_alert_batch
  maybe_alert_batch(batch_dir, created=3, failed=7)

  # manual test
  python notify.py --test
  python notify.py --check results/batch_xxx/
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent


def _load_dotenv_file(path: Path) -> None:
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k:
                os.environ[k] = v
    except Exception:
        pass


try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=True)
except ImportError:
    _load_dotenv_file(_ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)) or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)) or default)
    except ValueError:
        return default


# ── Config ───────────────────────────────────────────────────────────────────

NOTIFY_ENABLED = _env_bool("NOTIFY_ENABLED", False)
NOTIFY_TELEGRAM_BOT_TOKEN = _env("NOTIFY_TELEGRAM_BOT_TOKEN") or _env("TELEGRAM_BOT_TOKEN")
NOTIFY_TELEGRAM_CHAT_ID = _env("NOTIFY_TELEGRAM_CHAT_ID") or _env("TELEGRAM_CHAT_ID")

NOTIFY_EMAIL_TO = _env("NOTIFY_EMAIL_TO")
NOTIFY_SMTP_HOST = _env("NOTIFY_SMTP_HOST", "smtp.gmail.com")
NOTIFY_SMTP_PORT = _env_int("NOTIFY_SMTP_PORT", 587)
NOTIFY_SMTP_USER = _env("NOTIFY_SMTP_USER") or _env("GROK_IMAP_USER")
NOTIFY_SMTP_PASS = (_env("NOTIFY_SMTP_PASS") or _env("GROK_IMAP_PASS")).replace(" ", "")
NOTIFY_EMAIL_FROM = _env("NOTIFY_EMAIL_FROM") or NOTIFY_SMTP_USER

# Alert thresholds
NOTIFY_MIN_ATTEMPTS = _env_int("NOTIFY_MIN_ATTEMPTS", 3)
NOTIFY_FAIL_RATE = _env_float("NOTIFY_FAIL_RATE", 0.6)  # 60%
NOTIFY_IP_FAIL_RATE = _env_float("NOTIFY_IP_FAIL_RATE", 0.5)  # of fails classified as IP
NOTIFY_COOLDOWN_MIN = _env_int("NOTIFY_COOLDOWN_MIN", 30)
NOTIFY_ON_BATCH_DONE = _env_bool("NOTIFY_ON_BATCH_DONE", True)
NOTIFY_ON_IP_SUSPECT = _env_bool("NOTIFY_ON_IP_SUSPECT", True)
NOTIFY_ON_ALL_FAIL = _env_bool("NOTIFY_ON_ALL_FAIL", True)

STATE_PATH = Path(_env("NOTIFY_STATE_FILE", str(_ROOT / "results" / "notify_state.json")))

# Patterns that usually mean IP / bot / CF pressure (not OTP/IMAP config)
_IP_PATTERNS = [
    r"turnstile",
    r"verification failed",
    r"cloudflare",
    r"cf[-_ ]?clearance",
    r"challenge",
    r"access denied",
    r"forbidden",
    r"\b403\b",
    r"\b429\b",
    r"rate.?limit",
    r"too many requests",
    r"blocked",
    r"bot.?detect",
    r"captcha",
    r"suspicious",
    r"page load error",
    r"goto signup failed",
    r"net::err",
    r"timeout after .*turnstile",
    r"complete.?signup",
    r"unclear / form not advancing",
    r"account timeout",
]
_IP_RE = re.compile("|".join(_IP_PATTERNS), re.I)

_OTP_PATTERNS = [
    r"otp",
    r"imap",
    r"confirmation code",
    r"mail",
]
_OTP_RE = re.compile("|".join(_OTP_PATTERNS), re.I)


def public_ip() -> str:
    for url in (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    ):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "grok-farm-notify/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                ip = resp.read().decode("utf-8", errors="replace").strip()
                if ip and len(ip) < 64:
                    return ip
        except Exception:
            continue
    return "unknown"


def classify_error(error: str) -> str:
    e = (error or "").strip()
    if not e:
        return "unknown"
    if _IP_RE.search(e):
        return "ip_suspect"
    if _OTP_RE.search(e):
        return "otp_email"
    return "other"


def load_failed(batch_dir: Path) -> list[dict[str, Any]]:
    path = batch_dir / "failed.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def analyze_batch(
    batch_dir: Path | str,
    *,
    created: int | None = None,
    failed: int | None = None,
) -> dict[str, Any]:
    batch_dir = Path(batch_dir)
    rows = load_failed(batch_dir)

    if created is None or failed is None:
        meta_path = batch_dir / "batch_meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                created = int(meta.get("created", created or 0) or 0)
                failed = int(meta.get("failed", failed or 0) or 0)
            except Exception:
                pass
        if created is None:
            acc = batch_dir / "accounts.json"
            try:
                created = len(json.loads(acc.read_text())) if acc.is_file() else 0
            except Exception:
                created = 0
        if failed is None:
            failed = len(rows)

    created = int(created or 0)
    failed = int(failed or 0)
    total = created + failed
    fail_rate = (failed / total) if total else 0.0

    by_class: dict[str, int] = {"ip_suspect": 0, "otp_email": 0, "other": 0, "unknown": 0}
    samples: dict[str, list[str]] = {"ip_suspect": [], "otp_email": [], "other": [], "unknown": []}
    for row in rows:
        err = str(row.get("error") or "")
        cls = classify_error(err)
        by_class[cls] = by_class.get(cls, 0) + 1
        if len(samples[cls]) < 3 and err:
            samples[cls].append(err[:160])

    ip_fails = by_class.get("ip_suspect", 0)
    ip_share = (ip_fails / failed) if failed else 0.0

    reasons: list[str] = []
    severity = "ok"

    if total >= NOTIFY_MIN_ATTEMPTS and failed == total and NOTIFY_ON_ALL_FAIL:
        severity = "critical"
        reasons.append(f"ALL_FAIL: {failed}/{total} failed (0 success)")

    if total >= NOTIFY_MIN_ATTEMPTS and fail_rate >= NOTIFY_FAIL_RATE:
        if severity != "critical":
            severity = "warning"
        reasons.append(f"HIGH_FAIL_RATE: {fail_rate:.0%} ({failed}/{total})")

    if (
        NOTIFY_ON_IP_SUSPECT
        and total >= NOTIFY_MIN_ATTEMPTS
        and failed >= max(2, NOTIFY_MIN_ATTEMPTS - 1)
        and ip_share >= NOTIFY_IP_FAIL_RATE
        and fail_rate >= 0.4
    ):
        severity = "critical"
        reasons.append(
            f"IP_SUSPECT: {ip_fails}/{failed} fails look like Turnstile/CF/block "
            f"(ip_share={ip_share:.0%})"
        )

    return {
        "batch_dir": str(batch_dir),
        "batch_id": batch_dir.name,
        "created": created,
        "failed": failed,
        "total": total,
        "fail_rate": round(fail_rate, 3),
        "by_class": by_class,
        "ip_share": round(ip_share, 3),
        "samples": samples,
        "severity": severity,
        "reasons": reasons,
        "should_alert": severity in ("warning", "critical") and bool(reasons),
        "public_ip": public_ip(),
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _cooldown_ok(kind: str) -> bool:
    state = _load_state()
    last = float(state.get("last_alert", {}).get(kind, 0) or 0)
    if time.time() - last < NOTIFY_COOLDOWN_MIN * 60:
        return False
    return True


def _mark_sent(kind: str) -> None:
    state = _load_state()
    la = state.setdefault("last_alert", {})
    la[kind] = time.time()
    state["last_public_ip"] = public_ip()
    _save_state(state)


def format_message(report: dict[str, Any], *, title: str | None = None) -> str:
    sev = report.get("severity", "ok").upper()
    icon = {"OK": "✅", "WARNING": "⚠️", "CRITICAL": "🚨"}.get(sev, "ℹ️")
    title = title or f"{icon} Grok Farm alert — {sev}"
    lines = [
        title,
        "",
        f"Batch : {report.get('batch_id')}",
        f"IP    : {report.get('public_ip')}",
        f"OK    : {report.get('created')}  |  FAIL: {report.get('failed')}  |  total {report.get('total')}",
        f"Rate  : {float(report.get('fail_rate') or 0):.0%} fail",
        f"Class : ip={report.get('by_class', {}).get('ip_suspect', 0)} "
        f"otp={report.get('by_class', {}).get('otp_email', 0)} "
        f"other={report.get('by_class', {}).get('other', 0)}",
    ]
    if report.get("reasons"):
        lines.append("")
        lines.append("Why:")
        for r in report["reasons"]:
            lines.append(f"  • {r}")
    samples = (report.get("samples") or {}).get("ip_suspect") or []
    if samples:
        lines.append("")
        lines.append("Sample IP-like errors:")
        for s in samples:
            lines.append(f"  - {s}")
    if report.get("severity") == "critical" and report.get("by_class", {}).get("ip_suspect", 0):
        lines.append("")
        lines.append("Action: stop farm → ganti Public Network di panel VPS → test 1 akun lagi.")
    lines.append("")
    lines.append(f"Time: {report.get('checked_at')}")
    return "\n".join(lines)


def send_telegram(text: str) -> bool:
    token = NOTIFY_TELEGRAM_BOT_TOKEN
    chat_id = NOTIFY_TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": "true",
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return bool(data.get("ok"))
    except Exception as e:
        print(f"[notify] telegram error: {e}", flush=True)
        return False


def send_email(subject: str, text: str) -> bool:
    if not NOTIFY_EMAIL_TO or not NOTIFY_SMTP_USER or not NOTIFY_SMTP_PASS:
        return False
    msg = EmailMessage()
    msg["Subject"] = subject[:200]
    msg["From"] = NOTIFY_EMAIL_FROM or NOTIFY_SMTP_USER
    msg["To"] = NOTIFY_EMAIL_TO
    msg.set_content(text)
    try:
        with smtplib.SMTP(NOTIFY_SMTP_HOST, NOTIFY_SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            if NOTIFY_SMTP_PORT != 465:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(NOTIFY_SMTP_USER, NOTIFY_SMTP_PASS)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print(f"[notify] email error: {e}", flush=True)
        return False


def dispatch(text: str, *, subject: str | None = None) -> dict[str, bool]:
    """Send via configured channels. Email-only is fine (leave Telegram empty)."""
    out = {"telegram": False, "email": False}
    # Email first (preferred for this project)
    if NOTIFY_EMAIL_TO:
        out["email"] = send_email(subject or "Grok Farm alert", text)
    # Telegram only if explicitly configured
    if NOTIFY_TELEGRAM_BOT_TOKEN and NOTIFY_TELEGRAM_CHAT_ID:
        out["telegram"] = send_telegram(text)
    return out


def maybe_alert_batch(
    batch_dir: Path | str,
    *,
    created: int | None = None,
    failed: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Analyze batch and send alerts if thresholds hit. Safe no-op if disabled."""
    report = analyze_batch(batch_dir, created=created, failed=failed)
    report["sent"] = {"telegram": False, "email": False, "skipped": True, "reason": ""}

    if not NOTIFY_ENABLED and not force:
        report["sent"]["reason"] = "NOTIFY_ENABLED=false"
        return report

    if not report.get("should_alert") and not force:
        report["sent"]["reason"] = "thresholds_not_met"
        print(
            f"[notify] ok — no alert (fail_rate={report['fail_rate']:.0%} "
            f"severity={report['severity']})",
            flush=True,
        )
        return report

    kind = "ip_suspect" if "IP_SUSPECT" in " ".join(report.get("reasons") or []) else "batch_fail"
    if not force and not _cooldown_ok(kind):
        report["sent"]["reason"] = f"cooldown_{NOTIFY_COOLDOWN_MIN}m"
        print(f"[notify] skipped (cooldown {NOTIFY_COOLDOWN_MIN}m for {kind})", flush=True)
        return report

    text = format_message(report)
    subject = f"[Grok Farm] {report.get('severity', 'alert').upper()} — {report.get('public_ip')}"
    sent = dispatch(text, subject=subject)
    report["sent"] = {**sent, "skipped": False, "reason": "sent"}
    if any(sent.values()):
        _mark_sent(kind)
        print(f"[notify] sent telegram={sent['telegram']} email={sent['email']}", flush=True)
    else:
        print(
            "[notify] no channel configured — set NOTIFY_TELEGRAM_* or NOTIFY_EMAIL_TO",
            flush=True,
        )
        report["sent"]["reason"] = "no_channel"
    return report


def maybe_alert_midrun(
    *,
    consecutive_fails: int,
    last_errors: list[str],
    batch_id: str = "",
) -> dict[str, Any] | None:
    """Live alert during long runs when consecutive failures look like IP block."""
    if not NOTIFY_ENABLED:
        return None
    threshold = _env_int("NOTIFY_CONSECUTIVE_FAILS", 5)
    if consecutive_fails < threshold:
        return None
    ip_hits = sum(1 for e in last_errors if classify_error(e) == "ip_suspect")
    if ip_hits < max(3, threshold - 1):
        return None
    if not _cooldown_ok("midrun_ip"):
        return None

    ip = public_ip()
    text = format_message(
        {
            "batch_id": batch_id or "live",
            "public_ip": ip,
            "created": 0,
            "failed": consecutive_fails,
            "total": consecutive_fails,
            "fail_rate": 1.0,
            "by_class": {"ip_suspect": ip_hits, "otp_email": 0, "other": consecutive_fails - ip_hits},
            "samples": {"ip_suspect": [e[:160] for e in last_errors if classify_error(e) == "ip_suspect"][:3]},
            "severity": "critical",
            "reasons": [
                f"CONSECUTIVE_FAILS: {consecutive_fails} in a row",
                f"IP_SUSPECT: {ip_hits}/{len(last_errors)} recent errors look like block/Turnstile",
            ],
            "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        title="🚨 Grok Farm LIVE — IP mungkin kena",
    )
    sent = dispatch(text, subject=f"[Grok Farm] LIVE IP suspect — {ip}")
    if any(sent.values()):
        _mark_sent("midrun_ip")
    return {"sent": sent, "public_ip": ip, "consecutive_fails": consecutive_fails}


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Grok Farm notify / IP-suspect alerts")
    p.add_argument("--test", action="store_true", help="Send a test message now")
    p.add_argument("--check", metavar="BATCH_DIR", help="Analyze batch and alert if needed")
    p.add_argument("--force", action="store_true", help="Ignore NOTIFY_ENABLED / thresholds")
    p.add_argument("--ip", action="store_true", help="Print public IP only")
    args = p.parse_args(argv)

    if args.ip:
        print(public_ip())
        return 0

    if args.test:
        ip = public_ip()
        text = (
            f"✅ Grok Farm notify TEST\n\n"
            f"Public IP: {ip}\n"
            f"Telegram: {'on' if NOTIFY_TELEGRAM_BOT_TOKEN else 'off'}\n"
            f"Email: {'on' if NOTIFY_EMAIL_TO else 'off'}\n"
            f"Time: {datetime.now(timezone.utc).isoformat()}Z"
        )
        # force channels even if NOTIFY_ENABLED false when --test --force
        global NOTIFY_ENABLED
        if args.force:
            NOTIFY_ENABLED = True
        if not NOTIFY_ENABLED:
            print("NOTIFY_ENABLED=false — use --force or set NOTIFY_ENABLED=true")
            print(text)
            return 1
        sent = dispatch(text, subject=f"[Grok Farm] TEST — {ip}")
        print(f"sent={sent}")
        return 0 if any(sent.values()) else 2

    if args.check:
        report = maybe_alert_batch(args.check, force=args.force)
        print(json.dumps(report, indent=2))
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
