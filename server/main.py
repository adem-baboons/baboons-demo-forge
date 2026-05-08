"""FastMCP entry point. Run with ``python -m server.main``.

Listens on FORGE_HOST:FORGE_PORT (defaults 127.0.0.1:8090) and exposes the
MCP endpoint at /mcp via Streamable HTTP. Caddy in front of the box
terminates TLS at https://forge.baboons.dev/mcp and reverse-proxies to here.
"""
from __future__ import annotations

import logging
import sys

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from . import caddy as caddy_mod
from .auth import StaticBearerVerifier
from .config import Settings
from .tools import register_tools


def build_server(settings: Settings) -> FastMCP:
    """Construct the FastMCP server with auth, tools, and a health endpoint."""
    verifier = StaticBearerVerifier(
        settings.auth_token,
        base_url=settings.public_url,
    )
    mcp = FastMCP("Baboons Demo Forge", auth=verifier)

    register_tools(mcp, settings)

    # Caddy / uptime checks hit /health unauthenticated.
    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    settings = Settings.load()
    log = logging.getLogger("forge")
    log.info(
        "starting forge mcp on http://%s:%d (demos_root=%s)",
        settings.host,
        settings.port,
        settings.demos_root,
    )
    if settings.caddy_enabled:
        ok, msg = caddy_mod.check_reload_permission(settings.caddy_reload_cmd)
        if ok:
            log.info("forge.caddy preflight: %s", msg)
        else:
            log.warning("forge.caddy preflight WARNING: %s", msg)
    mcp = build_server(settings)
    # Streamable HTTP transport — works with Claude clients that consume
    # remote MCP servers via /mcp.
    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        path="/mcp",
    )


if __name__ == "__main__":
    main()
