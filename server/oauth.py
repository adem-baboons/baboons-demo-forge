"""Persistent OAuth 2.1 Authorization Server for the Forge.

Subclasses FastMCP's ``InMemoryOAuthProvider`` with two additions:

1. **Disk persistence.** Clients, auth codes, access tokens, refresh
   tokens are saved to ``state_path`` (JSON, mode 0600) on every
   mutation and loaded at startup. Without this, the GHA-driven
   service restarts would force Claude.ai to re-register and re-auth
   each time.

2. **DCR gating.** ``dcr_enabled`` toggles the ``/register`` endpoint.
   Default ON for first-time setup; an admin flips it off after the
   first successful registration to close the door against drive-by
   registrations.

Threat model
------------
With DCR open and no consent UI in front of ``/authorize``, anyone
reachable to ``forge.baboons.dev`` can register a client, complete
the OAuth flow, and call ``deploy_demo`` — i.e. arbitrary file upload
and build execution on the VPS. This class assumes the operator
toggles ``dcr_enabled`` to ``False`` immediately after Claude.ai has
registered itself.

State file format (``oauth-state.json``)
----------------------------------------
A single JSON object with four top-level keys: ``clients``,
``auth_codes``, ``access_tokens``, ``refresh_tokens``. Each maps a
string key (client_id / code / token) to a serialised pydantic model
of the corresponding MCP SDK type. Containing ACCESS TOKENS and
REFRESH TOKENS in cleartext, so the file is mode 0600 and stored
inside the same directory tree as ``/var/www/demos/.forge/`` (already
covered by the systemd unit's ReadWritePaths).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    RefreshToken,
    RegistrationError,
)
from mcp.server.auth.settings import (
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.shared.auth import OAuthClientInformationFull

log = logging.getLogger("forge.oauth")


class PersistentOAuthProvider(InMemoryOAuthProvider):
    """OAuth Authorization Server + Resource Server with disk-backed state.

    Wraps ``InMemoryOAuthProvider`` so we don't reimplement the OAuth 2.1
    flow itself; we add persistence around its four state dicts plus a
    flag that disables ``/register`` once the operator has finished
    pairing Claude.ai.
    """

    def __init__(
        self,
        *,
        state_path: Path,
        base_url: str,
        dcr_enabled: bool,
        required_scopes: list[str] | None = None,
    ):
        super().__init__(
            base_url=base_url,
            resource_base_url=base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["forge"],
                default_scopes=["forge"],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=required_scopes or ["forge"],
        )
        self._state_path = state_path
        self._dcr_enabled = dcr_enabled
        self._save_lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._state_path.exists():
            log.info(
                "forge.oauth: no prior state at %s — starting fresh",
                self._state_path,
            )
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.error(
                "forge.oauth: state file %s exists but unreadable (%s); "
                "ignoring — existing tokens will be invalid",
                self._state_path,
                exc,
            )
            return
        self.clients = {
            cid: OAuthClientInformationFull.model_validate(c)
            for cid, c in data.get("clients", {}).items()
        }
        self.auth_codes = {
            k: AuthorizationCode.model_validate(v)
            for k, v in data.get("auth_codes", {}).items()
        }
        self.access_tokens = {
            k: AccessToken.model_validate(v)
            for k, v in data.get("access_tokens", {}).items()
        }
        self.refresh_tokens = {
            k: RefreshToken.model_validate(v)
            for k, v in data.get("refresh_tokens", {}).items()
        }
        log.info(
            "forge.oauth: loaded %d clients, %d access tokens, %d refresh "
            "tokens from %s",
            len(self.clients),
            len(self.access_tokens),
            len(self.refresh_tokens),
            self._state_path,
        )

    def _save(self) -> None:
        with self._save_lock:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            payload = {
                "clients": {
                    k: v.model_dump(mode="json") for k, v in self.clients.items()
                },
                "auth_codes": {
                    k: v.model_dump(mode="json")
                    for k, v in self.auth_codes.items()
                },
                "access_tokens": {
                    k: v.model_dump(mode="json")
                    for k, v in self.access_tokens.items()
                },
                "refresh_tokens": {
                    k: v.model_dump(mode="json")
                    for k, v in self.refresh_tokens.items()
                },
            }
            tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
            os.replace(tmp, self._state_path)
            os.chmod(self._state_path, 0o600)

    # ------------------------------------------------------------------
    # DCR gating
    # ------------------------------------------------------------------

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not self._dcr_enabled:
            log.warning(
                "forge.oauth: rejected DCR attempt — FORGE_DCR_ENABLED=0 "
                "(known clients: %d)",
                len(self.clients),
            )
            # Generic on purpose: this endpoint is unauthenticated, so the reply must not
            # name config files or variables. The log line above tells the operator what to do.
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="dynamic client registration is disabled",
            )
        await super().register_client(client_info)
        log.info(
            "forge.oauth: registered client %s (total: %d). "
            "Recommended next step: set FORGE_DCR_ENABLED=0 and restart.",
            client_info.client_id,
            len(self.clients),
        )
        self._save()

    # ------------------------------------------------------------------
    # Persist after every state-mutating method exposed by the SDK
    # ------------------------------------------------------------------

    async def authorize(self, client, params):
        result = await super().authorize(client, params)
        self._save()
        return result

    async def exchange_authorization_code(self, client, authorization_code):
        out = await super().exchange_authorization_code(client, authorization_code)
        self._save()
        return out

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        out = await super().exchange_refresh_token(client, refresh_token, scopes)
        self._save()
        return out

    async def revoke_token(self, token):
        out = await super().revoke_token(token)
        self._save()
        return out
