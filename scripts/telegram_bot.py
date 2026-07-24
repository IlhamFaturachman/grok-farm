#!/usr/bin/env python3
"""Telegram command bot for Grok Farm status.

Commands (only your chat id):
  /acc /pool /status  → full status report
  /ip                 → public IP
  /help /start        → help

Long-poll getUpdates; runs as farm-telegram.service.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_SCRIPTS))


def load_env_file() -> None:
    p = _ROOT / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            os.environ[k] = v


load_env_file()

from notify import public_ip, send_telegram  # noqa: E402
from status_report import build_report  # noqa: E402

TOKEN = (os.environ.get("NOTIFY_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
ALLOWED_CHAT = (os.environ.get("NOTIFY_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
STATE_PATH = _ROOT / "results" / "telegram_bot_offset.json"
API = f"https://api.telegram.org/bot{TOKEN}"


def api(method: str, **params) -> dict:
    url = f"{API}/{method}"
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def load_offset() -> int:
    if not STATE_PATH.is_file():
        return 0
    try:
        return int(json.loads(STATE_PATH.read_text()).get("offset", 0))
    except Exception:
        return 0


def save_offset(offset: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"offset": offset}) + "\n", encoding="utf-8")


def reply(chat_id: int | str, text: str) -> None:
    chunk = text[:4000]
    try:
        api("sendMessage", chat_id=str(chat_id), text=chunk, disable_web_page_preview="true")
    except Exception as e:
        print(f"reply error: {e}", flush=True)
        try:
            send_telegram(chunk)
        except Exception:
            pass


HELP = """🤖 Grok Farm bot

Commands:
/acc     — status pool + akun (full report)
/pool    — sama seperti /acc
/status  — sama seperti /acc
/ip      — public IP VPS
/help    — bantuan

Hanya chat terdaftar yang dijawab.
Report otomatis juga tiap 6 jam.
"""


def handle(text: str) -> str:
    cmd = (text or "").strip().split()[0].lower() if text else ""
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]

    if cmd in ("/start", "/help"):
        return HELP
    if cmd in ("/acc", "/pool", "/status", "/accounts", "/akun"):
        return build_report()
    if cmd in ("/ip",):
        return f"🌐 Public IP: {public_ip()}"
    if cmd.startswith("/"):
        return f"Unknown command: {cmd}\n\n{HELP}"
    return ""


def main() -> int:
    if not TOKEN or not ALLOWED_CHAT:
        print("ERROR: set NOTIFY_TELEGRAM_BOT_TOKEN and NOTIFY_TELEGRAM_CHAT_ID", flush=True)
        return 1

    print(f"telegram bot start allowed_chat={ALLOWED_CHAT}", flush=True)
    offset = load_offset()

    while True:
        try:
            params: dict = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
            if offset:
                params["offset"] = offset
            data = api("getUpdates", **params)
        except urllib.error.URLError as e:
            print(f"getUpdates network: {e}", flush=True)
            time.sleep(5)
            continue
        except Exception as e:
            print(f"getUpdates error: {e}", flush=True)
            time.sleep(3)
            continue

        if not data.get("ok"):
            print(f"api not ok: {data}", flush=True)
            time.sleep(3)
            continue

        for upd in data.get("result") or []:
            uid = int(upd.get("update_id", 0)) + 1
            if uid > offset:
                offset = uid
                save_offset(offset)

            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            text = msg.get("text") or ""

            if chat_id != str(ALLOWED_CHAT):
                print(f"ignore chat {chat_id}", flush=True)
                continue
            if not text.startswith("/"):
                continue

            print(f"cmd from {chat_id}: {text!r}", flush=True)
            try:
                out = handle(text)
            except Exception as e:
                out = f"Error building report: {e}"
            if out:
                reply(chat_id, out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
