# OpenBao in the stack: the master key outside Janus-Ops (FR-86)

The `openbao` profile of `deploy/compose.yaml` adds an OpenBao (raft storage, internal TLS) that **unseals itself**
on every restart with a static key (`seal "static"`). Janus-Ops uses it as the Transit engine (the
master key never leaves OpenBao) or as KV v2.

## First time

    scripts/openbao-setup.sh                      # keys, certificates, init, Transit, KV, AppRole, .env
    # in deploy/.env: COMPOSE_PROFILES=openbao (add your other profiles), JANUS_BAO_ADDR=https://openbao:8200,
    #                 JANUS_BAO_CA_FILE=/etc/janus-ops/openbao-tls/ca.crt
    make docker/run
    docker compose ... exec janus-ops janus-ops secrets migrate --to openbao-transit
    # then in deploy/.env: JANUS_SECRET_BACKEND=openbao-transit, and recreate janus-ops

The script is idempotent and prints no secrets. It writes to `deploy/secrets/openbao/` (ignored by git):
`seal.key` (the unseal key), `tls/` (CA and certificate), `init.json` (the recovery key; the root token
is revoked at the end of the setup) and, in `deploy/.env`, the `JANUS_BAO_*` values (the AppRole's role id
and secret id).

## What it protects and what it does not

- The unseal key lives on the **same host** as the data: it protects against a copy of the data volume
  alone, or a backup, not against whoever controls the host. This is the price of automatic unsealing
  (OpenBao advises against it unless another root of trust already exists). A stronger alternative:
  manual unsealing with Shamir keys held by people, or an external KMS, at the cost of an intervention
  on every restart.
- **Back up** `deploy/secrets/openbao/` (above all `seal.key` and `init.json`) **separately** from the
  `openbao_data` volume and from the database dump: without the unseal key the OpenBao data, and therefore
  every secret in Transit, is lost. Data snapshot: `bao operator raft snapshot save`.
- Transit key rotation: `bao write -f transit/keys/janus-kek/rotate`; old secrets stay readable. New
  secret id for the app: `scripts/openbao-setup.sh --new-secret-id`, then recreate janus-ops.

## If OpenBao is not there

Credentials, notification channels and TOTP cannot be read: the functions that use them answer
`vault_unavailable` and `/api/v1/ready` shows `secret_manager: unreachable`. Password login without TOTP
and the break-glass account keep working. To go back: `janus-ops secrets migrate --to local`
(`JANUS_SECRET_KEY` must still be set).
