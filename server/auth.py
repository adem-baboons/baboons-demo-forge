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
from pydantic import AnyHttpUrl

log = logging.getLogger("forge.auth")


class StaticBearerVerifier(TokenVerifier):
    """Verifies that the presented token matches the configured forge token.

    Uses ``hmac.compare_digest`` so token comparison is constant-time —
    a timing attack cannot leak the token character-by-character.

    The token itself is *never* logged.

    ``base_url`` should be the public URL the resource is reachable at
    (e.g. ``https://forge.baboons.dev``). FastMCP's HTTP transport uses
    it when emitting MCP resource metadata.
    """

    def __init__(
        self,
        expected_token: str,
        client_id: str = "forge-client",
        base_url: AnyHttpUrl | str | None = None,
    ):
        if not expected_token:
            raise ValueError("expected_token must be non-empty")
        # Calls TokenVerifier.__init__ → AuthProvider.__init__, which sets
        # self.base_url and self.resource_base_url.
        super().__init__(base_url=base_url)
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
