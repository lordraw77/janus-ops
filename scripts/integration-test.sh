#!/usr/bin/env bash
# Run backend integration tests against throwaway TimescaleDB and lldap containers.
# lldap is seeded from backend/tests/fixtures/ldap with lldap's own bootstrap.sh.
# Usage: scripts/integration-test.sh [extra pytest args]
set -euo pipefail

TIMESCALE_IMAGE="${TIMESCALE_IMAGE:-timescale/timescaledb:2.32.0-pg16}"
LLDAP_IMAGE="${LLDAP_IMAGE:-lldap/lldap:v0.6.3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURES="$ROOT/backend/tests/fixtures/ldap"
DB="janus-ops-itest-db-$$"
LDAP="janus-ops-itest-ldap-$$"

rand_hex() { od -An -N16 -tx1 /dev/urandom | tr -d ' \n'; }
PG_PASSWORD="itest-$(rand_hex)"
LLDAP_ADMIN_PASSWORD="itest-$(rand_hex)"

cleanup() { docker rm -f "$DB" "$LDAP" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --rm --name "$DB" \
  -e POSTGRES_USER=janus -e POSTGRES_PASSWORD="$PG_PASSWORD" -e POSTGRES_DB=janus \
  -p 127.0.0.1::5432 "$TIMESCALE_IMAGE" >/dev/null

docker run -d --rm --name "$LDAP" \
  -e LLDAP_JWT_SECRET="$(rand_hex)" -e LLDAP_KEY_SEED="$(rand_hex)" \
  -e LLDAP_LDAP_BASE_DN=dc=janus-ops,dc=local -e LLDAP_LDAP_USER_PASS="$LLDAP_ADMIN_PASSWORD" \
  -p 127.0.0.1::3890 -p 127.0.0.1::17170 "$LLDAP_IMAGE" >/dev/null

# TimescaleDB restarts postgres once after init: require two consecutive successful probes
ok=0
for _ in $(seq 1 60); do
  if docker exec "$DB" pg_isready -U janus -d janus -h 127.0.0.1 >/dev/null 2>&1; then
    ok=$((ok + 1)); [ "$ok" -ge 2 ] && break
  else
    ok=0
  fi
  sleep 1
done
[ "$ok" -ge 2 ] || { echo "TimescaleDB did not become ready" >&2; docker logs "$DB" >&2; exit 1; }

for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$LDAP")" = "healthy" ] && break
  sleep 1
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$LDAP")" = "healthy" ] \
  || { echo "lldap did not become healthy" >&2; docker logs "$LDAP" >&2; exit 1; }

docker cp "$FIXTURES" "$LDAP:/tmp/fixtures"
docker exec \
  -e LLDAP_URL=http://localhost:17170 -e LLDAP_ADMIN_PASSWORD="$LLDAP_ADMIN_PASSWORD" \
  -e USER_CONFIGS_DIR=/tmp/fixtures/users -e GROUP_CONFIGS_DIR=/tmp/fixtures/groups \
  -e USER_SCHEMAS_DIR=/tmp/fixtures/none -e GROUP_SCHEMAS_DIR=/tmp/fixtures/none \
  "$LDAP" sh -c 'mkdir -p /tmp/fixtures/none && /app/bootstrap.sh' >/dev/null

port() { docker port "$1" "$2/tcp" | head -1 | awk -F: '{print $NF}'; }
DB_PORT="$(port "$DB" 5432)"
LDAP_PORT="$(port "$LDAP" 3890)"
LLDAP_HTTP_PORT="$(port "$LDAP" 17170)"
export JANUS_TEST_DATABASE_URL="postgresql+asyncpg://janus:${PG_PASSWORD}@127.0.0.1:${DB_PORT}/janus"
export JANUS_TEST_LDAP_URL="ldap://127.0.0.1:${LDAP_PORT}"
export JANUS_TEST_LLDAP_HTTP="http://127.0.0.1:${LLDAP_HTTP_PORT}"
export JANUS_TEST_LLDAP_ADMIN_PASSWORD="$LLDAP_ADMIN_PASSWORD"

cd "$ROOT/backend"
uv run pytest -m integration "$@"
