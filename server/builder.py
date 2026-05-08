"""Detect static vs npm/Next.js demos and run the appropriate build.

Conventions
-----------
* ``static`` — no ``package.json``. Files are served as-is.
* ``next``  — ``package.json`` includes a ``next`` dep. We expect the
  user's ``next.config.js`` to have ``output: "export"`` so that
  ``pnpm build`` produces a static ``out/`` directory we can serve.

The build runs in the staging dir; the resulting ``out/`` (or the staging
dir itself for static demos) is what the caller atomically swaps into
``/var/www/demos/{slug}/``.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger("forge.builder")

BuildMode = Literal["static", "next"]


class BuildError(RuntimeError):
    """Raised when a build command fails. ``logs`` carries the captured output."""

    def __init__(self, message: str, logs: str):
        super().__init__(message)
        self.logs = logs


@dataclass(frozen=True)
class BuildResult:
    mode: BuildMode
    output_dir: Path  # what to publish
    logs: str  # combined stdout+stderr (or a one-line note for static demos)


def detect_mode(staging_dir: Path) -> BuildMode:
    """Return ``"next"`` if a Next.js project is detected, else ``"static"``."""
    pkg_path = staging_dir / "package.json"
    if not pkg_path.is_file():
        return "static"
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise BuildError(
            f"package.json is present but not valid JSON: {exc}", logs=""
        ) from exc
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    if "next" in deps:
        return "next"
    # package.json without next: we don't know how to build this.
    raise BuildError(
        "package.json found but no 'next' dependency. "
        "The Forge only builds Next.js (with output: 'export') or pure static sites. "
        "Either add a Next.js dep or remove package.json to ship as static.",
        logs="",
    )


def build(staging_dir: Path) -> BuildResult:
    """Build the project at ``staging_dir`` and return the path to publish."""
    mode = detect_mode(staging_dir)
    if mode == "static":
        log.info("forge.builder: static demo (no build step)")
        return BuildResult(
            mode="static",
            output_dir=staging_dir,
            logs="static demo — no build step ran",
        )
    return _build_next(staging_dir)


def _build_next(staging_dir: Path) -> BuildResult:
    """Run ``pnpm install`` and ``pnpm build`` in the staging dir."""
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise BuildError(
            "pnpm is not installed on this host. "
            "Install pnpm (https://pnpm.io/installation) before deploying Next.js demos.",
            logs="",
        )

    log.info("forge.builder: next demo at %s", staging_dir)
    install_log = _run([pnpm, "install", "--frozen-lockfile=false"], cwd=staging_dir)
    build_log = _run([pnpm, "run", "build"], cwd=staging_dir)
    combined = f"$ pnpm install\n{install_log}\n\n$ pnpm run build\n{build_log}"

    out_dir = staging_dir / "out"
    if not out_dir.is_dir():
        raise BuildError(
            "Next.js build did not produce an 'out/' directory. "
            "Make sure next.config.js has output: 'export' for static export.",
            logs=combined,
        )

    return BuildResult(mode="next", output_dir=out_dir, logs=combined)


def _run(cmd: list[str], cwd: Path, timeout_s: int = 300) -> str:
    """Run a command, capturing combined stdout+stderr. Raise BuildError on non-zero."""
    log.info("forge.builder: $ %s (cwd=%s)", " ".join(cmd), cwd)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            f"command timed out after {timeout_s}s: {' '.join(cmd)}",
            logs=exc.output or "",
        ) from exc

    output = proc.stdout or ""
    if proc.returncode != 0:
        raise BuildError(
            f"command failed with exit code {proc.returncode}: {' '.join(cmd)}",
            logs=output,
        )
    return output
