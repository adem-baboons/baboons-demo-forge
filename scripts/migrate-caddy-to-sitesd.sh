#!/usr/bin/env bash
# One-shot migration to a /etc/caddy/sites.d/-based layout.
#
# What it does:
#   1. Backs up /etc/caddy/Caddyfile to /etc/caddy/Caddyfile.bak.<timestamp>
#   2. Creates /etc/caddy/sites.d/ owned by baboons-deploy
#   3. Generates three migration snippets:
#        - lupker-makelaardij.baboons.dev.conf  (was: lupker-makelaardij.demos…)
#        - leadlasers.baboons.dev.conf          (was: leadlasers.demos…)
#        - forge.baboons.dev.conf               (new: reverse proxy to localhost:8090)
#   4. Removes the two old <slug>.demos.baboons.dev blocks from the Caddyfile
#   5. Adds `import /etc/caddy/sites.d/*.conf` at the top
#   6. caddy validate, caddy reload
#   7. Installs sudoers rule allowing baboons-deploy to systemctl reload caddy
#
# Idempotent — safe to re-run. Run as root.
#
# Usage:
#   ssh root@$VPS_HOST 'bash -s' < scripts/migrate-caddy-to-sitesd.sh

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root" >&2
  exit 1
fi

CADDYFILE="/etc/caddy/Caddyfile"
SITES_DIR="/etc/caddy/sites.d"
TS=$(date +%s)
BACKUP="${CADDYFILE}.bak.${TS}"

# 1. Backup
echo "==> backing up ${CADDYFILE} to ${BACKUP}"
cp "${CADDYFILE}" "${BACKUP}"

# 2. Create sites.d
echo "==> ensuring ${SITES_DIR} exists, owned by baboons-deploy"
install -d -o baboons-deploy -g baboons-deploy -m 755 "${SITES_DIR}"

# 3. Generate migration snippets
write_snippet() {
  local slug="$1"
  local file="${SITES_DIR}/${slug}.baboons.dev.conf"
  cat > "${file}" <<EOF
${slug}.baboons.dev {
    root * /var/www/demos/${slug}
    try_files {path} {path}/index.html /index.html
    file_server
    encode zstd gzip

    @assets path /assets/* /images/*
    header @assets Cache-Control "public, max-age=31536000, immutable"

    @html path /index.html /
    header @html Cache-Control "no-cache, must-revalidate"
}
EOF
  chown baboons-deploy:baboons-deploy "${file}"
  chmod 644 "${file}"
  echo "  wrote ${file}"
}

write_snippet lupker-makelaardij
write_snippet leadlasers

FORGE_FILE="${SITES_DIR}/forge.baboons.dev.conf"
cat > "${FORGE_FILE}" <<'EOF'
forge.baboons.dev {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8090
}
EOF
chown baboons-deploy:baboons-deploy "${FORGE_FILE}"
chmod 644 "${FORGE_FILE}"
echo "  wrote ${FORGE_FILE}"

# 4-5. Edit the main Caddyfile in Python so the block boundaries are honoured.
python3 - "${CADDYFILE}" <<'PYEOF'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
src = p.read_text()

# Drop top-level blocks for "<slug>.demos.baboons.dev". The block body may
# contain Caddy placeholders like {path} which embed a single level of
# nested braces — naive [^}]* stops at the first inner `}`. The pattern
# below allows one level of nesting, which covers all known Caddyfile
# placeholders ({path}, {labels.2}, {http.error.status_code}, etc.).
new = re.sub(
    r"^[a-z0-9][a-z0-9-]*\.demos\.baboons\.dev \{(?:[^{}]|\{[^{}]*\})*\}\s*\n",
    "",
    src,
    flags=re.MULTILINE,
)

# Prepend the import directive if it isn't already there.
import_line = "import /etc/caddy/sites.d/*.conf"
if import_line not in new:
    new = f"{import_line}\n\n{new}"

if new != src:
    p.write_text(new)
    print("  Caddyfile updated")
else:
    print("  Caddyfile already migrated — nothing to do")
PYEOF

# 6. Validate and reload
echo "==> caddy validate"
caddy validate --config "${CADDYFILE}"

echo "==> systemctl reload caddy"
systemctl reload caddy

# 7. Sudoers rule for the forge service to reload caddy
SUDOERS="/etc/sudoers.d/baboons-demo-forge-caddy"
if [[ ! -f "${SUDOERS}" ]]; then
  echo "==> installing ${SUDOERS}"
  cat > "${SUDOERS}" <<'EOF'
baboons-deploy ALL=(root) NOPASSWD: /usr/bin/systemctl reload caddy
EOF
  chmod 440 "${SUDOERS}"
  visudo -cf "${SUDOERS}"
else
  echo "==> ${SUDOERS} already in place"
fi

echo
echo "==> migration done. Final state:"
ls -la "${SITES_DIR}"
echo
echo "If anything looks wrong, the previous Caddyfile is at ${BACKUP}."
