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

Three build modes, auto-detected from the payload:

- **static** — no `package.json` and no `vite.config.*`. Files are served as-is.
- **next** — `package.json` includes a `next` dependency. Runs
  `pnpm install && pnpm run build`. Expects `next.config.js` with
  `output: "export"` so a static `out/` directory is produced.
- **vite** — `package.json` includes a `vite` dependency, OR a
  `vite.config.{js,ts,mjs,cjs}` is present. Runs
  `pnpm install && pnpm run build`. Expects a `dist/` directory.

The build output is resolved by checking `dist/` → `out/` → `build/` in
that order; the first directory that exists wins. `node_modules`,
`.vite`, `.next`, source files etc. stay in the staging tmpdir and are
never published — only the resolved output directory is moved into
`/var/www/demos/<slug>/`.

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

## Heavy demos: when LLM-driven `deploy_demo` doesn't fit

`deploy_demo` accepts files inline (text or base64-encoded binary) in the
JSON-RPC payload. When the demo is small — a hand-written `index.html` plus
maybe a stylesheet — Claude can construct that payload directly. But built
SPAs (Vite + React, Next export, etc.) often produce a `dist/` directory
that's hundreds of kilobytes once base64-encoded, which blows past Claude's
output token budget mid-payload.

For those, use **`scripts/upload-static-dir.py`** — an operator-side script
that walks a built directory, classifies files (text vs binary by
extension), and POSTs the whole payload directly to `forge.baboons.dev/mcp`.

