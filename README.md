# Baboons Demo Forge

A small MCP server that lets any Claude client — web, mobile, Code — deploy
speculative client demos to `https://<slug>.baboons.dev` via tool calls.

## What it does

Exposes four MCP tools over Streamable HTTP:

| Tool             | What it does                                                        |
| ---------------- | ------------------------------------------------------------------- |
| `deploy_demo`    | Writes a payload of files to `/var/www/demos/<slug>/`, runs a build if needed, returns the public URL. |
| `list_demos`     | Lists every currently-deployed demo with its URL.                   |
| `delete_demo`    | Permanently removes a demo. Requires `confirm=True`.                |
| `get_build_logs` | Returns the last build log for a slug — useful for debugging.       |

Two build modes, auto-detected from the payload:

- **static** — no `package.json`. Files are served as-is.
- **next** — `package.json` includes a `next` dependency. Runs
  `pnpm install && pnpm run build`. Expects `next.config.js` with
  `output: "export"` so a static `out/` directory is produced.

## URL pattern

Every demo lives at the apex level: `https://<slug>.baboons.dev`. Routing is
per-slug: each successful `deploy_demo` writes a Caddy site block to
`/etc/caddy/sites.d/<slug>.baboons.dev.conf` and reloads Caddy. The top-level
Caddyfile contains a single `import /etc/caddy/sites.d/*.conf` directive.

> **Failure mode:** if a Caddy reload fails (e.g. another snippet is malformed),
> the failing snippet is rolled back, but the demo files at
> `/var/www/demos/<slug>/` stay in place so the deploy can be retried. Orphaned
> demo files require manual cleanup if no retry follows.

## Slug rules

- 2–30 characters, lowercase alphanumeric and hyphens.
- No leading or trailing hyphen.
- Cannot match the reserved blocklist (`sales`, `deploy`, `trades`, `stax`,
  `api`, `diwan`, `diwantravel`, `forge`, `www`, `mail`, …).

## Limits

- Max 100 files per deploy.
- Max 5 MB per file, 50 MB total.
- File paths must be relative — no leading `/`, no `..`. Path traversal is
  rejected before anything is written.

## Security model

### Authentication

The server accepts two parallel auth paths via `MultiAuth`:

