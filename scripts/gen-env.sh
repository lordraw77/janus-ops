#!/usr/bin/env bash
# Create deploy/.env from deploy/.env.example, filling the secrets with random hex.
# Idempotent: on an existing deploy/.env only missing or empty secrets are added
# (useful after an upgrade introduces a new one); nothing else is changed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXAMPLE="$ROOT/deploy/.env.example"
TARGET="$ROOT/deploy/.env"
SECRETS=(POSTGRES_PASSWORD LLDAP_JWT_SECRET LLDAP_KEY_SEED LLDAP_ADMIN_PASSWORD LDAP_BIND_PASSWORD)

rand_hex() { od -An -N32 -tx1 /dev/urandom | tr -d ' \n'; }

umask 077
if [ -e "$TARGET" ]; then
  action="updated"
else
  cp "$EXAMPLE" "$TARGET"
  action="created"
fi

added=()
for key in "${SECRETS[@]}"; do
  if grep -q "^${key}=\$" "$TARGET"; then
    sed -i "s|^${key}=\$|${key}=$(rand_hex)|" "$TARGET"
    added+=("$key")
  elif ! grep -q "^${key}=" "$TARGET"; then
    printf '%s=%s\n' "$key" "$(rand_hex)" >> "$TARGET"
    added+=("$key")
  fi
done

if [ "$action" = "updated" ] && [ "${#added[@]}" -eq 0 ]; then
  echo "deploy/.env already complete: left unchanged"
else
  echo "deploy/.env $action; generated: ${added[*]:-none}"
fi
