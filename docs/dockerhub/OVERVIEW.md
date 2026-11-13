<p align="center">
  <img src="https://raw.githubusercontent.com/lordraw77/janus-ops/main/res/logos/janus-ops-logo-512.png" alt="Janus-Ops" width="160">
</p>

<h1 align="center">Janus-Ops</h1>

<p align="center">
  <b>One portal for your whole infrastructure lifecycle.</b><br>
  Monitoring · Containers · Proxmox VE · SSH · Ansible · AI troubleshooting
</p>

---

> **Status: pre-alpha (0.1.x).** This image is the foundation of the project: control plane,
> web UI shell, LDAP sign-in with roles, audit log, database schema and health endpoints. The operational modules arrive phase by
> phase (see the [roadmap](https://github.com/lordraw77/janus-ops#project-status--roadmap)).
> Do not use it in production yet.

## What is Janus-Ops?

Janus-Ops replaces the daily jump between Checkmk, Portainer/Dockhand, the Proxmox VE UI,
MobaXterm and Ansible terminals with **one central inventory** and decoupled modules around it.
It is built for **segmented, air-gapped and proxy-restricted networks**: no runtime downloads,
self-hosted TLS, and a planned Go agent that only opens outbound connections.

| Module | Status |
|---|---|
| Control plane, web UI shell, database schema | ✅ available |
| LDAP sign-in, user / role / permission management, optional TOTP, break-glass, audit log | ✅ available |
| Inventory, credential vault | 🚧 phase F0 |
| SSH bastion (web terminal, SFTP) | phase F1 |
| `janus-agent`, telemetry, NOC dashboard | phase F2 |
| Alerting: thresholds, ack, downtimes, dependencies, notifications | phase F3 |
| Docker containers & Compose stacks | phase F4 |
| Proxmox VE clusters, VM/LXC, backup audit | phase F5 |
| Ansible automation | phase F6 |
| AI Copilot on self-hosted models | phase F7 |

## What is in this image

- **FastAPI** control plane (Python 3.12) and the **Next.js** web UI exported as static files,
  served by the same process.
- Runs as **non-root** (uid/gid `10001`), stateless (no volumes), ready for `read_only: true`.
- Listens on **port 8000** (plain HTTP): put it behind a TLS reverse proxy. The reference
  stack below uses Caddy.
- Built-in `HEALTHCHECK` on `/api/v1/health`.
- Platforms: `linux/amd64`, `linux/arm64`.

### Commands

The entrypoint is `janus-ops`:

| Command | Purpose |
|---|---|
| `serve` (default) | Start the HTTP server (API + web UI) |
| `migrate` | Apply database migrations up to the latest revision, then exit. Idempotent |
| `setup` | `migrate`, then create the initial administrator from `JANUS_ADMIN_*` if it does not exist |
| `breakglass` | Create the local emergency admin or rotate its password (prompts; `--password-stdin` for scripts) |
| `version` | Print the version and exit |

## Quick start (Docker Compose)

The reference stack runs **Caddy** (TLS) · **janus-ops** · **TimescaleDB** (PostgreSQL 16) ·
**lldap** (user directory), plus two one-shot jobs: **ldap-init** (creates the `janus-*` groups
and two service accounts) and **setup** (database schema and initial administrator).

```bash
mkdir janus-ops && cd janus-ops
base=https://raw.githubusercontent.com/lordraw77/janus-ops/main/deploy
curl -fsSLO "$base/compose.yaml"
curl -fsSLO "$base/Caddyfile"
curl -fsSL -o .env "$base/.env.example"

# Fill the required secrets with random hex values
for key in POSTGRES_PASSWORD LLDAP_JWT_SECRET LLDAP_KEY_SEED LLDAP_ADMIN_PASSWORD \
           LDAP_BIND_PASSWORD LDAP_MANAGER_PASSWORD JANUS_SECRET_KEY; do
  sed -i "s|^${key}=\$|${key}=$(openssl rand -hex 32)|" .env
done
# Password of the initial administrator "janus-admin" (or write your own, min. 12 chars)
sed -i "s|^JANUS_ADMIN_PASSWORD=\$|JANUS_ADMIN_PASSWORD=$(openssl rand -hex 10)|" .env
chmod 600 .env

docker compose up -d --wait
```

Open **https://localhost/**. By default Caddy issues the certificate from its own local CA
(`JANUS_TLS=internal`), so the browser warns about it until you trust that CA or set a real
domain and `JANUS_TLS=<your e-mail>` for a public certificate.

Check readiness:

```bash
curl -k https://localhost/api/v1/ready
# {"status":"ok","version":"0.1.0","checks":{"timescaledb":"ok","schema":"ok","database":"ok"}}
```

### First sign-in, users and roles

Sign in at **https://localhost/login/** as `janus-admin` with the `JANUS_ADMIN_PASSWORD` from
`.env` (`grep JANUS_ADMIN_PASSWORD .env`). The account is created only if missing, so a password
changed later in the UI is never overwritten.

Then open **Users & roles** to create users, set passwords, assign roles, disable access, reset
2FA or delete accounts. Users and groups live in lldap; Janus-Ops manages them through a dedicated
`janus-manager` account.

Each role maps to an LDAP group and holds a set of permissions (the union applies with several
roles). Built-in roles: **admin** (`janus-admins`, every permission), **operator**
(`janus-operators`), **auditor** (`janus-auditors`, `audit.read` + `users.read`). Permissions:

| Permission | Allows |
|---|---|
| `audit.read` | view the audit log and verify its hash chain |
| `users.read` | view users, roles and permissions |
| `users.manage` | create, edit, disable, delete users; reset passwords and 2FA |
| `roles.manage` | create and delete roles; change permissions and LDAP groups |

Who can read the audit log is a checkbox in the roles table. Only admins can grant the admin role.
Sessions last 8 hours with a 1-hour inactivity timeout; 5 failed attempts lock a username for
15 minutes.

### Two-factor authentication (optional)

Each user can enable TOTP from **My account**: scan the QR code with any authenticator app and
save the recovery codes. It works **behind corporate proxies and air-gapped**: the QR code is drawn
by the server and codes depend only on the clock (keep NTP in sync). Seeds are encrypted with
`JANUS_SECRET_KEY`: back it up together with the database volume.

### Break-glass account

Emergency admin that does not depend on LDAP:

```bash
docker compose run --rm --no-deps janus-ops breakglass
```

Sign in as `breakglass`. Every use is audited and logged as a warning.

## Running the image on its own

```bash
# 1. Apply the schema (needs PostgreSQL 16 with the TimescaleDB extension)
docker run --rm \
  -e JANUS_DATABASE_URL='postgresql+asyncpg://janus:PASSWORD@db-host:5432/janus' \
  lordraw77/janus-ops:0.1.0 migrate

# 2. Start the server
docker run -d --name janus-ops -p 127.0.0.1:8000:8000 \
  -e JANUS_DATABASE_URL='postgresql+asyncpg://janus:PASSWORD@db-host:5432/janus' \
  lordraw77/janus-ops:0.1.0
```

## Configuration

Variables read by the `janus-ops` process:

| Variable | Default | Description |
|---|---|---|
| `JANUS_DATABASE_URL` | — (required) | `postgresql+asyncpg://USER:PASSWORD@HOST:5432/DB` |
| `JANUS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Logs are JSON on stdout |
| `JANUS_HOST` | `0.0.0.0` | Listen address |
| `JANUS_PORT` | `8000` | Listen port |
| `JANUS_STATIC_DIR` | `/app/static` | Exported web UI; unset it to run the API only |
| `JANUS_DB_CONNECT_TIMEOUT_S` | `5` | Database connect timeout, seconds |
| `JANUS_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies trusted for `X-Forwarded-*` headers |
| `JANUS_LDAP_URL` | — | `ldap://host:port` or `ldaps://host:port`; unset = LDAP sign-in off |
| `JANUS_LDAP_BASE_DN` | — | Directory base DN (required with LDAP) |
| `JANUS_LDAP_USER_BASE_DN` | `ou=people,<base DN>` | Where users live |
| `JANUS_LDAP_USER_FILTER` | `(&(objectClass=person)(uid={username}))` | User search filter |
| `JANUS_LDAP_BIND_DN` / `JANUS_LDAP_BIND_PASSWORD` | — | Read-only service account (required with LDAP) |
| `JANUS_LLDAP_HTTP_URL` | — | lldap GraphQL endpoint; unset = users are read-only (external directory) |
| `JANUS_LDAP_MANAGER_USER` / `JANUS_LDAP_MANAGER_PASSWORD` | `janus-manager` / — | lldap account in `lldap_admin`, used for user administration |
| `JANUS_ADMIN_USERNAME` / `JANUS_ADMIN_PASSWORD` / `JANUS_ADMIN_EMAIL` | `janus-admin` / — / `<user>@janus-ops.invalid` | Initial administrator created by `setup` if missing |
| `JANUS_SECRET_KEY` | — | 64 hex chars, encrypts TOTP seeds; unset = TOTP unavailable |
| `JANUS_LDAP_TIMEOUT_S` | `5` | LDAP timeout, whole seconds |
| `JANUS_LDAP_CA_FILE` | system CAs | CA bundle to verify `ldaps://` servers |
| `JANUS_SESSION_TTL_S` / `JANUS_SESSION_IDLE_S` | `28800` / `3600` | Session lifetime / inactivity timeout, seconds |
| `JANUS_COOKIE_SECURE` | `true` | Session cookie over HTTPS only |
| `JANUS_BREAKGLASS_USERNAME` | `breakglass` | Name of the local emergency admin |

Variables used by the Compose stack (`.env`):

| Variable | Default | Description |
|---|---|---|
| `JANUS_IMAGE` / `JANUS_VERSION` | `lordraw77/janus-ops` / `0.1.0` | Image and tag to run |
| `JANUS_DOMAIN` | `localhost` | Host name served by Caddy |
| `JANUS_TLS` | `internal` | `internal` (local CA, works offline) or an e-mail for ACME |
| `JANUS_HTTP_PORT` / `JANUS_HTTPS_PORT` | `80` / `443` | Host ports of the proxy |
| `POSTGRES_PASSWORD` | — (required) | Password of the `janus` database user (use hex: it goes into a URL) |
| `LDAP_BASE_DN` | `dc=janus-ops,dc=local` | Base DN of the directory |
| `LLDAP_JWT_SECRET` / `LLDAP_KEY_SEED` | — (required) | lldap secrets |
| `LLDAP_ADMIN_PASSWORD` | — (required) | lldap `admin` password |
| `LLDAP_UI_PORT` | `17170` | lldap admin UI port, bound to `127.0.0.1` |
| `LDAP_BIND_PASSWORD` | — (required) | Password of the read-only service account `janus-svc` |
| `LDAP_MANAGER_PASSWORD` | — (required) | Password of the manager account `janus-manager` |
| `JANUS_SECRET_KEY` | — (required) | Encryption key for secrets at rest (64 hex chars) |
| `JANUS_ADMIN_USERNAME` / `JANUS_ADMIN_PASSWORD` / `JANUS_ADMIN_EMAIL` | `janus-admin` / — / — | Initial administrator (empty password = not created) |
| `JANUS_SESSION_TTL_S` / `JANUS_SESSION_IDLE_S` / `JANUS_BREAKGLASS_USERNAME` | see above | Passed to the app |
| `TZ` | `Etc/UTC` | Time zone of the lldap container |

## Endpoints

| Path | Description |
|---|---|
| `/` | Web UI (sign-in at `/login/`) |
| `/api/v1/auth/login` · `/login/totp` · `/logout` · `/me` | Sign-in (password, then TOTP code if enabled; HttpOnly session cookie), sign-out, current user |
| `/api/v1/auth/totp/*` | TOTP self-service: setup, enable, disable, recovery codes |
| `/api/v1/users` · `/api/v1/roles` | User and role/permission administration |
| `/api/v1/audit` · `/audit/verify` | Audit log (paged, filterable) and hash-chain verification |
| `/api/v1/health` | Liveness: the process is up |
| `/api/v1/ready` | Readiness: database reachable, TimescaleDB loaded, schema up to date (`503` otherwise) |
| `/api/docs` | OpenAPI documentation |

## Tags

| Tag | Meaning |
|---|---|
| `X.Y.Z` (e.g. `0.1.0`) | Immutable release, built from git tag `vX.Y.Z` |
| `latest` | Most recent release |

## Security

- Non-root user, no shell login, no package manager use at runtime.
- The reference stack drops all capabilities, sets `no-new-privileges` and a read-only root
  filesystem, and keeps database and directory on an internal network.
- Error responses and logs never include connection strings or credentials; logs carry no usernames.
- Session tokens are random, stored only as SHA-256 hashes, sent as `HttpOnly; Secure;
  SameSite=Strict` cookies; cross-origin writes are rejected.
- Every sign-in, failure and sign-out goes to a hash-chained, append-only audit log.
- Report vulnerabilities privately through
  [GitHub Security Advisories](https://github.com/lordraw77/janus-ops/security/advisories/new).

## License

Janus-Ops is licensed under the
[Apache License 2.0](https://github.com/lordraw77/janus-ops/blob/main/LICENSE).
The reference stack runs TimescaleDB Community Edition, which has its own license, the
[Timescale License (TSL)](https://www.tigerdata.com/legal/licenses). The TSL is free for
self-hosted use.

**Source, issues, docs:** https://github.com/lordraw77/janus-ops
