"""Per-slug Caddy snippet management.

For every demo we publish a self-contained site block at
``<sites_dir>/<slug>.baboons.dev.conf`` and ask Caddy to reload. The
top-level Caddyfile contains a single ``import <sites_dir>/*.conf``
directive, so adding/removing a snippet is enough to add/remove a
public route.

A flock guards against concurrent mutations: two parallel deploy_demo
calls cannot interleave a write/reload pair. On reload failure the
snippet is rolled back to its previous content (or removed if it didn't
exist before), so disk state on /etc/caddy stays consistent with what
Caddy currently has loaded.
"""
from __future__ import annotations

import fcntl
import logging
import os
import shlex
import subprocess
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("forge.caddy")

# Mirrors stax.baboons.dev's caching pattern: aggressive immutable cache
# on /assets/* and /images/*, and a no-cache header on /index.html and /
# so HTML changes propagate immediately.
SNIPPET_TEMPLATE = """\
{slug}.baboons.dev {{
    root * {root}
    try_files {{path}} {{path}}/index.html
    file_server
    encode zstd gzip

    @assets path /assets/* /images/*
    header @assets Cache-Control "public, max-age=31536000, immutable"

    @html path /index.html /
    header @html Cache-Control "no-cache, must-revalidate"
}}
"""


class CaddyError(RuntimeError):
    """Raised when a Caddy reload fails. ``output`` carries combined stdout+stderr."""

    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


def render_snippet(slug: str, demos_root: Path) -> str:
    """Render the Caddy site block for ``slug``. Pure function."""
    return SNIPPET_TEMPLATE.format(slug=slug, root=str(demos_root / slug))


def snippet_path(slug: str, sites_dir: Path) -> Path:
    return sites_dir / f"{slug}.baboons.dev.conf"


@contextmanager
def caddy_lock(lock_path: Path):
    """Exclusive flock guard against concurrent caddy mutations."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _strip_sudo_prefix(reload_cmd: str) -> list[str] | None:
    """Return the command tokens minus a leading ``sudo [-n -E …]``.

    Returns ``None`` when the command does not start with ``sudo`` (so
    there's nothing for ``sudo -l`` to preflight) or when stripping flags
    leaves us with an empty command.
    """
    try:
        parts = shlex.split(reload_cmd)
    except ValueError:
        return None
    if not parts or parts[0] != "sudo":
        return None
    rest = parts[1:]
    while rest and rest[0] in ("-n", "-E"):
        rest = rest[1:]
    return rest or None


def check_reload_permission(reload_cmd: str, timeout_s: int = 5) -> tuple[bool, str]:
    """Best-effort preflight: can the current user run ``reload_cmd``?

    Uses ``sudo -n -l -- <cmd>`` which returns 0 iff the user is permitted
    to run the given command via passwordless sudo. Does NOT execute the
    underlying command.

    Returns ``(ok, message)`` so the caller can log either way. Never raises.
    """
    cmd = _strip_sudo_prefix(reload_cmd)
    if cmd is None:
        # Either parsing failed or the reload_cmd doesn't use sudo —
        # nothing useful to preflight, so don't warn.
        return True, "reload_cmd does not use sudo; skipping preflight"

    check = ["sudo", "-n", "-l", "--"] + cmd
    try:
        proc = subprocess.run(
            check,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"could not run sudo preflight: {exc}"

    if proc.returncode == 0:
        return True, f"sudo reload permission OK ({' '.join(cmd)})"
    return False, (
        f"sudo preflight failed (exit {proc.returncode}): this user may not be "
        f"able to run '{' '.join(cmd)}' without a password. "
        f"Check /etc/sudoers.d/baboons-demo-forge-caddy. "
        f"Output: {(proc.stdout or '').strip()[:300]}"
    )


def reload_caddy(reload_cmd: str, timeout_s: int = 30) -> str:
    """Run the configured caddy-reload command. Raise CaddyError on non-zero exit."""
    cmd = shlex.split(reload_cmd)
    log.info("forge.caddy: $ %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaddyError(
            f"caddy reload timed out after {timeout_s}s",
            output=exc.output or "",
        ) from exc
    output = proc.stdout or ""
    if proc.returncode != 0:
        raise CaddyError(
            f"caddy reload failed (exit {proc.returncode})",
            output=output,
        )
    return output


def install_snippet(
    slug: str,
    sites_dir: Path,
    demos_root: Path,
    reload_cmd: str,
) -> None:
    """Write the snippet for ``slug`` and reload Caddy.

    On reload failure, the snippet is rolled back to its previous content
    (or deleted if it was brand-new) so /etc/caddy's on-disk state matches
    what Caddy currently has loaded.

    Caller MUST already hold ``caddy_lock``.
    """
    sites_dir.mkdir(parents=True, exist_ok=True)
    target = snippet_path(slug, sites_dir)
    previous = target.read_text(encoding="utf-8") if target.exists() else None

    _atomic_write(target, render_snippet(slug, demos_root))
    try:
        reload_caddy(reload_cmd)
    except CaddyError:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_write(target, previous)
        raise


def remove_snippet(
    slug: str,
    sites_dir: Path,
    reload_cmd: str,
) -> bool:
    """Remove the snippet for ``slug`` and reload Caddy.

    Returns True if a snippet file was removed, False if none existed.
    On reload failure the snippet is restored to its previous content.

    Caller MUST already hold ``caddy_lock``.
    """
    target = snippet_path(slug, sites_dir)
    if not target.exists():
        return False
    backup = target.read_text(encoding="utf-8")
    target.unlink()
    try:
        reload_caddy(reload_cmd)
    except CaddyError:
        _atomic_write(target, backup)
        raise
    return True
