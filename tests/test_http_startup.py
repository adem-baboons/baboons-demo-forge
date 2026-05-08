"""Boot the FastMCP server's HTTP app to catch construction-time errors.

We don't actually serve traffic in this test — building the Starlette app
is enough to exercise the auth provider's resource-metadata code path,
which is what bit us when ``StaticBearerVerifier`` skipped
``super().__init__()``.
"""
from __future__ import annotations

import asyncio

from fastmcp.server.auth.auth import MultiAuth

from server.auth import StaticBearerVerifier
from server.config import Settings
from server.main import build_server
from server.oauth import PersistentOAuthProvider


def _settings(tmp_path) -> Settings:
    return Settings(
        auth_token="x" * 64,
        demos_root=tmp_path,
        host="127.0.0.1",
        port=8090,
        domain="baboons.dev",
        public_url="https://forge.baboons.dev",
        caddy_enabled=False,
        caddy_sites_dir=tmp_path / "sites.d",
        caddy_lock_path=tmp_path / "forge-caddy.lock",
        caddy_reload_cmd="true",
        dcr_enabled=True,
        oauth_state_path=tmp_path / "oauth-state.json",
    )


def test_verifier_has_required_attrs() -> None:
    v = StaticBearerVerifier("y" * 64, base_url="https://forge.baboons.dev")
    # Both attributes are read by FastMCP's HTTP transport at startup.
    assert hasattr(v, "base_url")
    assert hasattr(v, "resource_base_url")


def test_http_app_constructs(tmp_path) -> None:
    mcp = build_server(_settings(tmp_path))
    app = mcp.http_app(path="/mcp", transport="http")
    # Starlette app — has a router with our /health route plus MCP routes.
    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/health" in paths


def test_static_bearer_satisfies_required_scope_under_multiauth(tmp_path) -> None:
    """Regression: with MultiAuth(server=oauth, verifiers=[static_bearer]),
    the static bearer must yield an AccessToken whose scopes include the
    OAuth provider's required_scopes — otherwise scoped /mcp routes 403.

    On 2026-05-08 we shipped MultiAuth wiring with StaticBearerVerifier
    returning ``scopes=[]``; OAuth's required_scopes=["forge"] then 403'd
    every static-bearer request even though the token authenticated.
    """
    bearer = StaticBearerVerifier("ops-token", client_id="forge-ops")
    oauth = PersistentOAuthProvider(
        state_path=tmp_path / "oauth-state.json",
        base_url="https://forge.baboons.dev",
        dcr_enabled=False,
    )
    auth = MultiAuth(server=oauth, verifiers=[bearer])

    # MultiAuth must inherit the OAuth provider's required scopes.
    assert auth.required_scopes is not None
    assert "forge" in auth.required_scopes

    # The static bearer must mint a token that already contains every
    # required scope, otherwise the framework's middleware will 403 it.
    token = asyncio.run(bearer.verify_token("ops-token"))
    assert token is not None
    assert set(auth.required_scopes).issubset(set(token.scopes)), (
        f"static bearer token scopes {token.scopes} do not satisfy "
        f"MultiAuth required_scopes {auth.required_scopes}"
    )


def test_static_bearer_default_scope_is_forge() -> None:
    """Independent unit test: the default scope is exactly 'forge'."""
    bearer = StaticBearerVerifier("any-token")
    token = asyncio.run(bearer.verify_token("any-token"))
    assert token is not None
    assert token.scopes == ["forge"]


def test_static_bearer_scopes_are_overridable() -> None:
    """Caller can override scopes for tests / future flexibility."""
    bearer = StaticBearerVerifier("any-token", scopes=["a", "b"])
    token = asyncio.run(bearer.verify_token("any-token"))
    assert token is not None
    assert token.scopes == ["a", "b"]