1. **OAuth 2.1 Authorization Server (Claude.ai's path).** Forge acts as both
   resource server and authorization server. Endpoints mounted automatically
   by FastMCP: `/.well-known/oauth-authorization-server`,
   `/.well-known/oauth-protected-resource/mcp`, `/register`, `/authorize`,
   `/token`, `/revoke`. State (clients, codes, access tokens, refresh tokens)
   is persisted to `<demos_root>/.forge/oauth-state.json` (mode `0600`) so
   restarts don't invalidate Claude's session.

2. **Static bearer side-door (ops only).** `FORGE_AUTH_TOKEN` from
   `/etc/baboons-demo-forge/forge.env` (mode `640`, `root:baboons-deploy`).
   Used by curl-based smoke tests and any offline ops scripting; not
   advertised in OAuth metadata. Constant-time comparison, never logged.

### DCR is gated by `FORGE_DCR_ENABLED`

Without gating, anyone reaching `https://forge.baboons.dev/register` could
register an OAuth client, complete the flow, and call `deploy_demo` — i.e.
arbitrary file upload + build execution on the VPS (the standard "free
build server" abuse pattern).

`FORGE_DCR_ENABLED=1` opens `/register`; `FORGE_DCR_ENABLED=0` returns a
structured RFC 7591 error pointing the caller at the env file. Workflow:

1. First-time setup: `FORGE_DCR_ENABLED=1`, restart service, add the MCP
   server in Claude (it auto-registers via DCR).
2. Once `forge.oauth: registered client …` appears in `journalctl`, edit
   `/etc/baboons-demo-forge/forge.env` to `FORGE_DCR_ENABLED=0` and
   `systemctl restart baboons-demo-forge`. The persisted client survives
   the restart; `/register` is now closed.
3. To register a second client (new device, etc.): flip back to `1`,
   restart, register, flip back to `0`, restart.

### Networking + system hardening

- The MCP server listens on `127.0.0.1:8090`. TLS termination + public
  ingress is handled by Caddy at `https://forge.baboons.dev/`.
- Service runs as `baboons-deploy` with a hardened systemd unit:
  `ProtectSystem=strict`, `ReadWritePaths=/var/www/demos -/etc/caddy/sites.d`,
  `ProtectHome=true`, `PrivateTmp=true`, `RestrictSUIDSGID=true`, …
- Deploys cannot escape `/var/www/demos/<slug>/`. Absolute paths and
  traversal segments are rejected before any file is written.

### Privilege-escalation tradeoff

`NoNewPrivileges=true` is intentionally NOT set so the service can run
`sudo systemctl reload caddy` after writing a snippet. The blast radius of
that allowance is bounded by `/etc/sudoers.d/baboons-demo-forge-caddy`,
which permits exactly one command — `/usr/bin/systemctl reload caddy` — and
nothing else. A polkit rule allowing reload via dbus would let us keep
`NoNewPrivileges=true`, but was rejected for setup complexity. The unit
file documents this alongside the directive.

### Soft spots worth knowing

- `/authorize` has no human-consent UI; whoever hits it with a registered
  `client_id` mints an auth code immediately. The real gate is DCR (above)
  + the `client_secret` issued at registration time. If an attacker
  obtains a registered `client_id`+`client_secret` (e.g. via
  `oauth-state.json` exfiltration), they can issue tokens. File mode 0600
  keeps that to root + `baboons-deploy`.
- `oauth-state.json` contains access tokens and refresh tokens in
  cleartext. Threat model assumes the VPS file system is trusted.

## Local dev

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest                                   # 59 tests, fast

# Boot the server locally (uses /tmp instead of /var/www/demos)
FORGE_AUTH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
FORGE_DEMOS_ROOT=/tmp/forge-dev-demos \
.venv/bin/python -m server.main
# → http://127.0.0.1:8090/mcp
```

## Production layout

```
/opt/baboons-demo-forge/         repo (git fetched by GHA)
  .venv/                          local venv created on first deploy
  server/                         python source
/var/www/demos/                   served by Caddy as <slug>.baboons.dev
  .forge/logs/<slug>.log          captured build logs
/etc/baboons-demo-forge/
  forge.env                       FORGE_AUTH_TOKEN + settings (mode 640)
/etc/systemd/system/
  baboons-demo-forge.service
/etc/caddy/Caddyfile              wildcard *.baboons.dev block + forge.baboons.dev
```

## Deploy flow

1. Push to `main` → GitHub Actions runs `pytest`.
2. If tests pass, the workflow SSHes to the VPS as `baboons-deploy`,
   `git fetch`/`reset --hard`, syncs deps, and `systemctl restart`s the service.
3. Post-deploy `curl http://127.0.0.1:8090/health` confirms the new build is up.

## Connecting Claude

In Claude (web/mobile/desktop), add a custom integration:

- URL: `https://forge.baboons.dev/mcp`
- Leave the "OAuth Client ID" and "Client Secret" fields blank — Forge
  supports Dynamic Client Registration; Claude will create its own client.

Operator workflow:

1. On the VPS, confirm `FORGE_DCR_ENABLED=1` in
   `/etc/baboons-demo-forge/forge.env` and the service is up.
2. Click *Add* in the Claude integrations dialog.
3. A browser tab opens to `https://forge.baboons.dev/authorize?…` and
   redirects back with an auth code. Token exchange happens automatically.
4. Watch `journalctl -u baboons-demo-forge -f` for
   `forge.oauth: registered client <id>`.
5. Set `FORGE_DCR_ENABLED=0` and `systemctl restart baboons-demo-forge`
   to close the registration door.

After this, `deploy_demo`, `list_demos`, `delete_demo`, `get_build_logs`
appear as available tools in any Claude chat.

For curl-based ops or smoke tests, the static `FORGE_AUTH_TOKEN` still
works as an `Authorization: Bearer` header, parallel to OAuth.
