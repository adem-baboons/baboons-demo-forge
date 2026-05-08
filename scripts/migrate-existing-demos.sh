#!/usr/bin/env bash
# Migrate the two pre-existing demos from <slug>.demos.baboons.dev URLs
# to <slug>.baboons.dev URLs.
#
# Both demos already live in /var/www/demos/<slug>/ on disk — only the
# Caddy routing changes. This script's job is to:
#   1. confirm the on-disk dirs exist (idempotent — does NOT move files)
#   2. print verification curl commands for the new URLs
#
# Run after the new Caddy snippet is installed and reloaded. Run as root
# (or as a user that can read /var/www/demos).
#
# Usage:
#   ssh root@$VPS_HOST 'bash -s' < scripts/migrate-existing-demos.sh

set -euo pipefail

DEMOS_ROOT="/var/www/demos"
SLUGS=("lupker-makelaardij" "leadlasers")

for slug in "${SLUGS[@]}"; do
  dir="${DEMOS_ROOT}/${slug}"
  if [[ ! -d "${dir}" ]]; then
    echo "WARN: ${dir} does not exist — skipping ${slug}"
    continue
  fi
  if [[ ! -f "${dir}/index.html" ]] && \
     ! find "${dir}" -maxdepth 1 -name 'index.htm*' -print -quit | grep -q .; then
    echo "WARN: no index.html in ${dir} — ${slug} may not serve a default page"
  fi
  echo "OK   : ${dir} ready to serve from https://${slug}.baboons.dev"
done

echo
echo "==> verification (run from your laptop or anywhere outside the VPS):"
for slug in "${SLUGS[@]}"; do
  echo "    curl -sI https://${slug}.baboons.dev | head -1"
done
echo
echo "==> the OLD URLs will return 404 — that is the intended breaking change:"
for slug in "${SLUGS[@]}"; do
  echo "    https://${slug}.demos.baboons.dev   (will 404)"
done
