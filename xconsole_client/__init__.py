# -*- coding: utf-8 -*-
"""xconsole_client — HTTP-only x.ai account registration protocol.

Reconstructs the accounts.x.ai signup/login/OAuth flow without a browser,
using gRPC-web protobuf calls + Next.js server actions + curl_cffi fingerprinting.

Protocol reverse-engineered from network captures; see README.md for details.
"""
from __future__ import annotations

from .client import XConsoleAuthClient
from .oauth import ProtocolOAuthClient, encode_create_session_request
from .solver import TurnstileSolver

__all__ = [
    "XConsoleAuthClient",
    "ProtocolOAuthClient",
    "TurnstileSolver",
    "encode_create_session_request",
]
