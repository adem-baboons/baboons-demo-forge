"""Bearer-token verification for FastMCP.

Implements a single-token verifier that compares the presented bearer
token against ``FORGE_AUTH_TOKEN`` using a constant-time comparison.

Wired into FastMCP via the ``auth=`` constructor argument.
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastmcp.server.auth.auth import TokenVerifier
from mcp.server.auth.provider import AccessToken

log = logging.getLogger("forge.auth")


class StaticBearerVerifier(TokenVerifier):
    """Verifies that the presented token matches the configured forge token.

    Uses ``hmac.compare_digest`` so token comparison is constant-time —
    a timing attack cannot leak the token character-by-character.

    The token itself is *never* logged.
    """

    def __init__(self, expected_token: str, client_id: str = "forge-client"):
        if not expected_token:
            raise ValueError("expected_token must be non-empty")
        # Keep a copy in bytes to avoid re-encoding on every check.
        self._expected = expected_token.encode("utf-8")
        self._client_id = client_id

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if not token:
            return None
        presented = token.encode("utf-8")
        if not hmac.compare_digest(presented, self._expected):
            log.warning("forge auth: bearer token mismatch (rejected)")
            return None
        return AccessToken(
            token=token,
            client_id=self._client_id,
            scopes=[],
            expires_at=None,
        )
