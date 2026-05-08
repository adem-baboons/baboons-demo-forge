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

- Single shared bearer token, stored only in `/etc/baboons-demo-forge/forge.env`
  (mode `640`, `root:baboons-deploy`). The token is never logged or echoed back.
- Token comparison is constant-time (`hmac.compare_digest`).
- The MCP server listens on `127.0.0.1:8090`. TLS termination + public ingress
  is handled by Caddy at `https://forge.baboons.dev/mcp`.
- Service runs as the `baboons-deploy` user with a hardened systemd unit
  (`ProtectSystem=strict`, `ReadWritePaths=/var/www/demos /etc/caddy/sites.d`,
  `ProtectHome=true`, `PrivateTmp=true`, `RestrictSUIDSGID=true`, …).
- Deploys cannot escape `/var/www/demos/<slug>/`. Absolute paths and
  traversal segments are rejected before any file is written.
- **Privilege-escalation tradeoff:** `NoNewPrivileges=true` is intentionally
  NOT set so the service can run `sudo systemctl reload caddy` after writing
  a snippet. The blast radius of that allowance is bounded by
  `/etc/sudoers.d/baboons-demo-forge-caddy`, which permits exactly one
  command — `/usr/bin/systemctl reload caddy` — and nothing else. A polkit
  rule allowing reload via dbus would let us keep `NoNewPrivileges=true`,
  but was rejected for setup complexity. The unit file documents this
  alongside the directive.

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
