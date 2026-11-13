<div align="center">

<img src="res/logos/janus-ops-logo-512.png" alt="Janus-Ops logo" width="200" />

# Janus-Ops

**One portal for your whole infrastructure lifecycle.**<br/>
Monitoring · Containers · Virtualization · SSH · Automation · AI troubleshooting — in a single pane of glass.

![Status](https://img.shields.io/badge/status-pre--alpha%200.1.0-F59E0B?style=flat-square)
![Backend](https://img.shields.io/badge/backend-FastAPI%20%C2%B7%20Python%203.12-10B981?style=flat-square)
![Frontend](https://img.shields.io/badge/frontend-Next.js%20%C2%B7%20TypeScript-22D3EE?style=flat-square)
![Agent](https://img.shields.io/badge/agent-Go%20static%20binary-818CF8?style=flat-square)
![DB](https://img.shields.io/badge/db-PostgreSQL%20%2B%20TimescaleDB-334155?style=flat-square)
![License](https://img.shields.io/badge/license-Apache--2.0-F1F5F9?style=flat-square)

</div>

---

## Why Janus-Ops

Operating a real-world infrastructure usually means juggling half a dozen tools:
**Checkmk** for monitoring, **Portainer / Dockhand** for containers, the **Proxmox VE** web UI,
**MobaXterm / Termius** for SSH, and a terminal for **Ansible**. Every jump costs context.

Janus-Ops — named after the two-faced Roman god of gateways and transitions — puts **one central
inventory** at the core and plugs decoupled modules around it. Every host is defined once; its
capabilities (agent, SSH target, Docker host, Proxmox node, SNMP device) light up the matching tools.

It is designed from day one for **segmented, air-gapped and proxy-restricted networks**: the
`janus-agent` is a zero-dependency Go binary that only opens **outbound** connections on `443`.

## Core capabilities

| Module | What it does | Inspired by |
|---|---|---|
| 👁️ **Observability & Alerting** | High-frequency metrics, auto service discovery, `OK/WARN/CRIT/UNKNOWN` with hysteresis, ack, downtimes, flapping detection, parent/child dependencies, notification routing | Checkmk |
| 🐳 **Container & Stack Manager** | Multi-host Docker management through the agent tunnel, live logs/stats, exec shell, Compose stacks with GitOps, prune & image update checks | Dockhand / Portainer |
| 🛡️ **Virtualization** | Proxmox VE clusters: quorum, nodes, storage, VM/LXC lifecycle, snapshots, migration, console, backup & replication audit | Proxmox VE UI |
| 🔑 **SSH Bastion** | Web terminal (xterm.js), credential vault, SFTP browser, multi-tab & broadcast, snippet library, transparent ProxyJump / agent tunnel | MobaXterm / Termius |
| ⚡ **Automation Hub** | Ansible playbooks with dynamic inventory from the DB, surveys, live output; visual event-driven workflow builder | AWX / n8n |
| 🤖 **Janus AI Copilot** | One-click RCA on failing services (logs + metrics + audit trail), contextual IaC generation, **human-approved** action plans only — powered by **self-hosted** models | — |

Cross-cutting: unified inventory, encrypted secret vault, LDAP-backed login with TOTP and RBAC
(Admin / Operator / Auditor), immutable audit log with optional terminal session recording,
global command palette (`Ctrl+K`).

## Quick start

Requirements: Docker with Compose v2, `make`, a Linux host. From a clone of this repository:

```bash
make env          # creates deploy/.env with random secrets; on upgrades adds only new ones
make docker/run   # builds the image and starts proxy, app, migrations, TimescaleDB, lldap
```

Open **https://localhost/**. The certificate comes from Caddy's local CA (`JANUS_TLS=internal`),
so the browser warns until you trust it or configure a real domain. Readiness check:

```bash
curl -k https://localhost/api/v1/ready
```

Stop with `make docker/stop`; data volumes are kept. All variables are documented in
[deploy/.env.example](deploy/.env.example). To run the published image without cloning the
repository, see the [Docker Hub overview](docs/dockerhub/OVERVIEW.md).

### Users and sign-in

Janus-Ops signs users in with **username and password from the LDAP directory** (lldap, in the
stack). At first start the one-shot `ldap-init` job creates three groups and a read-only service
account (`janus-svc`) that Janus-Ops uses to look users up:

| LDAP group | Janus-Ops role |
|---|---|
| `janus-admins` | admin |
| `janus-operators` | operator |
| `janus-auditors` | auditor |

1. Open the lldap admin UI. It listens on `127.0.0.1:17170` only; from your workstation:
   `ssh -L 17170:127.0.0.1:17170 <server>`, then http://localhost:17170
   (user `admin`, password `LLDAP_ADMIN_PASSWORD` in `deploy/.env`).
2. Create a user, set its password and add it to one of the groups above.
3. Sign in at https://localhost/login/.

A user in several groups gets the highest role; a user in none is refused. The role is read at
sign-in, so group changes apply at the next login. Sessions last 8 hours and end after 1 hour of
inactivity (`JANUS_SESSION_TTL_S`, `JANUS_SESSION_IDLE_S`). After 5 failed attempts a username is
locked for 15 minutes. Every sign-in, failure and sign-out is written to the audit log.
Two-factor authentication (TOTP) is the next step on the roadmap.

### Break-glass account

A local admin account for when the directory is down. It never uses LDAP:

```bash
make breakglass   # prompts for the password (min. 16 characters), creates or rotates it
```

Sign in with username `breakglass` (`JANUS_BREAKGLASS_USERNAME`). Rotating the password ends
all its sessions; every use is audited and logged as a warning.

## Development

| Command | What it does |
|---|---|
| `make deps` | Install backend (uv) and frontend (npm) dependencies |
| `make lint` | ruff, mypy (strict), ESLint, TypeScript |
| `make test` | Backend unit tests |
| `make test/integration` | Schema and sign-in tests against throwaway TimescaleDB and lldap containers |
| `make run` / `make run/ui` | Backend on :8000 (needs `JANUS_DATABASE_URL`) / Next.js dev server on :3000 |
| `make docker/build` | Build `lordraw77/janus-ops:<version>` |
| `make breakglass` | Create the break-glass admin or rotate its password |
| `make release` | Tag `v<version>`; CI then publishes the multi-arch image to Docker Hub |

`make help` lists every target. CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs
lint, unit and integration tests and the image build on every push; on a `v*` tag it pushes
`linux/amd64` + `linux/arm64` images (needs the `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`
repository secrets).

## Architecture at a glance

Target architecture. The current release ships the reverse proxy, the FastAPI core with the web UI,
PostgreSQL + TimescaleDB and the LDAP sidecar. Workers, Redis, the agent gateway and the agent
arrive with the phases that need them.

```mermaid
flowchart LR
    subgraph Browser
        UI["Next.js UI<br/>NOC · xterm.js · Monaco · React Flow"]
    end

    subgraph Server["Janus-Ops server"]
        RP["Reverse proxy (TLS :443)"]
        API["FastAPI core<br/>Inventory · Auth/RBAC · Vault · Audit"]
        GW["Agent gateway<br/>(WSS tunnel)"]
        WK["Workers<br/>checks · alerts · ansible · workflows · AI"]
        DB[("PostgreSQL 16<br/>+ TimescaleDB")]
        RD[("Redis")]
        LD[("LDAP directory<br/>(sidecar)")]
    end

    subgraph Managed["Managed infrastructure"]
        AG["janus-agent (Go)<br/>telemetry · docker proxy · exec/PTY"]
        SSH["SSH targets"]
        PVE["Proxmox VE API"]
        SNMP["SNMP devices"]
    end

    LLM["Self-hosted LLM<br/>Ollama / vLLM / SGLang"]

    UI <--> RP
    RP --> API
    RP --> GW
    API --> LD
    AG -- "outbound WSS :443<br/>(proxy-aware)" --> RP
    API <--> DB
    API <--> RD
    WK <--> DB
    WK <--> RD
    GW <--> RD
    WK --> SSH
    WK --> PVE
    WK --> SNMP
    WK --> LLM
```

Full functional specification: [docs/specs.md](docs/specs.md) ·
Implementation plan, ADRs and roadmap: [docs/PLAN.md](docs/PLAN.md)

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12+, FastAPI, SQLAlchemy 2.0 (async), Alembic |
| Data | PostgreSQL 16 + TimescaleDB (Community Edition), Redis |
| Identity | LDAP directory as a Docker sidecar (users & groups → roles), TOTP, local break-glass admin |
| Workers | Async task queue + scheduler (see ADR in [docs/PLAN.md](docs/PLAN.md)) |
| Integrations | AsyncSSH, aiodocker / Docker Engine API, Proxmox VE REST API, pysnmp, ansible-runner |
| Frontend | Next.js (App Router, static export served by the API), TypeScript, Tailwind CSS v4; later shadcn/ui, TanStack Query, Zustand, ECharts, Monaco, xterm.js, React Flow |
| Packaging | One image `lordraw77/janus-ops` (amd64/arm64), Docker Compose with Caddy for TLS |
| Agent | Go, `CGO_ENABLED=0` static builds for `linux/amd64`, `linux/arm64` (Windows later) |
| AI | OpenAI-compatible API to self-hosted models (Ollama / vLLM / SGLang), read-only tool calling |

## Design system — *Obsidian Graphite NOC*

A dark, high-density NOC theme with Checkmk-grade status contrast.

<img src="res/obsidian-graphite-palette-sheet.png" alt="Obsidian Graphite palette" width="720" />

| Token | Hex | Usage |
|---|---|---|
| `--bg-canvas` | `#090A0F` | Main background |
| `--bg-surface` | `#12141D` | Cards & panels |
| `--bg-elevated` | `#1B1E2B` | Modals, table headers, hover |
| `--border-default` | `#262A3B` | 1px dividers |
| `--accent-primary` | `#10B981` | Live / active / healthy |
| `--accent-telemetry` | `#22D3EE` | Charts, sparklines, CLI |
| `--accent-ai` | `#818CF8` | Janus AI diagnose & RCA |
| `--text-primary` | `#F1F5F9` | Hostnames, KPI values |
| `--text-nav` | `#94A3B8` | Inactive navigation items |
| `--text-muted` | `#64748B` | IPs, timestamps, logs |

Status badges: **OK** `#10B981` · **WARN** `#F59E0B` · **CRIT/DOWN** `#EF4444` · **UNKNOWN** `#8B5CF6` · **OFFLINE** `#475569`.
Typography: **Inter** for UI, **JetBrains Mono** (tabular numerals) for metrics.
Logo, favicons, tokens, base CSS, reference components and the app-shell mockup live in
[`res/`](res/) — see [res/README.md](res/README.md).

## Project status & roadmap

> 🚧 **Pre-alpha (0.1.0).** Available today: the single `lordraw77/janus-ops` image, LDAP
> sign-in with roles, break-glass account, sessions and hash-chained audit log, the full F0–F3
> database schema, the Compose stack and CI. Next: TOTP, then inventory and vault to close F0.

| Phase | Scope |
|---|---|
| **F0** Foundations | Monorepo, CI, compose stack, LDAP login/RBAC, inventory, vault, audit log, UI shell & design system |
| **F1** SSH Bastion | Web terminal, vault-backed credentials, SFTP, session audit |
| **F2** Agent & Telemetry | `janus-agent` enrollment, WSS tunnel, metrics ingest, NOC dashboard |
| **F3** Alerting | Check states, hysteresis, ack/downtime, dependencies, notifications |
| **F4** Containers | Docker via agent, logs/stats/exec, Compose stacks |
| **F5** Proxmox | Cluster overview, VM/LXC lifecycle, backup audit |
| **F6** Ansible | Dynamic inventory, playbook catalog, live runs |
| **F7** AI Copilot | RCA on alerts, action plans with human approval |
| **F8+** | Visual workflows, GitOps, SNMP, agent PTY & auto-update, Windows agent |

Details, acceptance criteria and open decisions: [docs/PLAN.md](docs/PLAN.md).

## Repository layout

```
janus-ops/
├── backend/         FastAPI control plane (package janus_ops, Alembic migrations, tests)
├── frontend/        Next.js web UI, exported as static files into the image
├── deploy/          compose.yaml, compose.build.yaml, Caddyfile, .env.example
├── scripts/         gen-env, integration tests, release
├── docs/            specs, PLAN (ADRs, roadmap), Docker Hub overview
├── res/             Brand & design resources (logo, favicons, tokens, mockups)
├── Dockerfile       Single image: UI build → Python venv → non-root runtime
└── Makefile         Entry points (`make help`)
```

Planned with later phases: `agent/` (Go, F2) and `proto/` (agent ⇄ server contracts).

## Security principles

- The agent **never listens** on a port; all traffic is outbound, authenticated and encrypted.
- Private keys and secrets **never reach the browser**; they are decrypted server-side only when used.
- Every privileged action (shell, restart, playbook, AI action plan) is **attributed and audited**.
- The AI Copilot runs on **self-hosted models only**, is **read-only by default**, never sees vault
  secrets and never executes destructive actions on its own.

## License

Janus-Ops is released under the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE).

Janus-Ops runs on TimescaleDB Community Edition, which has its own license, the
[Timescale License (TSL)](https://www.tigerdata.com/legal/licenses). The TSL is free for self-hosted use.
