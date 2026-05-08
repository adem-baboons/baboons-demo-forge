"""PersistentOAuthProvider — load/save round-trip, DCR gating, file mode."""
from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest
from mcp.server.auth.provider import RegistrationError
from mcp.shared.auth import OAuthClientInformationFull

from server.oauth import PersistentOAuthProvider


def _client_info(client_id: str = "client-a", **overrides) -> OAuthClientInformationFull:
    """Build a minimal pydantic OAuthClientInformationFull for tests."""
    base = dict(
        client_id=client_id,
        client_secret="s3cret",
        client_id_issued_at=1_700_000_000,
        client_secret_expires_at=None,
        redirect_uris=["https://claude.ai/callback"],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="forge",
    )
    base.update(overrides)
    return OAuthClientInformationFull.model_validate(base)


def _provider(state_path: Path, dcr_enabled: bool = True) -> PersistentOAuthProvider:
    return PersistentOAuthProvider(
        state_path=state_path,
        base_url="https://forge.baboons.dev",
        dcr_enabled=dcr_enabled,
    )


class TestPersistence:
    def test_initial_state_no_file(self, tmp_path: Path) -> None:
        sp = tmp_path / "oauth-state.json"
        provider = _provider(sp)
        assert provider.clients == {}
        assert not sp.exists(), "no save until something mutates state"

    def test_register_persists_to_disk(self, tmp_path: Path) -> None:
        sp = tmp_path / "oauth-state.json"
        provider = _provider(sp)
        info = _client_info("client-a")
        asyncio.run(provider.register_client(info))

        assert sp.exists()
        data = json.loads(sp.read_text())
        assert "client-a" in data["clients"]
        assert data["clients"]["client-a"]["client_secret"] == "s3cret"

    def test_state_file_is_0600(self, tmp_path: Path) -> None:
        sp = tmp_path / "oauth-state.json"
        provider = _provider(sp)
        asyncio.run(provider.register_client(_client_info("client-a")))
        mode = stat.S_IMODE(sp.stat().st_mode)
        assert mode == 0o600, f"expected mode 0o600, got {oct(mode)}"

    def test_load_restores_clients(self, tmp_path: Path) -> None:
        sp = tmp_path / "oauth-state.json"
        # Round 1: write
        provider1 = _provider(sp)
        asyncio.run(provider1.register_client(_client_info("client-a")))
        asyncio.run(provider1.register_client(_client_info("client-b")))

        # Round 2: fresh instance against the same file
        provider2 = _provider(sp)
        assert set(provider2.clients.keys()) == {"client-a", "client-b"}
        assert [str(u) for u in provider2.clients["client-a"].redirect_uris] == [
            "https://claude.ai/callback"
        ]

    def test_corrupt_state_file_does_not_crash_startup(self, tmp_path: Path) -> None:
        sp = tmp_path / "oauth-state.json"
        sp.write_text("{not valid json}")
        # _load() must not raise — it should log and proceed with empty state
        provider = _provider(sp)
        assert provider.clients == {}


class TestDCRGating:
    def test_register_allowed_when_enabled(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path / "s.json", dcr_enabled=True)
        asyncio.run(provider.register_client(_client_info("ok")))
        assert "ok" in provider.clients

    def test_register_rejected_when_disabled(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path / "s.json", dcr_enabled=False)
        with pytest.raises(RegistrationError) as exc:
            asyncio.run(provider.register_client(_client_info("blocked")))
        assert exc.value.error == "invalid_client_metadata"
        msg = (exc.value.error_description or "").lower()
        assert "disabled" in msg
        assert "forge_dcr_enabled=1" in msg.replace(" ", "_") or "forge_dcr_enabled" in msg.replace(" ", "")
        # Ensure no trace of the rejected registration on disk.
        assert "blocked" not in provider.clients

    def test_disabling_dcr_does_not_evict_existing_clients(self, tmp_path: Path) -> None:
        sp = tmp_path / "s.json"
        provider1 = _provider(sp, dcr_enabled=True)
        asyncio.run(provider1.register_client(_client_info("preexisting")))

        # Restart with DCR disabled
        provider2 = _provider(sp, dcr_enabled=False)
        assert "preexisting" in provider2.clients
        # And new registrations are still rejected
        with pytest.raises(RegistrationError):
            asyncio.run(provider2.register_client(_client_info("blocked")))


class TestProviderConstruction:
    def test_metadata_attrs_match_base_url(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path / "s.json")
        # FastMCP's HTTP transport reads these at startup.
        assert provider.base_url is not None
        assert str(provider.base_url) == "https://forge.baboons.dev/"

    def test_required_scopes_are_set(self, tmp_path: Path) -> None:
        provider = _provider(tmp_path / "s.json")
        assert provider.required_scopes == ["forge"]
