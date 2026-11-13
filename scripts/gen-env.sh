#!/usr/bin/env bash
# Create deploy/.env from deploy/.env.example, filling the secrets with random hex.
# Idempotent: on an existing deploy/.env only missing or empty secrets are added
# (useful after an upgrade introduces a new one); nothing else is changed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXAMPLE="$ROOT/deploy/.env.example"
TARGET="$ROOT/deploy/.env"
SECRETS=(POSTGRES_PASSWORD LLDAP_JWT_SECRET LLDAP_KEY_SEED LLDAP_ADMIN_PASSWORD
  LDAP_BIND_PASSWORD LDAP_MANAGER_PASSWORD JANUS_SECRET_KEY JANUS_ADMIN_PASSWORD)

# 32 random bytes as hex; the initial admin password is shorter so it can be typed
rand_hex() { od -An -N"${1:-32}" -tx1 /dev/urandom | tr -d ' \n'; }
secret_for() { if [ "$1" = JANUS_ADMIN_PASSWORD ]; then rand_hex 10; else rand_hex 32; fi; }

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
    sed -i "s|^${key}=\$|${key}=$(secret_for "$key")|" "$TARGET"
    added+=("$key")
  elif ! grep -q "^${key}=" "$TARGET"; then
    printf '%s=%s\n' "$key" "$(secret_for "$key")" >> "$TARGET"
    added+=("$key")
  fi
done

if [ "$action" = "updated" ] && [ "${#added[@]}" -eq 0 ]; then
  echo "deploy/.env already complete: left unchanged"
else
  echo "deploy/.env $action; generated: ${added[*]:-none}"
  if [[ " ${added[*]} " == *" JANUS_ADMIN_PASSWORD "* ]]; then
    admin="$(sed -n 's/^JANUS_ADMIN_USERNAME=//p' "$TARGET")"
    echo "Initial administrator: ${admin:-janus-admin} /" \
      "password in deploy/.env (JANUS_ADMIN_PASSWORD)"
  fi
fi
