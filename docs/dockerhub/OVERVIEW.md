<p align="center">
  <img src="https://raw.githubusercontent.com/lordraw77/janus-ops/main/res/logos/janus-ops-logo-512.png" alt="Janus-Ops" width="160">
</p>

<h1 align="center">Janus-Ops</h1>

<p align="center">
  <b>One portal for your whole infrastructure lifecycle.</b><br>
  Monitoring · Containers · Proxmox VE · SSH · Ansible · Workflows · AI troubleshooting
</p>

---

> **Status: pre-1.0 (0.5.x).** Every planned module exists (phases F0–F8), but they are not equally
> proven: some have been exercised against real infrastructure, others only against simulated
> services or in tests. Each module's verification status is listed in the
> [plan](https://github.com/lordraw77/janus-ops/blob/main/docs/PLAN.md). Evaluate it on a lab before
> relying on it in production.

## What is Janus-Ops?

Janus-Ops replaces the daily jump between Checkmk, Portainer/Dockhand, the Proxmox VE UI,
MobaXterm and Ansible terminals with **one central inventory** and decoupled modules around it.
It is built for **segmented, air-gapped and proxy-restricted networks**: no runtime downloads,
self-hosted TLS, and a Go agent that only opens outbound connections.

| Module | What it does |
|---|---|
| Control plane, UI, audit | Sign-in with LDAP / Active Directory, roles and permissions, optional TOTP, break-glass account, hash-chained audit log |
| Inventory and vault | Nodes, tags, groups, environments, per-role node scope; encrypted credential vault |
| SSH bastion | Web terminal, jump host, host-key pinning, session recording and replay, SFTP browser, broadcast, snippets |
| `janus-agent` | Outbound-only telemetry agent, SSH bootstrap, optional commands, shell and Docker access under a local policy, signed self-update with rollback |
| Monitoring | Checks (agent, metrics, ports, HTTP, systemd, SNMP, FRITZ!Box), hysteresis, acknowledge, downtimes, dependencies, notifications (mail, Telegram, webhook, Discord, Slack, ntfy) |
| Containers | Docker hosts and Compose stacks through the agent or SSH: logs, stats, actions, shell |
| Proxmox VE | Clusters, VM/LXC lifecycle, snapshots, backup audit, consoles |
| Ansible | Playbook catalogue (Git, upload, editor, network mount, SFTP), surveys, live runs, history |
| Workflows | Graph editor (drag steps, connect branches), manual / interval / alert / signed-webhook triggers, conditional branches, approvals, run status drawn on the flow |
| GitOps | Compose stacks deployed from Git with a signed webhook |
| AI Copilot | Diagnosis of problems on your own OpenAI-compatible endpoint, action plans you approve |
| Secret manager | Optional OpenBao / HashiCorp Vault for the master key (Transit) or the secrets themselves (KV) |

## What is in this image

- **FastAPI** control plane (Python 3.12) and the **Next.js** web UI exported as static files,
  served by the same process.
- The **agent binaries** (`/app/agent`: `linux/amd64`, `linux/arm64`, `linux/arm`), each with the
  release signature that lets the server push signed updates.
- Runs as **non-root** (uid/gid `10001`), ready for `read_only: true`. The only state on disk is
  the optional terminal recordings directory (`/var/lib/janus-ops/recordings`, mount a volume).
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
| `secrets status` / `migrate --to <backend>` / `prune` | Where the master key lives; move secrets between `local`, `openbao-transit` and `openbao-kv`; remove orphaned secrets in KV |
| `agent-keygen` / `agent-sign` / `agent-verify` | Create the agent release key, sign agent binaries, verify signed binaries (run on the release machine) |
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
# {"status":"ok","version":"0.6.0","checks":{"timescaledb":"ok","schema":"ok","database":"ok"}}
```

Optional overrides, used with `-f compose.yaml -f <file>` (all in the same `deploy/` folder):

| File | Adds |
|---|---|
| `compose.openbao.yaml` | An OpenBao with automatic unseal, as the home of the master key |
| `compose.external-directory.yaml` | Sign-in with an external LDAP / Active Directory instead of lldap |
| `compose.release.yaml` | A host directory of signed agent binaries (only for hand-signed builds) |

### First sign-in, users and roles

Sign in at **https://localhost/login/** as `janus-admin` with the `JANUS_ADMIN_PASSWORD` from
`.env` (`grep JANUS_ADMIN_PASSWORD .env`). The account is created only if missing, so a password
changed later in the UI is never overwritten.

Then open **Users & roles** to create users, set passwords, assign roles, disable access, reset
2FA or delete accounts. Users and groups live in lldap; Janus-Ops manages them through a dedicated
`janus-manager` account (with an external directory they are managed there).

Each role maps to an LDAP group and holds a set of permissions (the union applies with several
roles), optionally limited to some nodes (environment, tag, group, capability or specific nodes).
Built-in roles: **admin** (`janus-admins`, every permission), **operator** (`janus-operators`,
day-to-day operations) and **auditor** (`janus-auditors`: `audit.read`, `users.read`,
`inventory.read`). Permissions:

| Permission | Allows |
|---|---|
| `audit.read` | view the audit log and verify its hash chain |
| `users.read` / `users.manage` | view / create, edit, disable and delete users; reset passwords and 2FA |
| `roles.manage` | create and delete roles; change permissions, LDAP groups and node scope |
| `inventory.read` / `inventory.manage` | view / change nodes, tags, groups, capabilities |
| `environments.manage` | create, rename and delete environments |
| `credentials.manage` | create, replace and delete vault credentials (secrets are write-only) |
| `ssh.connect` | open web terminals (over SSH or through an agent) |
| `sftp.transfer` | browse and transfer files over SFTP |
| `snippets.manage` | manage shared command snippets |
| `agents.manage` | install, update, revoke, uninstall the agent (it runs as root) |
| `monitoring.manage` / `notifications.manage` | checks, downtimes, acknowledgements / notification channels and rules |
| `containers.manage` / `containers.exec` | container and stack actions / shells in containers |
| `proxmox.manage` / `proxmox.console` | VM/LXC actions / consoles |
| `ansible.read` / `ansible.manage` / `ansible.run` | browse / manage sources and files / run playbooks |
| `ai.use` / `ai.approve` / `actions.execute` | ask the Copilot / approve its plans / run approved plans on nodes |
| `gitops.manage` / `gitops.deploy` | manage GitOps stacks and webhook secrets / deploy now |
| `workflow.manage` / `workflow.run` / `workflow.approve` | edit workflows / start them / approve their waiting steps |

By default **operator** holds the operational permissions (inventory, agents, monitoring, containers,
Proxmox, Ansible runs, AI, GitOps deploys, workflows, SSH and SFTP); user, role, credential,
notification and environment administration stay with **admin**. Only admins can grant the admin
role. Sessions last 8 hours with a 1-hour inactivity timeout; 5 failed attempts lock a username for
15 minutes.

### Two-factor authentication (optional)

Each user can enable TOTP from **My account**: scan the QR code with any authenticator app and
save the recovery codes. It works **behind corporate proxies and air-gapped**: the QR code is drawn
by the server and codes depend only on the clock (keep NTP in sync). Seeds are encrypted with the
master key (`JANUS_SECRET_KEY`, or the secret manager): back it up together with the database volume.

### Inventory and vault

Add nodes (IP or FQDN, environment, tags, groups, parent dependency) and enable their
capabilities: SSH target, Janus agent, Proxmox node, Docker host, SNMP device, FRITZ!Box. Credentials
(SSH keys, passwords, Proxmox tokens or user + password, SNMP secrets, notification and webhook
secrets) live in the vault with envelope encryption; they are write-only and never sent to the browser.

### SSH bastion

Open terminals from the browser on nodes with the SSH capability (xterm.js over WebSocket; the
SSH connection and its credentials stay on the server). Host keys are pinned on first use and a
changed key blocks the connection. Sessions are recorded (asciicast, on the `recordings` volume)
and can be replayed; start, end and failures are audited. An SFTP file browser, command
broadcast across terminals and a snippet library complete the bastion. If the node's agent allows
it, you can also open **a shell through the agent**, with no SSH credential.

### The agent

`janus-agent` is a static Go binary installed over SSH from the node page. It opens no port: it
connects out to the server over WSS (HTTP(S) proxies and custom CAs supported), enrols with a
single-use token and an ed25519 key that never leaves the node, and buffers telemetry on disk while
disconnected. What it will do for the server is decided by a **local policy** written at
installation (`allow_exec`, `allow_pty`, `allow_docker`); the server cannot change it.

**Signed updates.** The agent replaces itself only with a binary signed by your release key
(`janus-ops agent-keygen`, kept off the server). The server holds only the public key
(`JANUS_AGENT_RELEASE_PUBLIC_KEY`) and distributes the signed binaries bundled in this image; the
agent checks the signature, refuses a version that is not newer, and rolls back automatically if the
new binary cannot connect. Roll an update out from **Inventory → Update agents**.

### Secret manager (optional)

By default the master key is `JANUS_SECRET_KEY`. Set `JANUS_SECRET_BACKEND` to keep it in
**OpenBao or HashiCorp Vault**: `openbao-transit` (the key lives in Transit; secrets stay encrypted in
PostgreSQL) or `openbao-kv` (the content of each secret lives in KV v2, PostgreSQL holds only a
reference). `janus-ops secrets migrate --to <backend>` moves existing secrets, all or nothing and
reversible. `compose.openbao.yaml` runs an OpenBao that unseals itself on restart.

### External directory / Active Directory (optional)

Point `JANUS_LDAP_*` at your own LDAP or Active Directory (login attribute such as `sAMAccountName`,
UPN filter, StartTLS, nested groups) and start the stack with `compose.external-directory.yaml`.

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
  lordraw/janus-ops:0.6.0 migrate

# 2. Start the server
docker run -d --name janus-ops -p 127.0.0.1:8000:8000 \
  -e JANUS_DATABASE_URL='postgresql+asyncpg://janus:PASSWORD@db-host:5432/janus' \
  lordraw/janus-ops:0.6.0
```

## Configuration

Variables read by the `janus-ops` process:

| Variable | Default | Description |
|---|---|---|
| `JANUS_DATABASE_URL` | — (required) | `postgresql+asyncpg://USER:PASSWORD@HOST:5432/DB` |
| `JANUS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Logs are JSON on stdout |
| `JANUS_HOST` / `JANUS_PORT` | `0.0.0.0` / `8000` | Listen address and port |
| `JANUS_STATIC_DIR` | `/app/static` | Exported web UI; unset it to run the API only |
| `JANUS_DB_CONNECT_TIMEOUT_S` | `5` | Database connect timeout, seconds |
| `JANUS_DB_POOL_SIZE` / `JANUS_DB_MAX_OVERFLOW` | `20` / `20` | Database connections kept open / extra in a burst. Size for the fleet: 500 agents sending 200 series every 10 s need the defaults, a pool of 5 starves |
| `JANUS_FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies trusted for `X-Forwarded-*` headers |
| `JANUS_LDAP_URL` | — | `ldap://host:port` or `ldaps://host:port`; unset = LDAP sign-in off |
| `JANUS_LDAP_BASE_DN` | — | Directory base DN (required with LDAP) |
| `JANUS_LDAP_USER_BASE_DN` | `ou=people,<base DN>` | Where users live |
| `JANUS_LDAP_USER_FILTER` | `(&(objectClass=person)(uid={username}))` | User search filter; must contain `{username}` |
| `JANUS_LDAP_USERNAME_ATTR` | `uid` | Login attribute (`sAMAccountName` for Active Directory) |
| `JANUS_LDAP_START_TLS` | `false` | StartTLS on `ldap://` (certificate verified) |
| `JANUS_LDAP_NESTED_GROUPS` / `JANUS_LDAP_GROUP_BASE_DN` | `false` / base DN | Active Directory: resolve groups of groups / where groups live |
| `JANUS_LDAP_BIND_DN` / `JANUS_LDAP_BIND_PASSWORD` | — | Read-only service account (required with LDAP) |
| `JANUS_LDAP_TIMEOUT_S` / `JANUS_LDAP_CA_FILE` | `5` / system CAs | LDAP timeout; CA bundle for `ldaps://` |
| `JANUS_LLDAP_HTTP_URL` | — | lldap GraphQL endpoint; unset = users are read-only (external directory) |
| `JANUS_LDAP_MANAGER_USER` / `JANUS_LDAP_MANAGER_PASSWORD` | `janus-manager` / — | lldap account in `lldap_admin`, used for user administration |
| `JANUS_ADMIN_USERNAME` / `JANUS_ADMIN_PASSWORD` / `JANUS_ADMIN_EMAIL` | `janus-admin` / — / `<user>@janus-ops.invalid` | Initial administrator created by `setup` if missing |
| `JANUS_SECRET_KEY` | — | 64 hex chars; the master key of the `local` backend (empty = not set) |
| `JANUS_SECRET_BACKEND` | `local` | `local`, `openbao-transit` or `openbao-kv` |
| `JANUS_BAO_ADDR` / `JANUS_BAO_ROLE_ID` / `JANUS_BAO_SECRET_ID` | — | OpenBao / Vault address (`https://`) and AppRole credentials |
| `JANUS_BAO_TRANSIT_MOUNT` / `JANUS_BAO_TRANSIT_KEY` | `transit` / `janus-kek` | Transit engine mount and key |
| `JANUS_BAO_KV_MOUNT` / `JANUS_BAO_KV_PREFIX` | `secret` / `janus-ops` | KV v2 mount and path prefix (`openbao-kv`) |
| `JANUS_BAO_CA_FILE` / `JANUS_BAO_TIMEOUT_S` / `JANUS_BAO_DEK_CACHE_S` | system CAs / `5` / `300` | CA bundle; request timeout; how long unwrapped data keys stay in memory |
| `JANUS_RECORDINGS_DIR` | — | Terminal recordings directory; unset or empty = no recording |
| `JANUS_TERMINAL_IDLE_S` / `JANUS_TERMINAL_MAX_PER_USER` | `1800` / `10` | Terminal inactivity timeout; open terminals per user |
| `JANUS_SSH_CONNECT_TIMEOUT_S` | `10` | SSH connect timeout |
| `JANUS_SFTP_MAX_UPLOAD_MB` / `JANUS_SFTP_IDLE_S` | `1024` / `300` | SFTP upload limit; idle SFTP connections are closed after this many seconds |
| `JANUS_ANSIBLE_MOUNT_ROOTS` | — | Directories that network-mount playbook sources may live under (unset = disabled) |
| `JANUS_AI_BASE_URL` / `JANUS_AI_MODEL` / `JANUS_AI_API_KEY` / `JANUS_AI_TIMEOUT_S` | — / — / — / `120` | OpenAI-compatible endpoint for the Copilot (both URL and model needed to enable it) |
| `JANUS_AGENT_SERVER_URL` | URL used in the browser | `https://` URL agents connect to; must match the TLS certificate |
| `JANUS_AGENT_CA_FILE` | `/var/lib/janus-ops/pki/root.crt` (Compose) | CA certificate (PEM) written to nodes for the agent |
| `JANUS_AGENT_INTERVAL_S` | `10` | Telemetry interval, 5–15 seconds |
| `JANUS_AGENT_ENROLL_TTL_S` / `JANUS_AGENT_BOOTSTRAP_WAIT_S` | `900` / `45` | Lifetime of the enrolment token; how long a bootstrap waits for the agent |
| `JANUS_AGENT_DIR` | `/app/agent` | Agent binaries copied to nodes and pushed as updates (built into the image, signed) |
| `JANUS_AGENT_RELEASE_PUBLIC_KEY` | unset | Base64 ed25519 public key of the agent release key; written to new installs, enables signed updates |
| `JANUS_MONITORING_ENABLED` | `true` | Run the check engine and notification dispatcher in the server |
| `JANUS_SESSION_TTL_S` / `JANUS_SESSION_IDLE_S` | `28800` / `3600` | Session lifetime / inactivity timeout, seconds |
| `JANUS_COOKIE_SECURE` | `true` | Session cookie over HTTPS only |
| `JANUS_BREAKGLASS_USERNAME` | `breakglass` | Name of the local emergency admin |

Variables used by the Compose stack (`.env`):

| Variable | Default | Description |
|---|---|---|
| `JANUS_IMAGE` / `JANUS_VERSION` | `lordraw/janus-ops` / `0.6.0` | Image and tag to run |
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
| `JANUS_SECRET_KEY` | — (required for `local`) | Master key for secrets at rest (64 hex chars) |
| `JANUS_SECRET_BACKEND` / `JANUS_BAO_*` | `local` | Secret manager settings, see above |
| `JANUS_AGENT_RELEASE_PUBLIC_KEY` | — | Public key of your agent release key |
| `EXT_LDAP_*` | — | Settings of `compose.external-directory.yaml` (`EXT_LDAP_URL`, `EXT_LDAP_BASE_DN`, `EXT_LDAP_BIND_DN`, …) |
| `JANUS_ADMIN_USERNAME` / `JANUS_ADMIN_PASSWORD` / `JANUS_ADMIN_EMAIL` | `janus-admin` / — / — | Initial administrator (empty password = not created) |
| `TZ` | `Etc/UTC` | Time zone of the lldap container |

## Endpoints

| Path | Description |
|---|---|
| `/` | Web UI (sign-in at `/login/`) |
| `/api/v1/auth/*` | Sign-in (password, then TOTP code if enabled; HttpOnly session cookie), sign-out, current user, TOTP self-service |
| `/api/v1/users` · `/roles` · `/environments` | User, role/permission and environment administration |
| `/api/v1/audit` · `/audit/verify` | Audit log (paged, filterable) and hash-chain verification |
| `/api/v1/nodes` · `/groups` · `/tags` · `/credentials` | Inventory and vault (metadata only; secrets are never returned) |
| `/api/v1/terminal/*` · `/sftp/*` · `/snippets` | Web terminal (SSH or through an agent), recordings, host keys, SFTP, snippets |
| `/api/v1/agent/ws` | Agent gateway (WebSocket; ed25519 challenge, one-time enrolment token) |
| `/api/v1/agents` · `/{node}/bootstrap` · `/update` · `/update-all` · `/revoke` · `/uninstall` | Agent status, installation over SSH, signed updates |
| `/api/v1/telemetry/*` · `/checks` · `/problems` · `/downtimes` | NOC summary and charts, checks, problems, downtimes |
| `/api/v1/notifications/*` | Notification channels (secrets write-only), rules, delivery log |
| `/api/v1/containers/*` | Docker hosts, containers, stacks, logs, stats, actions, shells |
| `/api/v1/proxmox/*` | Clusters, VM/LXC, tasks, backups, consoles |
| `/api/v1/ansible/*` | Playbook sources, files, runs |
| `/api/v1/workflows/*` | Workflows, runs, approvals; `/{id}/webhook` is the signed trigger (HMAC-SHA256) |
| `/api/v1/gitops/*` | GitOps stacks; `/stacks/{id}/webhook` is the signed push trigger |
| `/api/v1/snmp-oids` · `/snmp-templates` · `/fritzbox/*` | SNMP catalogue and templates, FRITZ!Box |
| `/api/v1/ai/*` | AI Copilot diagnoses and action plans |
| `/api/v1/health` | Liveness: the process is up |
| `/api/v1/ready` | Readiness: database reachable, TimescaleDB loaded, schema up to date and, with a secret manager, reachable (`503` otherwise) |
| `/api/docs` | OpenAPI documentation |

## Tags

| Tag | Meaning |
|---|---|
| `X.Y.Z` (e.g. `0.6.0`) | Immutable release, built from git tag `vX.Y.Z` |
| `X.Y` (e.g. `0.5`) | Latest patch release of that minor version |
| `latest` | Most recent release |

Pin `X.Y.Z` in production. Every release image carries the signed agent binaries of the same
version; the build refuses to publish an image whose agent signatures do not match.

## Security

- Non-root user, no shell login, no package manager use at runtime.
- The reference stack drops all capabilities, sets `no-new-privileges` and a read-only root
  filesystem, and keeps database and directory on an internal network.
- Error responses and logs never include connection strings or credentials; logs carry no usernames.
- Session tokens are random, stored only as SHA-256 hashes, sent as `HttpOnly; Secure;
  SameSite=Strict` cookies; cross-origin writes are rejected.
- Every sign-in, failure and privileged action goes to a hash-chained, append-only audit log.
- Anything that can run commands on nodes (exec, shells, workflows, GitOps) is gated by a permission
  **and** by the node's local agent policy, and destructive workflow steps wait for a person.
- The agent release private key never enters the server or this image: only the public key does.
- Report vulnerabilities privately through
  [GitHub Security Advisories](https://github.com/lordraw77/janus-ops/security/advisories/new).

## License

Janus-Ops is licensed under the
[Apache License 2.0](https://github.com/lordraw77/janus-ops/blob/main/LICENSE).
The reference stack runs TimescaleDB Community Edition, which has its own license, the
[Timescale License (TSL)](https://www.tigerdata.com/legal/licenses). The TSL is free for
self-hosted use.

**Source, issues, docs:** https://github.com/lordraw77/janus-ops
