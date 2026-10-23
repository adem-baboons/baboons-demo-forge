"""Configuration, paths, slug validation, and the reserved-name blocklist.

Pure functions / constants here are safe to import in tests without
any environment variables being set. Anything that *requires* env vars
lives behind ``Settings.load()``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

# 2..30 chars, lowercase alnum + hyphen, no leading/trailing hyphen.
SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")
SLUG_MIN_LEN = 2
SLUG_MAX_LEN = 30

# Subdomains that must never be claimed by a demo. Includes the things
# already in use on baboons.dev plus the standard service-y names that
# have a habit of being squatted by accident.
RESERVED_SLUGS: frozenset[str] = frozenset(
    {
        # Existing baboons.dev sites.
        "sales",
        "deploy",
        "trades",
        "stax",
        "diwan",
        "diwantravel",
        "forge",
        # Generic / infrastructure.
        "api",
        "www",
        "mail",
        "smtp",
        "imap",
        "pop",
        "ftp",
        "ns1",
        "ns2",
        "cpanel",
        "webmail",
        "admin",
        "root",
    }
)


class SlugError(ValueError):
    """Raised when a slug fails validation. The message is end-user-safe."""


def validate_slug(slug: str) -> str:
    """Return the slug if valid, otherwise raise SlugError with a helpful message.

    The returned value is the lowercased, trimmed slug — callers should
    use the *return value*, not the input, so casing/whitespace is normalised.
    """
    if not isinstance(slug, str):
        raise SlugError(f"Slug must be a string, got {type(slug).__name__}.")

    cleaned = slug.strip().lower()
    if not cleaned:
        raise SlugError("Slug cannot be empty.")
    if len(cleaned) < SLUG_MIN_LEN:
        raise SlugError(f"Slug '{cleaned}' is too short (min {SLUG_MIN_LEN} chars).")
    if len(cleaned) > SLUG_MAX_LEN:
        raise SlugError(f"Slug '{cleaned}' is too long (max {SLUG_MAX_LEN} chars).")
    if not SLUG_PATTERN.fullmatch(cleaned):
        raise SlugError(
            f"Slug '{cleaned}' is invalid: use lowercase letters, digits, "
            "and hyphens only; cannot start or end with a hyphen."
        )
    if cleaned in RESERVED_SLUGS:
        raise SlugError(
            f"Slug '{cleaned}' is reserved (collides with an existing "
            "baboons.dev service or infrastructure subdomain). "
            "Pick a different name."
        )
    return cleaned


# ---------------------------------------------------------------------------
# File-payload limits (per deploy_demo call)
# ---------------------------------------------------------------------------

MAX_FILES_PER_DEPLOY = 800
MAX_BYTES_PER_FILE = 5 * 1024 * 1024  # 5 MB
MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MB


# ---------------------------------------------------------------------------
# Asset hosting (per-client, served from <demos_root>/.assets/<client>/)
# ---------------------------------------------------------------------------

# Lives alongside demo slug directories. The leading dot keeps it out of
# _list_demo_slugs enumeration (which already filters dot-prefixed dirs).
ASSETS_SUBDIR = ".assets"

# 10 MB per asset upload. Real client logos sit comfortably under 1 MB; the
# cap exists to keep a malicious or accidentally-huge upload from filling
# the disk in one request.
MAX_ASSET_BYTES = 10 * 1024 * 1024

# Lowercase extensions (including the leading dot). Allowlist — anything
# not in this set is rejected before the file touches disk.
ALLOWED_ASSET_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}
)

# Cap the filename length so a pathologically long name can't bloat the
# directory listing or trip filesystem limits.
ASSET_FILENAME_MAX_LEN = 128


# ---------------------------------------------------------------------------
# Runtime settings (env-driven)
# ---------------------------------------------------------------------------

DEFAULT_DEMOS_ROOT = Path("/var/www/demos")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
DEFAULT_DOMAIN = "baboons.dev"
DEFAULT_PUBLIC_URL = "https://forge.baboons.dev"
LOGS_SUBDIR = ".forge/logs"

# Caddy integration: each successful deploy writes a per-slug snippet at
# ``<sites_dir>/<slug>.baboons.dev.conf`` and triggers ``reload_cmd``. A
# flock at ``lock_path`` serialises concurrent mutations.
#
# The default lock path lives under demos_root rather than /var/lock so it
# is reachable under the systemd unit's ProtectSystem=strict — without
# needing to whitelist /var/lock in ReadWritePaths.
DEFAULT_CADDY_SITES_DIR = Path("/etc/caddy/sites.d")
DEFAULT_CADDY_LOCK_FILENAME = "forge-caddy.lock"
DEFAULT_CADDY_RELOAD_CMD = "sudo -n /usr/bin/systemctl reload caddy"

# OAuth state lives under ``demos_root/.forge/`` (already inside the
# systemd unit's ReadWritePaths). The file is written with mode 0600.
DEFAULT_OAUTH_STATE_FILENAME = "oauth-state.json"


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Build with ``Settings.load()``."""

    auth_token: str
    demos_root: Path
    host: str
    port: int
    domain: str
    public_url: str

    # Caddy integration
    caddy_enabled: bool
    caddy_sites_dir: Path
    caddy_lock_path: Path
    caddy_reload_cmd: str

    # OAuth integration
    dcr_enabled: bool
    oauth_state_path: Path

    @property
    def logs_dir(self) -> Path:
        return self.demos_root / LOGS_SUBDIR

    def url_for(self, slug: str) -> str:
        return f"https://{slug}.{self.domain}"

    @classmethod
    def load(cls) -> "Settings":
        token = os.getenv("FORGE_AUTH_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "FORGE_AUTH_TOKEN env var is required. "
                "Generate a 64-char random token and set it via /etc/baboons-demo-forge/forge.env."
            )
        if len(token) < 32:
            raise RuntimeError(
                "FORGE_AUTH_TOKEN is too short (min 32 chars). "
                "Generate a fresh, high-entropy token."
            )

        return cls(
            auth_token=token,
            demos_root=Path(os.getenv("FORGE_DEMOS_ROOT", str(DEFAULT_DEMOS_ROOT))),
            host=os.getenv("FORGE_HOST", DEFAULT_HOST),
            port=int(os.getenv("FORGE_PORT", str(DEFAULT_PORT))),
            domain=os.getenv("FORGE_DOMAIN", DEFAULT_DOMAIN),
            public_url=os.getenv("FORGE_PUBLIC_URL", DEFAULT_PUBLIC_URL),
            caddy_enabled=os.getenv("FORGE_CADDY_ENABLED", "1") == "1",
            caddy_sites_dir=Path(
                os.getenv("FORGE_CADDY_SITES_DIR", str(DEFAULT_CADDY_SITES_DIR))
            ),
            caddy_lock_path=Path(
                os.getenv(
                    "FORGE_CADDY_LOCK_PATH",
                    str(
                        Path(os.getenv("FORGE_DEMOS_ROOT", str(DEFAULT_DEMOS_ROOT)))
                        / ".forge"
                        / DEFAULT_CADDY_LOCK_FILENAME
                    ),
                )
            ),
            caddy_reload_cmd=os.getenv("FORGE_CADDY_RELOAD_CMD", DEFAULT_CADDY_RELOAD_CMD),
            dcr_enabled=os.getenv("FORGE_DCR_ENABLED", "0") == "1",
            oauth_state_path=Path(
                os.getenv(
                    "FORGE_OAUTH_STATE_PATH",
                    str(
                        Path(os.getenv("FORGE_DEMOS_ROOT", str(DEFAULT_DEMOS_ROOT)))
                        / ".forge"
                        / DEFAULT_OAUTH_STATE_FILENAME
                    ),
                )
            ),
        )
