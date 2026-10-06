#!/usr/bin/env python3
"""
Import farmed Grok accounts into Etteum pool database.

Reads accounts.json from farm batches, classifies them, and imports:
  - THINK accounts → provider=grok, tokens={access_token, refresh_token, sso_cookie, ...}
  - BFS/NOTHINK accounts → provider=grok, tokens={sso_cookie, ...}, metadata={no_think: true}

Etteum's GrokProvider uses:
  - access_token for chat (via grok2api)
  - sso_cookie + dpop keys for image/video (via console.x.ai)

Usage:
    python import_to_etteum.py --batch results/batch_xxx/
    python import_to_etteum.py --all  # scan all batches
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ETTEUM_DB = os.environ.get("ETTEUM_DB", "/opt/etteum/data/poolprox3.db")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_accounts(batch_dir: str) -> list[dict[str, Any]]:
    """Load accounts from a batch directory."""
    p = Path(batch_dir)
    if p.is_dir():
        p = p / "accounts.json"
    if not p.is_file():
        print(f"ERROR: {p} not found")
        return []
    with open(p) as f:
        data = json.load(f)
    if not isinstance(data, list):
        data = [data]
    return data


def import_account_to_etteum(conn: sqlite3.Connection, account: dict[str, Any]) -> str:
    """
    Import a single account to Etteum DB.
    Returns status: "imported", "exists", "skipped".
    """
    email = account.get("email", "").lower().strip()
    if not email:
        return "skipped"

    tokens = account.get("tokens", account)
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    sso_cookie = tokens.get("sso_cookie", "")
    classification = account.get("classification", "")
    no_think = account.get("no_think", False)

    # Skip accounts without any useful tokens
    if not access_token and not sso_cookie:
        return "skipped"

    # Build tokens JSON for Etteum
    etteum_tokens = {}
    if access_token:
        etteum_tokens["access_token"] = access_token
    if refresh_token:
        etteum_tokens["refresh_token"] = refresh_token
    if sso_cookie:
        etteum_tokens["sso_cookie"] = sso_cookie

    # Build metadata
    metadata = {}
    if classification:
        metadata["classification"] = classification
    if no_think:
        metadata["no_think"] = True
        metadata["image_exhausted"] = False
        metadata["video_exhausted"] = False
        metadata["image_quota_used"] = 0
        metadata["video_quota_used"] = 0
    else:
        # THINK account — can do chat
        metadata["classification"] = "THINK"

    # Set quota based on classification
    if no_think:
        quota_limit = 7.0
        quota_remaining = 7.0
        metadata["image_quota_remaining"] = 5
        metadata["video_quota_remaining"] = 2
    elif sso_cookie:
        quota_limit = 7.0
        quota_remaining = 7.0
        metadata["image_quota_remaining"] = 5
        metadata["video_quota_remaining"] = 2
    else:
        quota_limit = 1000000.0
        quota_remaining = 1000000.0

    # Check if account already exists
    c = conn.cursor()
    c.execute("SELECT id, metadata FROM accounts WHERE provider='grok' AND email=?", (email,))
    existing = c.fetchone()
    if existing:
        # Update existing account — preserve quota/exhausted metadata
        existing_id = existing[0]
        existing_meta = {}
        if existing[1]:
            try:
                existing_meta = json.loads(existing[1])
            except (json.JSONDecodeError, TypeError):
                existing_meta = {}

        # Merge: new metadata as base, but preserve quota tracking fields
        quota_fields = [
            "image_quota_used", "image_quota_remaining", "image_exhausted",
            "video_quota_used", "video_quota_remaining", "video_exhausted",
            "chat_quota", "chat_quota_used",
        ]
        for field in quota_fields:
            if field in existing_meta:
                metadata[field] = existing_meta[field]

        c.execute(
            """UPDATE accounts SET tokens=?, metadata=?, status='active', quota_limit=?, quota_remaining=?, updated_at=? WHERE id=?""",
            (json.dumps(etteum_tokens), json.dumps(metadata), quota_limit, quota_remaining, datetime.now(), existing_id),
        )
        conn.commit()
        return "exists"

    # Insert new account
    password = account.get("password", "")
    c.execute(
        """INSERT INTO accounts (provider, email, password, status, enabled, tokens, metadata, quota_limit, quota_remaining, created_at, updated_at)
           VALUES ('grok', ?, ?, 'active', 1, ?, ?, ?, ?, ?, ?)""",
        (
            email,
            password,
            json.dumps(etteum_tokens),
            json.dumps(metadata),
            quota_limit,
            quota_remaining,
            datetime.now(),
            datetime.now(),
        ),
    )
    conn.commit()
    return "imported"


def import_batch(batch_dir: str, db_path: str) -> dict[str, int]:
    """Import all accounts from a batch directory."""
    accounts = load_accounts(batch_dir)
    if not accounts:
        return {"imported": 0, "exists": 0, "skipped": 0}

    stats = {"imported": 0, "exists": 0, "skipped": 0}
    conn = sqlite3.connect(db_path)
    for acc in accounts:
        result = import_account_to_etteum(conn, acc)
        stats[result] = stats.get(result, 0) + 1
    conn.close()
    return stats


def import_all(results_dir: str, db_path: str) -> dict[str, int]:
    """Scan all batch directories and import accounts."""
    results = Path(results_dir)
    batches = sorted(results.glob("batch_*/accounts.json"), reverse=True)

    total = {"imported": 0, "exists": 0, "skipped": 0}
    conn = sqlite3.connect(db_path)

    for batch_file in batches:
        accounts = load_accounts(str(batch_file))
        if not accounts:
            continue
        batch_name = batch_file.parent.name
        for acc in accounts:
            result = import_account_to_etteum(conn, acc)
            total[result] = total.get(result, 0) + 1
        print(f"  {batch_name}: {len(accounts)} accounts")

    conn.close()
    return total


def main():
    parser = argparse.ArgumentParser(description="Import farmed Grok accounts to Etteum")
    parser.add_argument("--batch", help="Path to batch directory or accounts.json")
    parser.add_argument("--all", action="store_true", help="Scan all batches in results/")
    parser.add_argument("--db", default=ETTEUM_DB, help="Etteum database path")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: Etteum DB not found at {args.db}")
        sys.exit(1)

    if args.all:
        results_dir = Path("/opt/grok-farm/results")
        print(f"Scanning all batches in {results_dir}...")
        stats = import_all(str(results_dir), args.db)
    elif args.batch:
        print(f"Importing from {args.batch}...")
        stats = import_batch(args.batch, args.db)
    else:
        parser.print_help()
        sys.exit(1)

    print(f"""
Done:
  Imported: {stats.get("imported", 0)}
  Updated:  {stats.get("exists", 0)}
  Skipped:  {stats.get("skipped", 0)}
""")


if __name__ == "__main__":
    main()
