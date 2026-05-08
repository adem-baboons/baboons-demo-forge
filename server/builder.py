"""Detect the build mode of a payload and run the appropriate build.

Three modes
-----------
* ``static`` — no ``package.json`` and no ``vite.config.*``. Files are
  served as-is.
* ``next``   — ``package.json`` includes a ``next`` dep. We expect
  ``next.config.{js,ts,mjs}`` to set ``output: "export"`` so the build
  produces a static ``out/`` directory.
* ``vite``   — ``package.json`` includes a ``vite`` dep, OR a
  ``vite.config.{js,ts,mjs,cjs}`` is present. Build produces ``dist/``.

After running ``pnpm install && pnpm run build``, we look for the build
output in this order: ``dist/`` (Vite, default Webpack), then ``out/``
(Next.js export), then ``build/`` (Create React App, some custom
setups). The first one that exists wins. The directory that wins is what
gets atomically swapped into ``/var/www/demos/{slug}/``.

node_modules / .vite / .next stay in the staging tmpdir and are never
published — only the resolved output directory is moved.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger("forge.builder")

BuildMode = Literal["static", "next", "vite"]

# Filenames that signal a Vite project even without a package.json
# (rare but valid — e.g. a single-file demo using Vite via npx).
VITE_CONFIG_FILES = (
    "vite.config.js",
    "vite.config.ts",
    "vite.config.mjs",
    "vite.config.cjs",
)

# Build output candidates checked in this order; first one that exists wins.
BUILD_OUTPUT_CANDIDATES: tuple[str, ...] = ("dist", "out", "build")


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


def _has_vite_config(staging_dir: Path) -> bool:
    return any((staging_dir / name).is_file() for name in VITE_CONFIG_FILES)


def detect_mode(staging_dir: Path) -> BuildMode:
    """Classify the staged payload as one of ``static`` / ``next`` / ``vite``."""
    pkg_path = staging_dir / "package.json"
    has_pkg = pkg_path.is_file()
    has_vite_cfg = _has_vite_config(staging_dir)

    if not has_pkg and not has_vite_cfg:
        return "static"

    pkg: dict = {}
    if has_pkg:
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise BuildError(
                f"package.json is present but not valid JSON: {exc}", logs=""
            ) from exc

    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    has_next = "next" in deps
    has_vite = "vite" in deps or has_vite_cfg

    if has_next and has_vite:
        raise BuildError(
            "package.json declares both 'next' and 'vite' dependencies. "
            "Forge can't decide which build to run — pick one.",
            logs="",
        )
    if has_next:
        return "next"
    if has_vite:
        return "vite"

    raise BuildError(
        "package.json found but no 'next' or 'vite' dependency, and no "
        "vite.config file is present. Forge currently builds Next.js "
        "(with output: 'export') and Vite. Add one of those, or remove "
        "package.json to ship as static.",
        logs="",
    )


def _resolve_build_output(staging_dir: Path) -> Path | None:
    """Return the first existing build-output directory, or ``None``."""
    for name in BUILD_OUTPUT_CANDIDATES:
        candidate = staging_dir / name
        if candidate.is_dir():
            return candidate
    return None


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
    return _build_node(staging_dir, mode)


def _build_node(staging_dir: Path, mode: BuildMode) -> BuildResult:
    """Run ``pnpm install`` and ``pnpm run build`` then resolve the output dir.

    Used for both Next.js and Vite — the only difference is which directory
    pnpm produces (Next emits ``out/``, Vite emits ``dist/``); we resolve
    by checking ``BUILD_OUTPUT_CANDIDATES`` in order.
    """
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise BuildError(
            "pnpm is not installed on this host. "
            "Install pnpm (https://pnpm.io/installation) before deploying "
            f"{mode} demos.",
            logs="",
        )

    log.info("forge.builder: %s demo at %s", mode, staging_dir)
    install_log = _run(
        [pnpm, "install", "--frozen-lockfile=false"], cwd=staging_dir
    )
    build_log = _run([pnpm, "run", "build"], cwd=staging_dir)
    combined = f"$ pnpm install\n{install_log}\n\n$ pnpm run build\n{build_log}"

    out_dir = _resolve_build_output(staging_dir)
    if out_dir is None:
        raise BuildError(
            f"{mode} build did not produce any of the expected output "
            f"directories: {', '.join(BUILD_OUTPUT_CANDIDATES)}. "
            "Check your build config — Vite emits 'dist/' by default, "
            "Next.js with output: 'export' emits 'out/'.",
            logs=combined,
        )
    log.info("forge.builder: build output resolved to %s", out_dir)
    return BuildResult(mode=mode, output_dir=out_dir, logs=combined)


def _run(cmd: list[str], cwd: Path, timeout_s: int = 300) -> str:
    """Run a command, capturing combined stdout+stderr. Raise BuildError on non-zero.

    Default timeout is 300s, generous enough for a cold ``pnpm install`` on a
    typical SPA. Subsequent installs hitting a populated pnpm-store finish in
    seconds.
    """
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
            env=os.environ.copy(),
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
