"""HTTP integration tests for the /assets/* routes.

These exercise ``register_asset_routes`` against a real Starlette app
spun up via ``mcp.http_app``. No network, no real /var/www/demos —
``tmp_path`` plays the role of demos_root.
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest
from fastmcp import FastMCP
from starlette.testclient import TestClient

from server.assets import _validate_filename, register_asset_routes
from server.config import ASSETS_SUBDIR, MAX_ASSET_BYTES, Settings


AUTH_TOKEN = "x" * 64
BEARER = {"Authorization": f"Bearer {AUTH_TOKEN}"}

# 1×1 PNG (89 bytes) — handy as a real-ish payload.
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
    b"\xff?\x03\x00\x05\xfe\x02\xfe\xa7]\x05\xbb\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        auth_token=AUTH_TOKEN,
        demos_root=tmp_path,
        host="127.0.0.1",
        port=8090,
        domain="baboons.dev",
        public_url="https://forge.baboons.dev",
        caddy_enabled=False,
        caddy_sites_dir=tmp_path / "sites.d",
        caddy_lock_path=tmp_path / "forge-caddy.lock",
        caddy_reload_cmd="true",
        dcr_enabled=True,
        oauth_state_path=tmp_path / "oauth-state.json",
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    mcp = FastMCP("forge-assets-test")
    register_asset_routes(mcp, _settings(tmp_path))
    return TestClient(mcp.http_app(path="/mcp", transport="http"))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_upload_requires_auth(client: TestClient) -> None:
    r = client.post("/assets/acme/logo.png", content=TINY_PNG)
    assert r.status_code == 401
    assert r.json() == {"ok": False, "error": "unauthorized"}


def test_upload_rejects_wrong_bearer(client: TestClient) -> None:
    r = client.post(
        "/assets/acme/logo.png",
        content=TINY_PNG,
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 401


def test_delete_requires_auth(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / ASSETS_SUBDIR / "acme").mkdir(parents=True)
    (tmp_path / ASSETS_SUBDIR / "acme" / "logo.png").write_bytes(TINY_PNG)
    r = client.delete("/assets/acme/logo.png")
    assert r.status_code == 401
    # File must still exist — auth failure is not allowed to delete.
    assert (tmp_path / ASSETS_SUBDIR / "acme" / "logo.png").is_file()


def test_list_requires_auth(client: TestClient) -> None:
    r = client.get("/assets/acme/")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Validation: client slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_client", ["A", "forge", "a", "has_underscore", "-leading"])
def test_upload_validates_client_slug(client: TestClient, bad_client: str) -> None:
    r = client.post(
        f"/assets/{bad_client}/logo.png", content=TINY_PNG, headers=BEARER
    )
    assert r.status_code == 400, r.text
    assert r.json()["ok"] is False


# ---------------------------------------------------------------------------
# Validation: filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        ".hidden.png",       # leading dot
        "evil.exe",          # disallowed extension
        "no-extension",      # missing extension
        "page.html",         # disallowed extension
        "script.js",         # disallowed extension
    ],
)
def test_upload_validates_filename(client: TestClient, bad_name: str) -> None:
    r = client.post(f"/assets/acme/{bad_name}", content=TINY_PNG, headers=BEARER)
    assert r.status_code == 400, r.text
    assert r.json()["ok"] is False


def test_validate_filename_rejects_traversal_and_separators() -> None:
    """Unit-level coverage for filename inputs that cannot easily be driven
    through the HTTP layer:

    - ``..`` and ``.``: httpx (the HTTP client inside Starlette's TestClient)
      runs path normalization on the request URL before it is sent. A request
      to ``/assets/acme/..`` is rewritten to ``/assets/`` on the wire, which
      never reaches our route — so a TestClient assertion of ``status == 400``
      tests the client's normalizer, not our validator.
    - ``sub/logo.png`` / ``sub\\logo.png`` / ``C:logo.png``: Starlette's default
      ``{filename}`` path converter doesn't match path separators, so requests
      containing them either 404 or never assemble in the client.

    The validator itself must still reject all of these — if the routing
    layer is ever changed (different converter, regex match, etc.) the
    server cannot fall back to "the client would have stripped it." Do NOT
    rewrite this as an HTTP test thinking it'll be more end-to-end; you'll
    silently lose this coverage to client-side URL normalization.
    """
    for bad in ("..", ".", "sub/logo.png", "sub\\logo.png", "C:logo.png"):
        with pytest.raises(ValueError):
            _validate_filename(bad)


def test_validate_filename_rejects_overlong() -> None:
    with pytest.raises(ValueError):
        _validate_filename("a" * 200 + ".png")


# ---------------------------------------------------------------------------
# Validation: body size
# ---------------------------------------------------------------------------


def test_max_asset_bytes_constant_is_10mb() -> None:
    """Pin the documented contract — the README + Caddy notes both say 10 MB."""
    assert MAX_ASSET_BYTES == 10 * 1024 * 1024


def test_upload_rejects_oversized_via_content_length(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content-Length over the cap → 413 without reading the body. Cap
    lowered via monkeypatch so the test doesn't have to push 10 MB through
    the in-memory ASGI transport (~150 s per call on this machine)."""
    monkeypatch.setattr("server.assets.MAX_ASSET_BYTES", 1024)
    r = client.post("/assets/acme/logo.png", content=b"\x00" * 1025, headers=BEARER)
    assert r.status_code == 413, r.text


def test_upload_accepts_exactly_max_bytes(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: exactly cap bytes should succeed. Cap monkeypatched for speed."""
    monkeypatch.setattr("server.assets.MAX_ASSET_BYTES", 1024)
    payload = b"\x00" * 1024
    r = client.post("/assets/acme/logo.png", content=payload, headers=BEARER)
    assert r.status_code == 200, r.text
    assert (tmp_path / ASSETS_SUBDIR / "acme" / "logo.png").stat().st_size == 1024




# ---------------------------------------------------------------------------
# Happy path: upload
# ---------------------------------------------------------------------------


def test_upload_creates_client_dir_and_writes_file(
    client: TestClient, tmp_path: Path
) -> None:
    assets_root = tmp_path / ASSETS_SUBDIR
    assert not assets_root.exists()  # nothing pre-created
    r = client.post("/assets/acme/logo.png", content=TINY_PNG, headers=BEARER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["url"] == "https://forge.baboons.dev/assets/acme/logo.png"
    assert body["size"] == len(TINY_PNG)
    written = assets_root / "acme" / "logo.png"
    assert written.read_bytes() == TINY_PNG


def test_upload_public_url_uses_settings_public_url(tmp_path: Path) -> None:
    """URL in the response comes from settings.public_url, not a hardcoded host."""
    s = _settings(tmp_path)
    custom = Settings(
        auth_token=s.auth_token,
        demos_root=s.demos_root,
        host=s.host,
        port=s.port,
        domain=s.domain,
        public_url="https://staging.forge.baboons.dev",
        caddy_enabled=s.caddy_enabled,
        caddy_sites_dir=s.caddy_sites_dir,
        caddy_lock_path=s.caddy_lock_path,
        caddy_reload_cmd=s.caddy_reload_cmd,
        dcr_enabled=s.dcr_enabled,
        oauth_state_path=s.oauth_state_path,
    )
    mcp = FastMCP("forge-assets-test")
    register_asset_routes(mcp, custom)
    c = TestClient(mcp.http_app(path="/mcp", transport="http"))
    r = c.post("/assets/acme/logo.png", content=TINY_PNG, headers=BEARER)
    assert r.status_code == 200
    assert r.json()["url"] == "https://staging.forge.baboons.dev/assets/acme/logo.png"


def test_upload_file_is_world_readable(client: TestClient, tmp_path: Path) -> None:
    """Caddy serves these as the caddy user — file must have 0644 visible bits."""
    r = client.post("/assets/acme/logo.png", content=TINY_PNG, headers=BEARER)
    assert r.status_code == 200
    written = tmp_path / ASSETS_SUBDIR / "acme" / "logo.png"
    mode = stat.S_IMODE(written.stat().st_mode)
    assert mode & 0o644 == 0o644, f"file mode {oct(mode)} lacks world read"


def test_upload_overwrites_existing(client: TestClient, tmp_path: Path) -> None:
    client.post("/assets/acme/logo.png", content=b"v1-bytes" * 10, headers=BEARER)
    r = client.post("/assets/acme/logo.png", content=TINY_PNG, headers=BEARER)
    assert r.status_code == 200
    assert (tmp_path / ASSETS_SUBDIR / "acme" / "logo.png").read_bytes() == TINY_PNG


@pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"])
def test_upload_accepts_all_allowed_extensions(
    client: TestClient, tmp_path: Path, ext: str
) -> None:
    name = f"asset{ext}"
    r = client.post(f"/assets/acme/{name}", content=TINY_PNG, headers=BEARER)
    assert r.status_code == 200, r.text
    assert (tmp_path / ASSETS_SUBDIR / "acme" / name).is_file()


# ---------------------------------------------------------------------------
# Happy path: delete
# ---------------------------------------------------------------------------


def test_delete_removes_file_and_empty_client_dir(
    client: TestClient, tmp_path: Path
) -> None:
    client.post("/assets/acme/logo.png", content=TINY_PNG, headers=BEARER)
    r = client.delete("/assets/acme/logo.png", headers=BEARER)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] == "https://forge.baboons.dev/assets/acme/logo.png"
    assert not (tmp_path / ASSETS_SUBDIR / "acme" / "logo.png").exists()
    # client dir was empty, should be gone
    assert not (tmp_path / ASSETS_SUBDIR / "acme").exists()


def test_delete_keeps_nonempty_client_dir(client: TestClient, tmp_path: Path) -> None:
    client.post("/assets/acme/logo.png", content=TINY_PNG, headers=BEARER)
    client.post("/assets/acme/banner.jpg", content=TINY_PNG, headers=BEARER)
    r = client.delete("/assets/acme/logo.png", headers=BEARER)
    assert r.status_code == 200
    # Other file still there, so the dir survives.
    assert (tmp_path / ASSETS_SUBDIR / "acme").is_dir()
    assert (tmp_path / ASSETS_SUBDIR / "acme" / "banner.jpg").is_file()


def test_delete_nonexistent_returns_404(client: TestClient) -> None:
    r = client.delete("/assets/acme/nope.png", headers=BEARER)
    assert r.status_code == 404
    assert r.json()["error"] == "not found"


# ---------------------------------------------------------------------------
# Happy path: list
# ---------------------------------------------------------------------------


def test_list_returns_files_for_client(client: TestClient) -> None:
    client.post("/assets/acme/logo.png", content=TINY_PNG, headers=BEARER)
    client.post("/assets/acme/banner.jpg", content=b"jpeg-bytes" * 5, headers=BEARER)
    r = client.get("/assets/acme/", headers=BEARER)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["client"] == "acme"
    names = [a["name"] for a in body["assets"]]
    assert names == ["banner.jpg", "logo.png"]  # sorted
    sizes = {a["name"]: a["size"] for a in body["assets"]}
    assert sizes["logo.png"] == len(TINY_PNG)
    assert sizes["banner.jpg"] == 50
    for a in body["assets"]:
        assert a["url"] == f"https://forge.baboons.dev/assets/acme/{a['name']}"


def test_list_empty_for_nonexistent_client(client: TestClient) -> None:
    r = client.get("/assets/never-existed/", headers=BEARER)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "client": "never-existed", "assets": []}


def test_list_validates_client_slug(client: TestClient) -> None:
    r = client.get("/assets/FORGE/", headers=BEARER)
    assert r.status_code == 400
