"""Boot the FastMCP server's HTTP app to catch construction-time errors.

We don't actually serve traffic in this test — building the Starlette app
is enough to exercise the auth provider's resource-metadata code path,
which is what bit us when ``StaticBearerVerifier`` skipped
``super().__init__()``.
"""
from __future__ import annotations

from server.auth import StaticBearerVerifier
from server.config import Settings
from server.main import build_server


def _settings(tmp_path) -> Settings:
    return Settings(
        auth_token="x" * 64,
        demos_root=tmp_path,
        host="127.0.0.1",
        port=8090,
        domain="baboons.dev",
        public_url="https://forge.baboons.dev",
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
