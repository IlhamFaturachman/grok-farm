#!/usr/bin/env python3
"""
Convert grok-farm batch results → chenyme/grok2api Build account import JSON,
optionally push them into a running grok2api instance.

Standalone:
  python g2a_export.py results/batch_xxx/accounts.json -o g2a_import.json
  python g2a_export.py results/batch_xxx/accounts.json --import
  python g2a_export.py results/batch_xxx/ --import   # picks accounts.json

Env (also used by farm.py auto-import):
  GROK2API_URL=http://127.0.0.1:8000
  GROK2API_ADMIN_USER=admin
  GROK2API_ADMIN_PASS=...
  GROK2API_AUTO_IMPORT=true
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Load .env from script dir (same pattern as farm.py)
_ROOT = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)
except ImportError:
    env_path = _ROOT / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)

DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_SCOPE = (
    "openid profile email offline_access "
    "grok-cli:access api:access conversations:read conversations:write"
)


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


def _normalize_expires_at(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # treat as unix seconds
        return (
            datetime.fromtimestamp(float(value), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    s = str(value).strip()
    if not s:
        return None
    # already RFC3339-ish
    if s.endswith("+00:00"):
        return s.replace("+00:00", "Z")
    return s


def farm_record_to_g2a(record: dict[str, Any]) -> dict[str, Any] | None:
    """Map one farm.py success record → grok2api importedCredentialEntry."""
    tokens = record.get("tokens") if isinstance(record.get("tokens"), dict) else {}
    # allow flat records (already converted / hand-written)
    if not tokens and (record.get("access_token") or record.get("refresh_token")):
        tokens = record

    access = (tokens.get("access_token") or "").strip()
    refresh = (tokens.get("refresh_token") or "").strip()
    if not access and not refresh:
        return None

    # Drop bot-flagged JWTs (bfs=1 / bot_flag_source=1) — thinking dead on xAI.
    if tokens.get("bot_flagged") is True or record.get("bot_flagged") is True:
        return None
    if access and access.count(".") >= 2:
        try:
            part = access.split(".")[1]
            part += "=" * (-len(part) % 4)
            claims = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
            if claims.get("bfs") == 1 or claims.get("bfs") is True:
                return None
            if claims.get("bot_flag_source") == 1 or claims.get("bot_flag_source") is True:
                return None
        except Exception:
            pass

    email = (
        (record.get("email") or tokens.get("email") or "").strip().lower()
    )
    client_id = (
        tokens.get("client_id")
        or record.get("client_id")
        or DEFAULT_CLIENT_ID
    )
    scope = tokens.get("scope") or record.get("scope") or DEFAULT_SCOPE
    expires_at = _normalize_expires_at(
        tokens.get("expires_at") or record.get("expires_at")
    )
    expires_in = tokens.get("expires_in") or record.get("expires_in")
    try:
        expires_in_i = int(expires_in) if expires_in is not None else None
    except (TypeError, ValueError):
        expires_in_i = None

    entry: dict[str, Any] = {
        "provider": "grok_build",
        "name": email or (tokens.get("name") or "Grok Build account"),
        "client_id": str(client_id).strip() or DEFAULT_CLIENT_ID,
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "scope": str(scope).strip() or DEFAULT_SCOPE,
    }
    id_token = (tokens.get("id_token") or record.get("id_token") or "").strip()
    if id_token:
        entry["id_token"] = id_token
    if email:
        entry["email"] = email
    if expires_at:
        entry["expires_at"] = expires_at
    if expires_in_i and 0 < expires_in_i <= 365 * 24 * 3600:
        entry["expires_in"] = expires_in_i

    for k in ("user_id", "principal_id", "team_id"):
        v = tokens.get(k) or record.get(k)
        if v:
            entry[k] = str(v)

    return entry


def load_farm_accounts(path: Path) -> list[dict[str, Any]]:
    """Load accounts.json (or a directory containing it)."""
    p = path
    if p.is_dir():
        cand = p / "accounts.json"
        if not cand.is_file():
            raise FileNotFoundError(f"no accounts.json in {p}")
        p = cand
    raw = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("accounts"), list):
        # already g2a batch, or mixed
        return list(raw["accounts"])
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    raise ValueError(f"unsupported accounts format in {p}")


def _access_is_bot_flagged(access: str) -> bool:
    if not access or access.count(".") < 2:
        return False
    try:
        part = access.split(".")[1]
        part += "=" * (-len(part) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
    except Exception:
        return False
    if claims.get("bfs") == 1 or claims.get("bfs") is True:
        return True
    if claims.get("bot_flag_source") == 1 or claims.get("bot_flag_source") is True:
        return True
    return False


def convert_accounts(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Return grok2api batch document {accounts: [...]}."""
    out: list[dict[str, Any]] = []
    skipped = 0
    for rec in records:
        if not isinstance(rec, dict):
            skipped += 1
            continue
        # already a g2a entry?
        if rec.get("provider") == "grok_build" and (
            rec.get("access_token") or rec.get("refresh_token")
        ):
            access = str(rec.get("access_token") or "")
            if rec.get("bot_flagged") is True or _access_is_bot_flagged(access):
                skipped += 1
                continue
            out.append(rec)
            continue
        entry = farm_record_to_g2a(rec)
        if entry is None:
            skipped += 1
            continue
        out.append(entry)
    return {"accounts": out, "_meta": {"converted": len(out), "skipped": skipped}}


