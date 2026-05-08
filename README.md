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

Every demo lives at the apex level: `https://<slug>.baboons.dev`. The wildcard
`*.baboons.dev` is served by Caddy with a Cloudflare DNS-01 cert. More-specific
sites (`forge.baboons.dev`, `sales.baboons.dev`, `stax.baboons.dev`, …) win
over the wildcard.

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

- Single shared bearer token, stored only in `/etc/baboons-demo-forge/forge.env`
  (mode `640`, `root:baboons-deploy`). The token is never logged or echoed back.
- Token comparison is constant-time (`hmac.compare_digest`).
- The MCP server listens on `127.0.0.1:8090`. TLS termination + public ingress
  is handled by Caddy at `https://forge.baboons.dev/mcp`.
- Service runs as the `baboons-deploy` user with a hardened systemd unit
  (`ProtectSystem=strict`, `ReadWritePaths=/var/www/demos`, no privilege escalation).
- Deploys cannot escape `/var/www/demos/<slug>/`. Absolute paths and
  traversal segments are rejected before any file is written.

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

In Claude (web/mobile/desktop), add an MCP server:

- URL: `https://forge.baboons.dev/mcp`
- Transport: Streamable HTTP
- Auth: Bearer `<FORGE_AUTH_TOKEN>`

Then call `deploy_demo` from any chat.
