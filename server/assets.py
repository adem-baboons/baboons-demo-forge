"""HTTP endpoints for centralized client asset hosting.

These are plain HTTP routes (NOT MCP tools) attached to the FastMCP app
via ``mcp.custom_route``. The whole reason this module exists is to bypass
the base64-through-tool-call corruption pattern that hits real client
images when shipped via ``deploy_demo`` — by uploading raw bytes over a
straight POST instead of through a JSON-RPC payload.

Endpoints (mounted on https://forge.baboons.dev/):

  POST   /assets/<client>/<filename>  — write  (bearer required)
  DELETE /assets/<client>/<filename>  — remove (bearer required)
  GET    /assets/<client>/            — list   (bearer required)
  GET    /assets/<client>/<filename>  — served by Caddy file_server, NOT here

The MCP MultiAuth layer does NOT apply to ``custom_route`` handlers
(that's why ``/health`` works without auth), so each mutating endpoint
verifies ``FORGE_AUTH_TOKEN`` itself with a constant-time compare.
"""
from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import (
    ALLOWED_ASSET_EXTS,
    ASSET_FILENAME_MAX_LEN,
    ASSETS_SUBDIR,
    MAX_ASSET_BYTES,
    Settings,
    SlugError,
    validate_slug,
)

log = logging.getLogger("forge.assets")


def _check_bearer(request: Request, expected_token: str) -> bool:
    """Constant-time bearer check. Same semantics as StaticBearerVerifier."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    presented = auth[7:].encode("utf-8")
    return hmac.compare_digest(presented, expected_token.encode("utf-8"))


def _validate_filename(name: str) -> str:
    """Validate a single filename component. Raises ValueError with a safe message."""
    if not name or not isinstance(name, str):
        raise ValueError("filename must be a non-empty string")
    if len(name) > ASSET_FILENAME_MAX_LEN:
        raise ValueError(
            f"filename too long ({len(name)} > {ASSET_FILENAME_MAX_LEN} chars)"
        )
    if "/" in name or "\\" in name or ":" in name:
        raise ValueError(
            f"filename must not contain path separators or ':' — got {name!r}"
        )
    if name in (".", "..") or name.startswith("."):
        raise ValueError(
            f"filename must not be '.', '..', or start with '.' — got {name!r}"
        )
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_ASSET_EXTS:
        allowed = ", ".join(sorted(ALLOWED_ASSET_EXTS))
        raise ValueError(
            f"filename extension {ext!r} not allowed (allowed: {allowed})"
        )
    return name


def _assets_root(settings: Settings) -> Path:
    return settings.demos_root / ASSETS_SUBDIR


def _public_url(settings: Settings, client: str, filename: str) -> str:
    return f"{settings.public_url.rstrip('/')}/assets/{client}/{filename}"


async def _read_body_capped(request: Request, cap: int) -> bytes | None:
    """Stream the body up to ``cap`` bytes. Returns None if the cap is breached.

    We pre-check Content-Length when present to fail fast without buffering,
    then enforce the cap again during the stream loop because Content-Length
    is client-supplied and can lie.
    """
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > cap:
                return None
        except ValueError:
            pass  # malformed — fall through to streaming cap
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def register_asset_routes(mcp: FastMCP, settings: Settings) -> None:
    """Attach POST/DELETE/list handlers for /assets/* onto the FastMCP app.

    GET on a specific file (``/assets/<client>/<filename>``) is intentionally
    not registered — Caddy's ``file_server`` serves those directly from disk
    for lower CPU and immutable-Cache-Control headers.
    """
    assets_root = _assets_root(settings)

    @mcp.custom_route("/assets/{client}/{filename}", methods=["POST"])
    async def upload_asset(request: Request) -> JSONResponse:
        if not _check_bearer(request, settings.auth_token):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        try:
            client = validate_slug(request.path_params["client"])
        except SlugError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        try:
            filename = _validate_filename(request.path_params["filename"])
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        body = await _read_body_capped(request, MAX_ASSET_BYTES)
        if body is None:
            return JSONResponse(
                {"ok": False, "error": f"upload exceeds {MAX_ASSET_BYTES} byte cap"},
                status_code=413,
            )

        client_dir = assets_root / client
        client_dir.mkdir(parents=True, exist_ok=True)

        target = client_dir / filename
        tmp = client_dir / f".{filename}.tmp.{os.getpid()}"
        try:
            tmp.write_bytes(body)
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            log.error("forge.assets: write failed %s/%s: %s", client, filename, exc)
            return JSONResponse(
                {"ok": False, "error": f"write failed: {exc}"}, status_code=500
            )

        url = _public_url(settings, client, filename)
        log.info("forge.assets: uploaded %s (%d bytes)", url, len(body))
        return JSONResponse(
            {"ok": True, "url": url, "size": len(body)}, status_code=200
        )

    @mcp.custom_route("/assets/{client}/{filename}", methods=["DELETE"])
    async def delete_asset(request: Request) -> JSONResponse:
        if not _check_bearer(request, settings.auth_token):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        try:
            client = validate_slug(request.path_params["client"])
        except SlugError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        try:
            filename = _validate_filename(request.path_params["filename"])
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        target = assets_root / client / filename
        if not target.is_file():
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        target.unlink()
        # Best-effort: drop the client dir if it's now empty. Swallow ENOTEMPTY
        # and any other OSError — the file removal already succeeded.
        try:
            (assets_root / client).rmdir()
        except OSError:
            pass

        url = _public_url(settings, client, filename)
        log.info("forge.assets: deleted %s", url)
        return JSONResponse({"ok": True, "deleted": url}, status_code=200)

    @mcp.custom_route("/assets/{client}/", methods=["GET"])
    async def list_assets(request: Request) -> JSONResponse:
        if not _check_bearer(request, settings.auth_token):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        try:
            client = validate_slug(request.path_params["client"])
        except SlugError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        client_dir = assets_root / client
        if not client_dir.is_dir():
            return JSONResponse(
                {"ok": True, "client": client, "assets": []}, status_code=200
            )
        entries = []
        for child in sorted(client_dir.iterdir()):
            if not child.is_file() or child.name.startswith("."):
                continue
            entries.append(
                {
                    "name": child.name,
                    "url": _public_url(settings, client, child.name),
                    "size": child.stat().st_size,
                }
            )
        return JSONResponse(
            {"ok": True, "client": client, "assets": entries}, status_code=200
        )
