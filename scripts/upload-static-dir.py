#!/usr/bin/env python3
"""Upload a built static directory to Baboons Forge as a demo.

Reads the dist directory recursively, detects text vs binary by file extension,
builds a JSON-RPC tools/call payload for deploy_demo, and POSTs it to
https://forge.baboons.dev/mcp with a static bearer token from FORGE_AUTH_TOKEN.

The MCP streamable HTTP transport requires a three-step handshake before any
tools/call will succeed (otherwise the server returns 400 "Missing session ID"):

  1. POST an `initialize` request. The response carries an `Mcp-Session-Id`
     header that must be echoed back on every subsequent request in this session.
  2. POST a `notifications/initialized` notification (no id, no response) with
     the session id header set.
  3. POST the actual `tools/call` with the session id header set.

Spec: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#http-transport

Usage:
  FORGE_AUTH_TOKEN=<token> ./upload-static-dir.py --dist-dir <path> --slug <slug>
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

TEXT_EXTS = {".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".json", ".svg", ".txt"}
FORGE_URL = "https://forge.baboons.dev/mcp"
PROTOCOL_VERSION = "2025-06-18"


def collect_files(dist_dir: Path) -> list[dict]:
    files = []
    for path in sorted(dist_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(dist_dir).as_posix()
        if path.suffix.lower() in TEXT_EXTS:
            files.append({
                "path": rel,
                "content": path.read_text(encoding="utf-8"),
                "encoding": "text",
            })
        else:
            files.append({
                "path": rel,
                "content": base64.b64encode(path.read_bytes()).decode("ascii"),
                "encoding": "base64",
            })
    return files


def parse_response(raw: str) -> dict:
    """Accept either a plain JSON body or an SSE stream containing a single data: <json> event."""
    text = raw.strip()
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise ValueError(f"Unrecognized response body:\n{raw}")


def post(token: str, body: dict, session_id: str | None = None, expect_response: bool = True):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        FORGE_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw_body = resp.read().decode("utf-8") if expect_response else ""
            new_session = resp.headers.get("Mcp-Session-Id")
            return raw_body, new_session
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {e.reason} for {body.get('method')}\n{detail}") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dist-dir", required=True, help="Path to the built static directory")
    parser.add_argument("--slug", required=True, help="Subdomain slug, e.g. 'acme-pitch'")
    args = parser.parse_args()

    token = os.environ.get("FORGE_AUTH_TOKEN")
    if not token:
        print("FORGE_AUTH_TOKEN environment variable is required", file=sys.stderr)
        return 2

    dist_dir = Path(args.dist_dir).resolve()
    if not dist_dir.is_dir():
        print(f"--dist-dir is not a directory: {dist_dir}", file=sys.stderr)
        return 2

    files = collect_files(dist_dir)
    total_bytes = sum(len(f["content"]) for f in files)
    print(f"Collected {len(files)} files from {dist_dir} (~{total_bytes / 1024:.1f} KB encoded)", file=sys.stderr)

    try:
        # Step 1: initialize — server returns Mcp-Session-Id header.
        init_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "upload-static-dir", "version": "1.0"},
            },
        }
        _, session_id = post(token, init_body)
        if not session_id:
            print("Server did not return Mcp-Session-Id on initialize", file=sys.stderr)
            return 1
        print(f"Initialized session {session_id}", file=sys.stderr)

        # Step 2: notifications/initialized — fire-and-forget, no id, no response body.
        post(token, {"jsonrpc": "2.0", "method": "notifications/initialized"},
             session_id=session_id, expect_response=False)

        # Step 3: the actual tools/call.
        call_body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "deploy_demo",
                "arguments": {"slug": args.slug, "files": files},
            },
        }
        raw, _ = post(token, call_body, session_id=session_id)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    parsed = parse_response(raw)
    print(json.dumps(parsed, indent=2))
    return 0 if "error" not in parsed else 1


if __name__ == "__main__":
    sys.exit(main())
