# -*- coding: utf-8 -*-
"""Turnstile solver client — delegates to Boterdrop-Solver or CapSolver API.

Boterdrop-Solver runs as a separate service (api_server.py) exposing a REST API.
This client submits a Turnstile solve task and polls until the token is ready.

Falls back to CapSolver (AntiTurnstileTaskProxyLess) if SOLVER_URL is unreachable
and CAPSOLVER_API_KEY is set.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests


class TurnstileSolver:
    """Solve Cloudflare Turnstile tokens via external solver services."""

    def __init__(
        self,
        *,
        solver_url: str = "",
        capsolver_key: str = "",
        max_wait: int = 120,
        poll_interval: int = 3,
        debug: bool = False,
    ):
        self.solver_url = (solver_url or os.environ.get("SOLVER_URL", "")).rstrip("/")
        self.capsolver_key = capsolver_key or os.environ.get("CAPSOLVER_API_KEY", "")
        self.max_wait = max_wait
        self.poll_interval = poll_interval
        self.debug = debug

    def solve_turnstile(
        self,
        website_url: str,
        website_key: str,
        *,
        proxy: str = "",
    ) -> Optional[str]:
        """Solve a Turnstile challenge and return the token, or None on failure.

        Tries Boterdrop-Solver first, then CapSolver as fallback.
        """
        token = self._solve_via_boterdrop(website_url, website_key, proxy=proxy)
        if token:
            return token

        if self.capsolver_key:
            token = self._solve_via_capsolver(website_url, website_key)
            if token:
                return token

        return None

    def _solve_via_boterdrop(
        self,
        website_url: str,
        website_key: str,
        *,
        proxy: str = "",
    ) -> Optional[str]:
        """Submit to Boterdrop-Solver and poll for result.

        Boterdrop API uses GET with query params:
          GET /turnstile?url=...&sitekey=...  -> {"task_id": "...", "status": "accepted"}
          GET /result?id=<task_id>            -> {"status": "success", "value": "<token>"}
        """
        if not self.solver_url:
            return None

        try:
            params: dict = {"url": website_url, "sitekey": website_key}
            if proxy:
                params["proxy"] = proxy
                params["proxy_support"] = True

            resp = requests.get(
                f"{self.solver_url}/turnstile",
                params=params,
                timeout=15,
            )
            if resp.status_code == 429:
                if self.debug:
                    print("  [solver] Boterdrop busy (429), trying fallback")
                return None
            if resp.status_code not in (200, 202):
                if self.debug:
                    print(f"  [solver] Boterdrop submit failed: HTTP {resp.status_code}")
                return None

            data = resp.json()
            task_id = data.get("task_id") or data.get("id")
            if not task_id:
                # Some solvers return the token directly
                token = data.get("value") or data.get("token")
                if token:
                    if self.debug:
                        print(f"  [solver] Boterdrop immediate token (len={len(token)})")
                    return token
                return None

            if self.debug:
                print(f"  [solver] Boterdrop task: {task_id}")

            start = time.time()
            while time.time() - start < self.max_wait:
                time.sleep(self.poll_interval)
                try:
                    r = requests.get(
                        f"{self.solver_url}/result",
                        params={"id": task_id},
                        timeout=15,
                    )
                except Exception:
                    continue

                if r.status_code != 200:
                    continue

                result = r.json()
                status = result.get("status", "")

                if status in ("success", "done", "ready"):
                    token = result.get("value") or result.get("token") or ""
                    if token:
                        elapsed = round(time.time() - start, 1)
                        if self.debug:
                            print(f"  [solver] Boterdrop solved in {elapsed}s (len={len(token)})")
                        return token

                if status in ("error", "failed"):
                    if self.debug:
                        print(f"  [solver] Boterdrop failed: {result.get('error', '')}")
                    return None

            if self.debug:
                print("  [solver] Boterdrop timeout")
            return None

        except Exception as e:
            if self.debug:
                print(f"  [solver] Boterdrop error: {str(e)[:80]}")
            return None

    def _solve_via_capsolver(
        self,
        website_url: str,
        website_key: str,
    ) -> Optional[str]:
        """Fallback: solve via CapSolver AntiTurnstileTaskProxyLess."""
        try:
            resp = requests.post(
                "https://api.capsolver.com/createTask",
                json={
                    "clientKey": self.capsolver_key,
                    "task": {
                        "type": "AntiTurnstileTaskProxyLess",
                        "websiteURL": website_url,
                        "websiteKey": website_key,
                    },
                },
                timeout=30,
            )
            data = resp.json()
            if data.get("errorId", 1) != 0:
                if self.debug:
                    print(f"  [solver] CapSolver create error: {data.get('errorDescription', '')}")
                return None

            task_id = data["taskId"]
            if self.debug:
                print(f"  [solver] CapSolver task: {task_id}")

            start = time.time()
            while time.time() - start < self.max_wait:
                time.sleep(5)
                r = requests.post(
                    "https://api.capsolver.com/getTaskResult",
                    json={"clientKey": self.capsolver_key, "taskId": task_id},
                    timeout=30,
                ).json()
                st = r.get("status")
                if st == "ready":
                    tok = r.get("solution", {}).get("token")
                    if self.debug:
                        print(f"  [solver] CapSolver solved (len={len(tok or '')})")
                    return tok
                if st == "failed" or r.get("errorId"):
                    if self.debug:
                        print(f"  [solver] CapSolver failed: {r.get('errorDescription', '')}")
                    return None

            return None
        except Exception as e:
            if self.debug:
                print(f"  [solver] CapSolver error: {str(e)[:80]}")
            return None
