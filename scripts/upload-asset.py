#!/usr/bin/env python3
"""Upload a client asset (or a directory of them) to Baboons Forge.

Bypasses MCP entirely — talks straight to ``POST /assets/<client>/<filename>``
with the static bearer from ``FORGE_AUTH_TOKEN``. The whole reason this
script exists is to avoid the base64-through-tool-call corruption that
hits real client images when shipped via ``deploy_demo``.

Usage:
  Single file:
    FORGE_AUTH_TOKEN=… ./upload-asset.py --client acme --file ./logo.png
    FORGE_AUTH_TOKEN=… ./upload-asset.py --client acme --file ./logo.png --name brand.png

  Bulk (non-recursive — only image files in the given dir):
    FORGE_AUTH_TOKEN=… ./upload-asset.py --client acme --dir ./logos/

Stdlib only; no `pip install` required. Mirrors the auth + URL shape of
scripts/upload-static-dir.py but talks to the asset endpoints, not /mcp.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

FORGE_BASE = "https://forge.baboons.dev"

# Must match server.config.ALLOWED_ASSET_EXTS. Kept locally so the script
# can fail fast client-side instead of waiting for a 400.
CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".gif": "image/gif",
}


def upload_one(token: str, client: str, local_path: Path, remote_name: str) -> dict:
    ext = local_path.suffix.lower()
    content_type = CONTENT_TYPES.get(ext)
    if content_type is None:
        raise SystemExit(
            f"refusing to upload {local_path}: extension {ext!r} not in allowlist "
            f"({', '.join(sorted(CONTENT_TYPES))})"
        )
    body = local_path.read_bytes()
    url = f"{FORGE_BASE}/assets/{client}/{remote_name}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"HTTP {e.code} {e.reason} uploading {local_path} → {url}\n{detail}"
        ) from None
    import json as _json
    return _json.loads(raw)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--client", required=True,
        help="Client slug. Same format rules as demo slugs (lowercase, hyphens).",
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--file", help="Single file to upload.",
    )
    grp.add_argument(
        "--dir",
        help="Directory of files to upload. Non-recursive; only image files are picked up.",
    )
    parser.add_argument(
        "--name",
        help="Remote filename for --file mode. Defaults to the local basename.",
    )
    args = parser.parse_args()

    token = os.environ.get("FORGE_AUTH_TOKEN")
    if not token:
        print("FORGE_AUTH_TOKEN environment variable is required", file=sys.stderr)
        return 2

    if args.file:
        local = Path(args.file).resolve()
        if not local.is_file():
            print(f"--file is not a file: {local}", file=sys.stderr)
            return 2
        remote = args.name or local.name
        result = upload_one(token, args.client, local, remote)
        print(result.get("url", result))
        return 0 if result.get("ok") else 1

    # --dir mode
    if args.name is not None:
        print("--name is only valid with --file", file=sys.stderr)
        return 2
    root = Path(args.dir).resolve()
    if not root.is_dir():
        print(f"--dir is not a directory: {root}", file=sys.stderr)
        return 2
    targets = sorted(
        p for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in CONTENT_TYPES
    )
    if not targets:
        print(f"no image files in {root} (extensions: {', '.join(sorted(CONTENT_TYPES))})",
              file=sys.stderr)
        return 1

    rc = 0
    for path in targets:
        result = upload_one(token, args.client, path, path.name)
        if result.get("ok"):
            print(result["url"])
        else:
            print(f"FAILED {path.name}: {result}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
