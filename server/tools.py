"""The four MCP tools exposed by the Forge.

Tools are registered onto a FastMCP server in ``server.main``. They take
``Settings`` via closure so that tests can construct alternative settings
(e.g. a tmp DEMOS_ROOT) without hammering ``/var/www/demos``.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from . import caddy as caddy_mod
from .builder import BuildError, build
from .config import (
    LOGS_SUBDIR,
    MAX_BYTES_PER_FILE,
    MAX_FILES_PER_DEPLOY,
    MAX_TOTAL_BYTES,
    Settings,
    SlugError,
    validate_slug,
)

log = logging.getLogger("forge.tools")


# ---------------------------------------------------------------------------
# Input model for deploy_demo
# ---------------------------------------------------------------------------


class FileEntry(BaseModel):
    """A single file to write into the demo root."""

    path: str = Field(
        description=(
            "Relative path inside the demo root, using forward slashes. "
            "Must NOT start with '/' or contain '..'. Examples: "
            "'index.html', 'assets/logo.png', 'styles/main.css'."
        )
    )
    content: str = Field(
        description=(
            "File contents. Interpret per the 'encoding' field — text by default. "
            "For binary files (images/fonts), set encoding='base64' and pass a "
            "standard base64 string."
        )
    )
    encoding: Literal["text", "base64"] = Field(
        default="text",
        description="How 'content' is encoded. 'text' (utf-8) or 'base64' (binary).",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_relative_path(raw: str, root: Path) -> Path:
    """Validate and resolve ``raw`` to an absolute path strictly under ``root``.

    Rejects absolute paths, traversal segments, Windows-style drive letters,
    and any path that escapes ``root`` after normalisation.
    """
    if not raw or not isinstance(raw, str):
        raise ValueError("file path must be a non-empty string")
    if raw.startswith("/") or raw.startswith("\\"):
        raise ValueError(f"file path must be relative, got: {raw!r}")
    if ":" in raw:  # Windows drive letters / weird URIs
        raise ValueError(f"file path must not contain ':' — got: {raw!r}")

    pp = PurePosixPath(raw)
    if pp.is_absolute() or any(part in ("..", "") for part in pp.parts if part != pp.anchor):
        raise ValueError(f"file path must not contain '..' segments — got: {raw!r}")

    candidate = (root / raw).resolve()
    root_resolved = root.resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(
            f"file path escapes the demo root after normalisation: {raw!r}"
        ) from exc
    return candidate


def _decode(entry: FileEntry) -> bytes:
    if entry.encoding == "text":
        return entry.content.encode("utf-8")
    try:
        return base64.b64decode(entry.content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            f"file {entry.path!r}: encoding=base64 but content is not valid base64: {exc}"
        ) from exc


def _validate_payload(files: list[FileEntry]) -> list[tuple[FileEntry, bytes]]:
    """Decode + size-check every entry. Returns paired (entry, decoded_bytes)."""
    if not files:
        raise ValueError("files cannot be empty — provide at least one file")
    if len(files) > MAX_FILES_PER_DEPLOY:
        raise ValueError(
            f"too many files: {len(files)} > {MAX_FILES_PER_DEPLOY} max per deploy"
        )

    decoded: list[tuple[FileEntry, bytes]] = []
    total = 0
    seen_paths: set[str] = set()
    for entry in files:
        if entry.path in seen_paths:
            raise ValueError(f"duplicate file path in payload: {entry.path!r}")
        seen_paths.add(entry.path)

        data = _decode(entry)
        if len(data) > MAX_BYTES_PER_FILE:
            raise ValueError(
                f"file {entry.path!r} is {len(data)} bytes — "
                f"exceeds {MAX_BYTES_PER_FILE} byte per-file limit"
            )
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(
                f"total payload exceeds {MAX_TOTAL_BYTES} bytes "
                "(50 MB max across all files)"
            )
        decoded.append((entry, data))
    return decoded


def _write_files_into(staging_dir: Path, decoded: list[tuple[FileEntry, bytes]]) -> None:
    for entry, data in decoded:
        target = _safe_relative_path(entry.path, staging_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _atomic_replace_dir(src: Path, dst: Path) -> None:
    """Replace ``dst`` directory's contents with ``src`` directory's contents.

    Implementation: rename current ``dst`` aside, move ``src`` to ``dst``,
    then remove the old. Atomic from the perspective of any individual
    request — there is never a moment when ``dst`` does not exist (after
    the first successful deploy).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if dst.exists():
        backup = dst.with_name(dst.name + ".forge-old")
        if backup.exists():
            shutil.rmtree(backup)
        os.rename(dst, backup)
    try:
        shutil.move(str(src), str(dst))
    except Exception:
        # Roll back if the move failed.
        if backup is not None and not dst.exists():
            os.rename(backup, dst)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
    _make_world_readable(dst)