def write_import_json(doc: dict[str, Any], dest: Path) -> Path:
    """Write only the accounts array document (no _meta) for grok2api."""
    payload = {"accounts": list(doc.get("accounts") or [])}
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return dest


def export_batch(
    source: Path,
    dest: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """
    Convert farm batch → g2a_import.json.
    Returns (output_path, meta with counts).
    """
    records = load_farm_accounts(source)
    doc = convert_accounts(records)
    meta = doc.pop("_meta", {"converted": len(doc["accounts"]), "skipped": 0})
    if dest is None:
        if source.is_dir():
            dest = source / "g2a_import.json"
        else:
            dest = source.parent / "g2a_import.json"
    write_import_json(doc, dest)
    meta["path"] = str(dest)
    meta["accounts"] = len(doc["accounts"])
    return dest, meta


# ── HTTP helpers (stdlib only) ───────────────────────────────────────────────

def _http_json(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    data = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                return {}
            return json.loads(raw)
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {err_body[:500]}") from e
    except URLError as e:
        raise RuntimeError(f"request failed {url}: {e}") from e


def admin_login(base_url: str, username: str, password: str) -> str:
    """Return admin access JWT."""
    base = base_url.rstrip("/")
    url = f"{base}/api/admin/v1/auth/login"
    data = _http_json(
        "POST",
        url,
        body={"username": username, "password": password},
        timeout=30.0,
    )
    # grok2api may wrap payload as {data:{tokens:{accessToken}}} or flat {tokens:{...}}
    root = data.get("data") if isinstance(data.get("data"), dict) else data
    tokens = root.get("tokens") if isinstance(root.get("tokens"), dict) else root
    access = (
        (tokens or {}).get("accessToken")
        or (tokens or {}).get("access_token")
        or (root or {}).get("accessToken")
        or (root or {}).get("access_token")
        or data.get("accessToken")
    )
    if not access:
        raise RuntimeError(f"login response missing accessToken: {list(data.keys())}")
    return str(access)


def _multipart_body(file_path: Path, field_name: str = "file") -> tuple[bytes, str]:
    boundary = f"----GrokFarmBoundary{uuid.uuid4().hex}"
    filename = file_path.name
    content = file_path.read_bytes()
    ctype = mimetypes.guess_type(filename)[0] or "application/json"
    parts: list[bytes] = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        (
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
    )
    parts.append(content)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def _parse_sse_complete(stream_text: str) -> dict[str, Any] | None:
    """Parse SSE text; return last complete event JSON if present."""
    event_name = ""
    data_lines: list[str] = []
    last_complete: dict[str, Any] | None = None
    errors: list[str] = []

    def flush():
        nonlocal event_name, data_lines, last_complete
        if not data_lines and not event_name:
            return
        data = "\n".join(data_lines).strip()
        if event_name == "complete" and data:
            try:
                last_complete = json.loads(data)
            except json.JSONDecodeError:
                last_complete = {"raw": data}
        elif event_name == "error" and data:
            errors.append(data)
        event_name = ""
        data_lines = []

    for line in stream_text.splitlines():
        if line.startswith(":"):
            continue
        if line == "":
            flush()
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
    flush()
    if errors and last_complete is None:
        raise RuntimeError(f"import SSE error: {errors[-1][:500]}")
    return last_complete


def import_to_grok2api(
    import_json: Path,
    *,
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    access_token: str | None = None,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """
    POST multipart import to grok2api admin API.
    Returns complete-event payload (created/updated/synced/syncFailed).
    """
    base = (base_url or _env("GROK2API_URL", "http://127.0.0.1:8000")).rstrip("/")
    if not import_json.is_file():
        raise FileNotFoundError(str(import_json))

    # empty accounts — no-op success
    try:
        payload = json.loads(import_json.read_text(encoding="utf-8"))
        n = len(payload.get("accounts") or [])
    except Exception:
        n = -1
    if n == 0:
        return {"created": 0, "updated": 0, "synced": 0, "syncFailed": 0, "skipped": True}

    token = access_token
    if not token:
        user = username or _env("GROK2API_ADMIN_USER", "admin")
        pw = password or _env("GROK2API_ADMIN_PASS")
        if not pw:
            raise RuntimeError(
                "set GROK2API_ADMIN_PASS (or pass password=) for auto-import"
            )
        token = admin_login(base, user, pw)

    body, boundary = _multipart_body(import_json, "file")
    url = f"{base}/api/admin/v1/accounts/import"
    req = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "text/event-stream, application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"import HTTP {e.code}: {err_body[:800]}") from e
    except URLError as e:
        raise RuntimeError(f"import request failed: {e}") from e

    if "text/event-stream" in ctype or raw.lstrip().startswith("event:") or "data:" in raw[:200]:
        complete = _parse_sse_complete(raw)
        if complete is None:
            # try last JSON object in stream
            raise RuntimeError(f"import SSE finished without complete event: {raw[:400]}")
        return complete

    # non-SSE JSON fallback
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"unexpected import response: {raw[:400]}") from e


def export_and_maybe_import(
    source: Path,
    *,
    dest: Path | None = None,
    do_import: bool | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """
    Convert batch and optionally import.
    do_import defaults to GROK2API_AUTO_IMPORT env.
    """
    out_path, meta = export_batch(source, dest)
    result: dict[str, Any] = {
        "export_path": str(out_path),
        "converted": meta.get("converted", meta.get("accounts", 0)),
        "skipped": meta.get("skipped", 0),
        "imported": False,
    }
    should = _env_bool("GROK2API_AUTO_IMPORT", False) if do_import is None else do_import
    if should and result["converted"]:
        complete = import_to_grok2api(out_path, base_url=base_url)
        result["imported"] = True
        result["import_result"] = complete
    elif should and not result["converted"]:
        result["import_note"] = "no accounts to import"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export grok-farm accounts to grok2api Build import JSON"
    )
    parser.add_argument(
        "source",
        type=Path,
        help="farm accounts.json or batch directory",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output path (default: <batch>/g2a_import.json)",
    )
    parser.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        help="POST import to GROK2API_URL after convert",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="grok2api base URL (default: GROK2API_URL or http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="admin username (default: GROK2API_ADMIN_USER)",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="admin password (default: GROK2API_ADMIN_PASS)",
    )
    args = parser.parse_args(argv)

    if not args.source.exists():
        print(f"ERROR: not found: {args.source}", file=sys.stderr)
        return 1

    try:
        out_path, meta = export_batch(args.source, args.output)
    except Exception as e:
        print(f"ERROR: export failed: {e}", file=sys.stderr)
        return 1

    print(f"Wrote {meta.get('accounts', 0)} account(s) → {out_path}")
    if meta.get("skipped"):
        print(f"  skipped {meta['skipped']} record(s) without tokens")

    if args.do_import:
        if meta.get("accounts", 0) == 0:
            print("Nothing to import.")
            return 0
        try:
            complete = import_to_grok2api(
                out_path,
                base_url=args.url,
                username=args.user,
                password=args.password,
            )
        except Exception as e:
            print(f"ERROR: import failed: {e}", file=sys.stderr)
            return 2
        print(
            "Import complete: "
            f"created={complete.get('created')} "
            f"updated={complete.get('updated')} "
            f"synced={complete.get('synced')} "
            f"syncFailed={complete.get('syncFailed')}"
        )
        print(json.dumps(complete, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