For **real client images** (logos, photos, anything where base64 round-trips
through tool calls corrupt bytes), use the centralized asset host
described in [Client asset hosting](#client-asset-hosting) below — demos
reference `https://forge.baboons.dev/assets/<client>/<file>` instead of
shipping the binary inline.

| Use case | Pick |
|---|---|
| Small static demo, hand-written HTML, ≲100 KB total | Claude-driven `deploy_demo` (any chat) |
| Vite/Next.js **source** (server-side build), source ≲100 KB | Claude-driven `deploy_demo` (mobile, web, desktop — anywhere with the connector) |
| Built static directory with binary assets, anything that exceeds the LLM's output budget | `scripts/upload-static-dir.py` |
| Real client images / logos to embed in demos | `scripts/upload-asset.py` → reference the public URL from the demo |

The Claude-driven path for Vite source: send `package.json`,
`vite.config.ts`, `index.html`, the contents of `src/`, and any config
files (`tailwind.config.js`, `tsconfig.json`, …) as files to
`deploy_demo`. Forge runs `pnpm install && pnpm run build` server-side,
publishes `dist/`. pnpm caches its content-addressed store under
`<demos_root>/.forge/pnpm-home/.local/share/pnpm/` (the unit redirects
`$HOME` there because `ProtectHome=true` blocks the default location).
The cache survives across deploys, so warm builds finish in seconds.

Measured on the production VPS (Vite + React + Tailwind, 8-file
project): cold deploy ~10 s, warm deploy ~5 s, end-to-end including
upload, MCP handshake, pnpm install, vite build, atomic swap, Caddy
reload.

Caveat: pnpm install can run long on heavier dep trees, and Claude.ai's
MCP connector enforces a tool-call timeout (~60–120 s in our testing).
If you hit a timeout on a fat dep tree, wait 30 s and call `list_demos`
to see whether the deploy landed before declaring failure — the server
keeps working even after the client gives up.

### Usage

```bash
# Pull the ops bearer from the VPS env file (one-time)
export FORGE_AUTH_TOKEN=$(ssh -i ~/.ssh/id_ed25519 root@134.209.207.63 \
  'sudo grep FORGE_AUTH_TOKEN /etc/baboons-demo-forge/forge.env | cut -d= -f2')

# Build your demo locally
cd ~/path/to/demo && pnpm build   # produces ./dist or ./out

# Upload
python3 scripts/upload-static-dir.py \
  --dist-dir ./dist \
  --slug acme-pitch
```

The script does the proper MCP three-step handshake (`initialize` →
`notifications/initialized` → `tools/call`, carrying the `Mcp-Session-Id`
header through). The earlier shortcut of POSTing `tools/call` directly fails
with `400 Missing session ID` — the handshake is non-optional.

Authenticates using the static `FORGE_AUTH_TOKEN` (the ops side door); does
not go through OAuth. Uses Python stdlib only — no `pip install` needed.

## Client asset hosting

Real client images (logos, headshots, product photos) lose bytes when
shoved through Claude's tool-call layer as base64 — the encoder is
lossless but the chat transport occasionally corrupts the string mid-flight,
and the result looks like a broken image in the deployed demo. The fix is
to **bypass the chat path entirely**: upload the binary over plain HTTP to a
shared asset host, then have the demo reference the public URL.

### Layout

```
/var/www/demos/.assets/<client>/<filename>
```

Sits alongside demo slug directories. The leading dot keeps it out of
`list_demos` (which filters dot-prefixed dirs). A client can exist as an
asset folder without a deployed demo — the two are decoupled. Client slugs
follow the same rules as demo slugs (see [Slug rules](#slug-rules)).

### URL pattern

```
https://forge.baboons.dev/assets/<client>/<filename>
```

Caddy serves these directly via `file_server` with `Cache-Control:
public, max-age=31536000, immutable`. SVGs are served with
`Content-Security-Policy: script-src 'none'; sandbox` and
`X-Content-Type-Options: nosniff` so any in-file JS can't run.

### Endpoints

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/assets/<client>/<filename>` | Bearer | Body is raw image bytes (10 MB cap). Returns `{ok, url, size}`. |
| `DELETE` | `/assets/<client>/<filename>` | Bearer | Removes the file; drops the client dir if empty. |
| `GET` | `/assets/<client>/` (trailing slash) | Bearer | Lists files for a client. |
| `GET` | `/assets/<client>/<filename>` | Public | Served by Caddy, not the Python app. |

POST/DELETE/list use the static `FORGE_AUTH_TOKEN` only — they are **not**
exposed as MCP tools. That's the point: surfacing them as tool calls
would re-introduce the base64-corruption problem we're fixing.

Allowed extensions: `.png`, `.jpg`, `.jpeg`, `.webp`, `.svg`, `.gif`.

### Usage

```bash
# Same one-time token export as for upload-static-dir.py
export FORGE_AUTH_TOKEN=$(ssh -i ~/.ssh/id_ed25519 root@134.209.207.63 \
  'sudo grep FORGE_AUTH_TOKEN /etc/baboons-demo-forge/forge.env | cut -d= -f2')

# Single file
python3 scripts/upload-asset.py --client kluytmans --file ./logo.png
# → https://forge.baboons.dev/assets/kluytmans/logo.png

# Single file with a different remote name
python3 scripts/upload-asset.py --client kluytmans --file ./logo.png --name brand.png

# Bulk (non-recursive — only image files in the directory)
python3 scripts/upload-asset.py --client kluytmans --dir ./logos/
```

Demos then reference the URLs directly:

```html
<img src="https://forge.baboons.dev/assets/kluytmans/logo.png" alt="Kluytmans">
```

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
  .assets/<client>/<file>         centralized asset host (mode 2755, setgid)
/etc/baboons-demo-forge/
  forge.env                       FORGE_AUTH_TOKEN + settings (mode 640)
/etc/systemd/system/
  baboons-demo-forge.service
/etc/caddy/Caddyfile              wildcard *.baboons.dev block + forge.baboons.dev
```

## Maintenance

One-off setup tasks (idempotent, safe to re-run):

| Script | What it does |
|---|---|
| `scripts/setup-vps.sh` | Bootstraps the VPS: deploy user, demos root, env file, sudoers. |
| `scripts/migrate-caddy-to-sitesd.sh` | Migrates the legacy Caddyfile layout to `/etc/caddy/sites.d/`. |
| `scripts/migrate-assets-dir.sh` | Creates / repairs `/var/www/demos/.assets` (mode 2755, owner baboons-deploy). Run this once before the first `upload-asset.py` invocation. |

The `forge.baboons.dev` Caddy snippet is the source of truth at
`deploy/caddy-snippet.conf`. After editing it, copy the updated
`forge.baboons.dev { … }` block over `/etc/caddy/sites.d/forge.baboons.dev.conf`
on the VPS, then `caddy validate && systemctl reload caddy`.

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