def _make_world_readable(root: Path) -> None:
    """Set perms across ``root`` so Caddy (a different user) can serve it.

    Without this, dirs created via ``tempfile.TemporaryDirectory`` arrive
    with mode 0700; Caddy lacks traverse permission and returns 403. We
    set 0755 on every directory and 0644 on every file under root.
    """
    os.chmod(root, 0o755)
    for cur, dirs, files in os.walk(root):
        for d in dirs:
            os.chmod(os.path.join(cur, d), 0o755)
        for f in files:
            os.chmod(os.path.join(cur, f), 0o644)


def _save_build_log(settings: Settings, slug: str, log_text: str) -> Path:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.logs_dir / f"{slug}.log"
    log_path.write_text(log_text, encoding="utf-8")
    return log_path


def _list_demo_slugs(settings: Settings) -> list[str]:
    if not settings.demos_root.is_dir():
        return []
    out: list[str] = []
    for child in sorted(settings.demos_root.iterdir()):
        if not child.is_dir():
            continue
        # Skip dirs that aren't valid demo slugs:
        #   - "."-prefixed: hidden / metadata dirs (e.g. ".forge")
        #   - "_"-prefixed: leftover build inputs (e.g. "_assets") — these
        #     can never have been created by deploy_demo (validate_slug
        #     rejects underscores) and aren't reachable as subdomains
        #     (RFC 952/3986 disallows underscores in DNS labels).
        if child.name.startswith((".", "_")):
            continue
        out.append(child.name)
    return out


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_tools(mcp: FastMCP, settings: Settings) -> None:
    """Attach the four tools to the FastMCP instance."""

    @mcp.tool(
        name="deploy_demo",
        description=(
            "Deploy a static demo to https://<slug>.baboons.dev.\n\n"
            "Modes (auto-detected from the file payload):\n"
            "  - static: any payload without a package.json — files are served as-is.\n"
            "  - next:   payload with package.json containing a 'next' dep — runs "
            "'pnpm install && pnpm run build'; expects next.config.js with "
            "output: 'export' so an out/ directory is produced.\n\n"
            "Limits: max 200 files, 5 MB per file, 50 MB total. File paths must be "
            "relative (no leading slash, no '..'). Returns the public URL on success."
        ),
    )
    def deploy_demo(
        slug: Annotated[str, Field(description="Subdomain slug, e.g. 'acme-pitch'.")],
        files: Annotated[
            list[FileEntry],
            Field(description="List of files to write into the demo root."),
        ],
    ) -> dict[str, Any]:
        try:
            slug = validate_slug(slug)
        except SlugError as exc:
            return {"ok": False, "error": str(exc)}

        try:
            decoded = _validate_payload(files)
        except ValueError as exc:
            return {"ok": False, "slug": slug, "error": str(exc)}

        with tempfile.TemporaryDirectory(prefix=f"forge-staging-{slug}-") as tmp:
            staging = Path(tmp)
            try:
                _write_files_into(staging, decoded)
            except ValueError as exc:
                return {"ok": False, "slug": slug, "error": str(exc)}

            try:
                result = build(staging)
            except BuildError as exc:
                _save_build_log(settings, slug, exc.logs or str(exc))
                return {
                    "ok": False,
                    "slug": slug,
                    "error": f"build failed: {exc}",
                    "logs": exc.logs[-4000:] if exc.logs else "",
                }

            target = settings.demos_root / slug
            try:
                _atomic_replace_dir(result.output_dir, target)
            except OSError as exc:
                return {
                    "ok": False,
                    "slug": slug,
                    "error": f"failed to publish: {exc}",
                }

            if settings.caddy_enabled:
                try:
                    with caddy_mod.caddy_lock(settings.caddy_lock_path):
                        caddy_mod.install_snippet(
                            slug,
                            settings.caddy_sites_dir,
                            settings.demos_root,
                            settings.caddy_reload_cmd,
                        )
                except caddy_mod.CaddyError as exc:
                    log.error("forge.deploy: caddy reload failed for %s: %s", slug, exc)
                    return {
                        "ok": False,
                        "slug": slug,
                        "error": (
                            f"published files to {target}, but Caddy reload failed: {exc}. "
                            "The snippet was rolled back; the route is unchanged."
                        ),
                        "caddy_output": (exc.output or "")[-2000:],
                    }

            log_path = _save_build_log(settings, slug, result.logs)
            log.info("forge.deploy: published %s (mode=%s)", slug, result.mode)
            return {
                "ok": True,
                "slug": slug,
                "url": settings.url_for(slug),
                "mode": result.mode,
                "file_count": len(decoded),
                "log_file": str(log_path),
            }

    @mcp.tool(
        name="list_demos",
        description=(
            "List every demo currently deployed under /var/www/demos/. "
            "Returns each slug and its public URL."
        ),
    )
    def list_demos() -> dict[str, Any]:
        slugs = _list_demo_slugs(settings)
        return {
            "ok": True,
            "count": len(slugs),
            "demos": [{"slug": s, "url": settings.url_for(s)} for s in slugs],
        }

    @mcp.tool(
        name="delete_demo",
        description=(
            "PERMANENTLY remove a deployed demo from /var/www/demos/<slug>/. "
            "This breaks any links to https://<slug>.baboons.dev. "
            "ALWAYS confirm with the user before calling this — there is no "
            "undo. Caller must pass confirm=True; the slug must already exist."
        ),
    )
    def delete_demo(
        slug: Annotated[str, Field(description="Slug to remove.")],
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Must be True. Forces the caller to pass an explicit confirmation "
                    "so accidental destructive calls fail loudly."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        try:
            slug = validate_slug(slug)
        except SlugError as exc:
            return {"ok": False, "error": str(exc)}
        if not confirm:
            return {
                "ok": False,
                "slug": slug,
                "error": (
                    "delete_demo refused: confirm=True is required. "
                    "Confirm with the user before retrying."
                ),
            }

        target = settings.demos_root / slug
        if not target.exists():
            return {"ok": False, "slug": slug, "error": "demo does not exist"}
        if not target.is_dir():
            return {"ok": False, "slug": slug, "error": "target is not a directory"}

        shutil.rmtree(target)

        if settings.caddy_enabled:
            try:
                with caddy_mod.caddy_lock(settings.caddy_lock_path):
                    caddy_mod.remove_snippet(
                        slug,
                        settings.caddy_sites_dir,
                        settings.caddy_reload_cmd,
                    )
            except caddy_mod.CaddyError as exc:
                log.error("forge.delete: caddy reload failed for %s: %s", slug, exc)
                return {
                    "ok": False,
                    "slug": slug,
                    "error": (
                        f"removed files at {target}, but Caddy reload failed: {exc}. "
                        "The snippet was restored; the route is unchanged."
                    ),
                    "caddy_output": (exc.output or "")[-2000:],
                }

        log_path = settings.logs_dir / f"{slug}.log"
        if log_path.exists():
            log_path.unlink()
        log.info("forge.delete: removed %s", slug)
        return {"ok": True, "slug": slug, "message": f"removed https://{slug}.{settings.domain}"}

    @mcp.tool(
        name="get_build_logs",
        description=(
            "Return the last captured build log for the given slug. Useful for "
            "debugging a failed deploy_demo call. Truncated to 16 KB tail."
        ),
    )
    def get_build_logs(
        slug: Annotated[str, Field(description="Slug whose build log to fetch.")],
    ) -> dict[str, Any]:
        try:
            slug = validate_slug(slug)
        except SlugError as exc:
            return {"ok": False, "error": str(exc)}

        log_path = settings.logs_dir / f"{slug}.log"
        if not log_path.exists():
            return {
                "ok": False,
                "slug": slug,
                "error": f"no build log for '{slug}' — has it been deployed yet?",
            }
        text = log_path.read_text(encoding="utf-8", errors="replace")
        truncated = text[-16_000:]
        return {
            "ok": True,
            "slug": slug,
            "log": truncated,
            "truncated": len(text) > len(truncated),
            "bytes": len(text),
        }
