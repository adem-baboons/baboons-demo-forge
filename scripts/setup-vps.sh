#!/usr/bin/env bash
# Bootstrap the Demo Forge on the VPS. Run as root.
#
# Idempotent — safe to re-run. It will print the bearer token only when
# generating a new one. If /etc/baboons-demo-forge/forge.env already
# exists, the existing token is preserved and NOT printed.
#
# Usage:
#   ssh root@$VPS_HOST 'bash -s' < scripts/setup-vps.sh

set -euo pipefail

DEPLOY_USER="baboons-deploy"
DEMOS_ROOT="/var/www/demos"
LOGS_DIR="${DEMOS_ROOT}/.forge/logs"
ENV_DIR="/etc/baboons-demo-forge"
ENV_FILE="${ENV_DIR}/forge.env"
INSTALL_DIR="/opt/baboons-demo-forge"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

if ! id "${DEPLOY_USER}" &>/dev/null; then
  echo "ERROR: user '${DEPLOY_USER}' does not exist on this host" >&2
  exit 1
fi

echo "==> ensuring ${DEMOS_ROOT} exists"
mkdir -p "${DEMOS_ROOT}"
mkdir -p "${LOGS_DIR}"
# pnpm needs a store dir under ReadWritePaths (ProtectHome=true blocks
# the default $HOME/.pnpm-store). Co-locate with the rest of .forge state.
mkdir -p "${DEMOS_ROOT}/.forge/pnpm-store"
mkdir -p "${DEMOS_ROOT}/.forge/pnpm-home"

# Caddy reads files as the caddy user; the deploy user writes them.
# Make demos_root group-owned by the deploy user's group so caddy + deploy
# coexist via standard 0775 + setgid semantics.
echo "==> ensuring ${DEPLOY_USER} can write ${DEMOS_ROOT}"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${DEMOS_ROOT}"
chmod -R u=rwX,g=rX,o=rX "${DEMOS_ROOT}"
# setgid so newly-created subdirs inherit the group
find "${DEMOS_ROOT}" -type d -exec chmod g+s {} +

echo "==> ensuring ${ENV_DIR} exists"
mkdir -p "${ENV_DIR}"
chmod 750 "${ENV_DIR}"
chown root:"${DEPLOY_USER}" "${ENV_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  echo "==> ${ENV_FILE} already exists — leaving token in place"
else
  echo "==> generating bearer token (64 chars hex)"
  TOKEN="$(openssl rand -hex 32)"
  umask 077
  cat > "${ENV_FILE}" <<EOF
# Baboons Demo Forge — runtime env
# Generated $(date --iso-8601=seconds 2>/dev/null || date)
FORGE_AUTH_TOKEN=${TOKEN}
FORGE_DEMOS_ROOT=${DEMOS_ROOT}
FORGE_HOST=127.0.0.1
FORGE_PORT=8090
FORGE_DOMAIN=baboons.dev
FORGE_PUBLIC_URL=https://forge.baboons.dev
# DCR is open at first boot so Claude.ai can self-register via /register.
# Flip to 0 (and \`systemctl restart baboons-demo-forge\`) immediately after the
# first registration succeeds. See README's "DCR is gated" section.
FORGE_DCR_ENABLED=1
# FastMCP otherwise tries to write a version cache under \$HOME on startup,
# which collides with the unit's ProtectHome=true. The unit file also sets
# this — keeping it here too means env-file edits stay self-contained.
FASTMCP_CHECK_FOR_UPDATES=off
EOF
  chown root:"${DEPLOY_USER}" "${ENV_FILE}"
  chmod 640 "${ENV_FILE}"

  echo
  echo "================================================================"
  echo "  FORGE_AUTH_TOKEN (save this — it will not be shown again):"
  echo
  echo "    ${TOKEN}"
  echo
  echo "  Use it as the bearer token when configuring Forge as an MCP"
  echo "  server in Claude (URL: https://forge.baboons.dev/mcp)."
  echo "================================================================"
fi

echo "==> ensuring ${INSTALL_DIR} exists"
mkdir -p "${INSTALL_DIR}"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${INSTALL_DIR}"

# Permission for GHA to systemctl restart the service (no password).
SUDOERS_FILE="/etc/sudoers.d/baboons-demo-forge"
if [[ ! -f "${SUDOERS_FILE}" ]]; then
  echo "==> installing sudoers rule for ${DEPLOY_USER}"
  cat > "${SUDOERS_FILE}" <<EOF
${DEPLOY_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl restart baboons-demo-forge
${DEPLOY_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl reload baboons-demo-forge
EOF
  chmod 440 "${SUDOERS_FILE}"
  visudo -cf "${SUDOERS_FILE}"
else
  echo "==> ${SUDOERS_FILE} already in place"
fi

echo "==> done."
echo
echo "Next steps (manual on the VPS):"
echo "  1. As ${DEPLOY_USER}: clone the repo into ${INSTALL_DIR}"
echo "       git clone git@github.com:adem-baboons/baboons-demo-forge.git ${INSTALL_DIR}"
echo "  2. As ${DEPLOY_USER}: create venv and install deps"
echo "       cd ${INSTALL_DIR} && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
echo "  3. As root: install systemd unit"
echo "       cp ${INSTALL_DIR}/deploy/baboons-demo-forge.service /etc/systemd/system/"
echo "       systemctl daemon-reload && systemctl enable --now baboons-demo-forge"
echo "  4. As root: append ${INSTALL_DIR}/deploy/caddy-snippet.conf to /etc/caddy/Caddyfile"
echo "       caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy"
