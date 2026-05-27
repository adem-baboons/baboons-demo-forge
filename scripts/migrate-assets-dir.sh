#!/usr/bin/env bash
# One-shot setup for the centralized asset hosting directory.
#
# What it does:
#   1. Creates /var/www/demos/.assets (mode 2755, owner baboons-deploy)
#   2. If the dir already exists with the wrong owner or mode, fixes it
#   3. Prints the final state
#
# Why mode 2755 (setgid):
#   - 0755 so Caddy (a different user) can traverse and read files
#   - setgid so files written by the forge service inherit the
#     baboons-deploy group, keeping the group consistent across processes
#
# Idempotent — safe to re-run. Run as root.
#
# Usage:
#   ssh root@$VPS_HOST 'bash -s' < scripts/migrate-assets-dir.sh

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

DEPLOY_USER="baboons-deploy"
ASSETS_DIR="/var/www/demos/.assets"
WANT_OWNER="${DEPLOY_USER}:${DEPLOY_USER}"
WANT_MODE="2755"

if ! id "${DEPLOY_USER}" &>/dev/null; then
  echo "ERROR: user '${DEPLOY_USER}' does not exist on this host" >&2
  exit 1
fi

if [[ -e "${ASSETS_DIR}" && ! -d "${ASSETS_DIR}" ]]; then
  echo "ERROR: ${ASSETS_DIR} exists but is not a directory" >&2
  exit 1
fi

if [[ ! -d "${ASSETS_DIR}" ]]; then
  echo "==> creating ${ASSETS_DIR} (owner ${WANT_OWNER}, mode ${WANT_MODE})"
  install -d -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" -m "${WANT_MODE}" "${ASSETS_DIR}"
else
  echo "==> ${ASSETS_DIR} already exists — checking owner/mode"

  CUR_OWNER=$(stat -c '%U:%G' "${ASSETS_DIR}")
  if [[ "${CUR_OWNER}" != "${WANT_OWNER}" ]]; then
    echo "  fixing owner: ${CUR_OWNER} → ${WANT_OWNER}"
    chown "${WANT_OWNER}" "${ASSETS_DIR}"
  else
    echo "  owner ok (${CUR_OWNER})"
  fi

  CUR_MODE=$(stat -c '%a' "${ASSETS_DIR}")
  if [[ "${CUR_MODE}" != "${WANT_MODE}" ]]; then
    echo "  fixing mode: ${CUR_MODE} → ${WANT_MODE}"
    chmod "${WANT_MODE}" "${ASSETS_DIR}"
  else
    echo "  mode ok (${CUR_MODE})"
  fi
fi

echo
echo "==> final state:"
ls -lad "${ASSETS_DIR}"
