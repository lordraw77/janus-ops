#!/usr/bin/env bash
# Set up the OpenBao of the stack (FR-86): keys and certificates, start, init, Transit and KV,
# policy, AppRole, and the JANUS_BAO_* values in deploy/.env. Idempotent: safe to run again.
# Secrets go to files (mode 600) and to deploy/.env; nothing is printed.
# Usage: scripts/openbao-setup.sh [--new-secret-id]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="$ROOT/deploy"
SECRETS="$DEPLOY/secrets/openbao"
ENVFILE="$DEPLOY/.env"
COMPOSE=(docker compose -f "$DEPLOY/compose.yaml" -f "$DEPLOY/compose.openbao.yaml" --env-file "$ENVFILE")
BAO_UID=100 BAO_GID=1000  # the user of the openbao image

[ -f "$ENVFILE" ] || { echo "deploy/.env missing: run 'make env' first" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl is needed" >&2; exit 1; }

# ---- keys and certificates (only if absent)
umask 077
mkdir -p "$SECRETS/tls"
if [ ! -f "$SECRETS/seal.key" ]; then
  openssl rand -out "$SECRETS/seal.key" 32
  echo "created the unseal key"
fi
if [ ! -f "$SECRETS/tls/ca.crt" ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
    -subj "/CN=Janus-Ops OpenBao CA" -keyout "$SECRETS/tls/ca.key" -out "$SECRETS/tls/ca.crt" 2>/dev/null
  echo "created the CA"
fi
if [ ! -f "$SECRETS/tls/server.crt" ]; then
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=openbao" \
    -keyout "$SECRETS/tls/server.key" -out "$SECRETS/tls/server.csr" 2>/dev/null
  printf 'subjectAltName=DNS:openbao,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' \
    > "$SECRETS/tls/san.cnf"
  openssl x509 -req -in "$SECRETS/tls/server.csr" -CA "$SECRETS/tls/ca.crt" \
    -CAkey "$SECRETS/tls/ca.key" -CAcreateserial -days 825 -extfile "$SECRETS/tls/san.cnf" \
    -out "$SECRETS/tls/server.crt" 2>/dev/null
  echo "created the server certificate"
fi
# the container runs as uid 100: it reads what it needs, nobody else does
chown -R "$BAO_UID:$BAO_GID" "$SECRETS/seal.key" "$SECRETS/tls/server.key" "$SECRETS/tls/server.crt" "$SECRETS/tls/ca.crt"
chmod 0400 "$SECRETS/seal.key" "$SECRETS/tls/server.key"
chmod 0444 "$SECRETS/tls/server.crt" "$SECRETS/tls/ca.crt"
chmod 0711 "$SECRETS" "$SECRETS/tls"  # traversable by the container user, not listable

# ---- start and wait until it answers
"${COMPOSE[@]}" up -d openbao >/dev/null
bao() { "${COMPOSE[@]}" exec -T -e BAO_TOKEN="${BAO_TOKEN:-}" openbao bao "$@"; }
for _ in $(seq 1 60); do
  st="$(bao status -format=json 2>/dev/null || true)"
  [ -n "$st" ] && break
  sleep 1
done
[ -n "${st:-}" ] || { echo "OpenBao did not answer" >&2; exit 1; }

json_field() { python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"; }

# ---- init (once): with a static seal it unseals itself, so only a recovery key and a root token
if [ "$(printf '%s' "$st" | json_field initialized)" != "True" ]; then
  bao operator init -recovery-shares=1 -recovery-threshold=1 -format=json > "$SECRETS/init.json"
  chmod 0600 "$SECRETS/init.json"
  echo "initialised (recovery key and root token in deploy/secrets/openbao/init.json)"
  sleep 3
fi
ROOT_TOKEN="$(python3 -c 'import json; print(json.load(open("'"$SECRETS"'/init.json")).get("root_token",""))' 2>/dev/null || true)"
if [ -z "$ROOT_TOKEN" ]; then
  echo "no root token in init.json (already revoked): the setup was done; use --new-secret-id" >&2
fi
export BAO_TOKEN="$ROOT_TOKEN"

if [ -n "$ROOT_TOKEN" ]; then
  bao secrets list -format=json | grep -q '"transit/"' || bao secrets enable transit >/dev/null
  bao read transit/keys/janus-kek >/dev/null 2>&1 || bao write -f transit/keys/janus-kek type=aes256-gcm96 >/dev/null
  bao secrets list -format=json | grep -q '"secret/"' || bao secrets enable -path=secret kv-v2 >/dev/null
  # the least the app needs: wrap/unwrap with one key, and its own prefix in KV
  bao policy write janus-ops - >/dev/null <<'POLICY'
path "transit/encrypt/janus-kek" { capabilities = ["update"] }
path "transit/decrypt/janus-kek" { capabilities = ["update"] }
path "secret/data/janus-ops/*"     { capabilities = ["create", "update", "read"] }
path "secret/metadata/janus-ops"   { capabilities = ["list"] }
path "secret/metadata/janus-ops/*" { capabilities = ["list", "read", "delete"] }
POLICY
  bao auth list -format=json | grep -q '"approle/"' || bao auth enable approle >/dev/null
  bao write auth/approle/role/janus-ops token_policies=janus-ops token_ttl=1h token_max_ttl=24h \
    secret_id_ttl=0 >/dev/null
fi

# ---- the app's role and secret id into deploy/.env (replaced if present, never printed)
if [ -n "$ROOT_TOKEN" ] && { ! grep -q '^JANUS_BAO_SECRET_ID=.\+' "$ENVFILE" || [ "${1:-}" = "--new-secret-id" ]; }; then
  ROLE_ID="$(bao read -field=role_id auth/approle/role/janus-ops/role-id)"
  SECRET_ID="$(bao write -f -field=secret_id auth/approle/role/janus-ops/secret-id)"
  python3 - "$ENVFILE" "$ROLE_ID" "$SECRET_ID" <<'PY'
import sys
path, role, secret = sys.argv[1:4]
want = {"JANUS_BAO_ADDR": "https://openbao:8200", "JANUS_BAO_ROLE_ID": role,
        "JANUS_BAO_SECRET_ID": secret}
lines, seen = open(path).read().splitlines(), set()
out = []
for line in lines:
    key = line.split("=", 1)[0]
    if key in want:
        out.append(f"{key}={want[key]}"); seen.add(key)
    else:
        out.append(line)
out += [f"{k}={v}" for k, v in want.items() if k not in seen]
open(path, "w").write("\n".join(out) + "\n")
PY
  echo "role and secret id written to deploy/.env"
fi

# ---- the root token is not kept: the recovery key can make another (bao operator generate-root)
if [ -n "$ROOT_TOKEN" ]; then
  bao token revoke -self >/dev/null
  python3 - "$SECRETS/init.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["root_token"] = ""
json.dump(d, open(p, "w"))
PY
  echo "root token revoked"
fi
echo "OpenBao is ready: JANUS_SECRET_BACKEND=openbao-transit (or openbao-kv) to use it"
