"""End-to-end integration tests for the four MCP tools.

These tests build a real FastMCP instance, register the tools against
a tmp demos_root, and exercise them via ``mcp.call_tool``. No network,
no real /var/www/demos.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp import FastMCP

from server.config import Settings
from server.tools import register_tools


@pytest.fixture
def mcp(tmp_path: Path) -> FastMCP:
    settings = Settings(
        auth_token="x" * 64,  # not used in-process — tools don't check auth
        demos_root=tmp_path,
        host="127.0.0.1",
        port=8090,
        domain="baboons.dev",
    )
    server = FastMCP("forge-test")
    register_tools(server, settings)
    return server


def _call(mcp: FastMCP, name: str, **kwargs):
    return asyncio.run(mcp.call_tool(name, kwargs))


def test_static_deploy_then_list_then_delete(mcp: FastMCP, tmp_path: Path) -> None:
    deploy = _call(
        mcp,
        "deploy_demo",
        slug="forgetest",
        files=[{"path": "index.html", "content": "<h1>Hi</h1>"}],
    )
    body = deploy.structured_content
    assert body["ok"] is True
    assert body["slug"] == "forgetest"
    assert body["url"] == "https://forgetest.baboons.dev"
    assert body["mode"] == "static"
    assert (tmp_path / "forgetest" / "index.html").read_text() == "<h1>Hi</h1>"

    listed = _call(mcp, "list_demos").structured_content
    assert listed["count"] == 1
    assert listed["demos"][0]["slug"] == "forgetest"

    logs = _call(mcp, "get_build_logs", slug="forgetest").structured_content
    assert logs["ok"] is True
    assert "no build step" in logs["log"]

    refused = _call(mcp, "delete_demo", slug="forgetest").structured_content
    assert refused["ok"] is False
    assert "confirm" in refused["error"].lower()
    assert (tmp_path / "forgetest").exists(), "delete without confirm should not remove anything"

    removed = _call(
        mcp, "delete_demo", slug="forgetest", confirm=True
    ).structured_content
    assert removed["ok"] is True
    assert not (tmp_path / "forgetest").exists()
    assert _call(mcp, "list_demos").structured_content["count"] == 0


def test_path_traversal_is_rejected(mcp: FastMCP, tmp_path: Path) -> None:
    res = _call(
        mcp,
        "deploy_demo",
        slug="attackme",
        files=[{"path": "../escape.txt", "content": "pwn"}],
    ).structured_content
    assert res["ok"] is False
    assert ".." in res["error"]
    # Nothing should have been written either inside or above tmp_path.
    assert not (tmp_path / "attackme").exists()
    assert not (tmp_path.parent / "escape.txt").exists()


def test_absolute_path_is_rejected(mcp: FastMCP) -> None:
    res = _call(
        mcp,
        "deploy_demo",
        slug="abspath",
        files=[{"path": "/etc/passwd", "content": "x"}],
    ).structured_content
    assert res["ok"] is False
    assert "relative" in res["error"].lower()


def test_reserved_slug_rejected_with_named_reason(mcp: FastMCP) -> None:
    res = _call(
        mcp,
        "deploy_demo",
        slug="sales",
        files=[{"path": "index.html", "content": "x"}],
    ).structured_content
    assert res["ok"] is False
    assert "sales" in res["error"]
    assert "reserved" in res["error"].lower()


def test_too_many_files_rejected(mcp: FastMCP) -> None:
    payload = [
        {"path": f"f{i}.html", "content": "x"} for i in range(0, 101)
    ]
    res = _call(
        mcp, "deploy_demo", slug="bigdrop", files=payload
    ).structured_content
    assert res["ok"] is False
    assert "too many" in res["error"].lower()


def test_oversized_file_rejected(mcp: FastMCP) -> None:
    big = "x" * (5 * 1024 * 1024 + 1)
    res = _call(
        mcp,
        "deploy_demo",
        slug="oversized",
        files=[{"path": "big.txt", "content": big}],
    ).structured_content
    assert res["ok"] is False
    assert "exceeds" in res["error"].lower()


def test_base64_binary_payload(mcp: FastMCP, tmp_path: Path) -> None:
    import base64

    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    encoded = base64.b64encode(raw).decode("ascii")
    res = _call(
        mcp,
        "deploy_demo",
        slug="binary",
        files=[
            {"path": "index.html", "content": "<img src=logo.png>"},
            {"path": "logo.png", "content": encoded, "encoding": "base64"},
        ],
    ).structured_content
    assert res["ok"] is True
    assert (tmp_path / "binary" / "logo.png").read_bytes() == raw


def test_get_build_logs_for_unknown_slug(mcp: FastMCP) -> None:
    res = _call(mcp, "get_build_logs", slug="nope").structured_content
    assert res["ok"] is False
    assert "deployed" in res["error"].lower()


def test_redeploy_overwrites_previous(mcp: FastMCP, tmp_path: Path) -> None:
    _call(
        mcp,
        "deploy_demo",
        slug="redeploy",
        files=[{"path": "index.html", "content": "v1"}],
    )
    _call(
        mcp,
        "deploy_demo",
        slug="redeploy",
        files=[{"path": "index.html", "content": "v2"}],
    )
    assert (tmp_path / "redeploy" / "index.html").read_text() == "v2"
