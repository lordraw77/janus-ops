# Janus-Ops — Analysis & Implementation Plan

> Planning document derived from [specs.md](specs.md).
> Status: **draft v0.2 — 2026-11-13**. Decisions marked `OPEN` await confirmation.
>
> **v0.2** — integrated the answers to the open questions: scale confirmed (A2), LDAP as a sidecar
> directory in the stack (A5 → ADR-008), Windows out of the MVP (A4), AI on external self-hosted models
> with access to all operational data (A6), TimescaleDB licence verified (C12). Added the analysis of the
> `janusops-design-kit/` (§8).
>
> **v0.3** — **Apache-2.0** licence (A8), **Janus-Ops** brand spelling (C15), favicon and logo variants
> generated from the master, `res/` as the single design-assets folder (§8). No open questions.
>
> **v0.4** — start of F0: single image `lordraw/janus-ops` (ADR-009, AMEND ADR-006), F0–F3 DDL schema
> as Alembic migration `0001`, backend/frontend/deploy/CI scaffold, Docker Hub overview
> (`docs/dockerhub/`). Progress in §5.1.
>
> **v0.5** — LDAP login with username and password, roles from groups, sessions, break-glass, audit with
> hash chain, login page (AC-02, AC-02b, AC-02c verified). Implementation details in ADR-008.
>
> **v0.6** — initial administrator from `.env`; user management from the UI (lldap through `janus-manager`);
> configurable roles with permissions (ADR-010), including who reads the audit log; audit viewer;
> **optional TOTP** per user, working behind a corporate proxy and air-gapped.
>
> **v0.7** — inventory (nodes, tags, groups, capabilities) and credential vault (ADR-004 implemented):
> **phase F0 completed**, AC-01 verified. Migration `0003`, `inventory.*` and
> `credentials.manage` permissions.
>
> **v0.8** — F1 started: web SSH bastion (xterm.js ⇄ WebSocket ⇄ AsyncSSH) with credentials from the
> vault, jump host, host-key pinning (TOFU), session recording and replay,
> `ssh.connect` permission, migration `0004`. AC-03 verified.
>
> **v0.9** — roadmap: secrets and keys in an external secret manager (OpenBao, HashiCorp Vault or
> similar), FR-86 promoted from WON'T to SHOULD, proposed in ADR-011.
>
> **v0.10** — **phase F1 completed**: SFTP browser (FR-11), command broadcast to several terminals
> and snippet library (FR-12). Migration `0005` (`snippet` table), `sftp.transfer` permissions
> (operator) and `snippets.manage` (admin only by default). Migration `0006`: Proxmox credential
> with user and password (`proxmox_password`, user with realm `root@pam`), besides the API token.
>
> **v0.11** — **phase F2**: Go agent (`agent/`, static amd64/arm64 binary), WebSocket gateway,
> enrollment with a single-use token and an ed25519 key generated on the node (AMEND ADR-002), bootstrap
> over SSH, telemetry with on-disk buffer and idempotent ingest into TimescaleDB, NOC dashboard (hexagon,
> node cards, "needs attention") and node page with charts. Migration `0007`, `agents.manage`
> permission (operator). Progress in §5.3.
>
> **v0.12** — **phase F3**: check engine (agent, metrics, systemd, ports, TCP and HTTP from the server)
> with hysteresis, soft/hard states and flapping; problems, ack, downtime, parent/child dependencies;
> email notifications (SMTP with or without login), Telegram, signed webhook, Discord, Slack, ntfy;
> check discovery. Migration `0008`, `monitoring.manage` (operator) and
> `notifications.manage` (admin) permissions. Progress in §5.4.
>
> **v0.13** — **phase F4**: Docker containers through the agent tunnel (multiplexed streams, local
> `allow_docker` policy) or over SSH (Unix socket forwarding); list, Compose stacks, inspect,
> live logs, statistics, actions, exec in the browser. Migration `0009`, `containers.manage` and
> `containers.exec` permissions (operator). Progress in §5.5. The Containers view is
> a table (state, health, uptime, restarts, CPU, memory, network, disk, IP, published
> ports, stack) with search, filters and sorting.
>
> **v0.14** — **phase F5**: Proxmox VE. Async httpx client (ADR C4: Proxmoxer is not needed), ticket or
> API token from the vault, clusters grouped with failover between members, overview of nodes/storage/VMs/LXCs,
> lifecycle, snapshot, clone, migration, tasks with logs, vzdump/PBS backup audit and xterm.js
> console via termproxy. Migration `0010`, `proxmox.manage` and `proxmox.console` permissions
> (operator). Progress in §5.6.
>
> **v0.15** — **configurable environments and per-role node scope**. Environments are a
> table (no longer a fixed constraint): CRUD for admins (`environments.manage`). Each role sees
> all the nodes or only those that satisfy at least one rule; a rule combines with AND
> environment, tag, group, capability and specific nodes; several roles add up. Applied to inventory,
> checks, problems, downtime, dashboard, agent, terminal, SFTP, containers, Proxmox and session
> history; nodes outside the scope answer 404. Migration `0011`. Details in §5.7.
>
> **v0.16** — **F6 — playbook catalogue from several sources**: besides Git, playbooks can come
> from a local file upload, from an editor built into the UI, from a network mount (NFS/SMB)
> or from a remote host over SFTP/SCP. Each source is registered with its type; remote sources
> use credentials from the vault (ADR-004). Sources are read-only, except the editor and
> the upload, which write to the internal catalogue. Further details in §5.8 (F6 decisions).
> In this version the alert from the backups to the F3 engine (`backup` check) was closed in F5; testing
> with a real PBS remains open.
>
> **v0.17** — **phase F6 started**: backend of the playbook catalogue (migration `0012`, permissions
> `ansible.read`, `ansible.manage`, `ansible.run`). Git, upload, editor and network mount sources
> (only directories listed in `JANUS_ANSIBLE_MOUNT_ROOTS`); versioned files, diff, runs with a
> frozen history, UI in the console. Progress in §5.9.
>
> **v0.18** — **phase F6 completed** (with partial verifications, see §5.9): SSH password, jump host
> (one hop, key only), playbook surveys (`<name>.survey.json`, values frozen in the run),
> read-only SFTP source with TOFU host key, dedicated integration tests. Migration `0013`.
>
> **v0.19** — **phase F7 started** (ADR-007): Copilot on a self-hosted OpenAI-compatible endpoint (https),
> read-only tools (inventory, metrics, check history, node audit), asynchronous diagnosis of an
> open problem, approvable or editable action plan (`ai.use`, `ai.approve`). Migration `0014`.
> Progress in §5.10.
>
> **v0.20** — **phase F8 started** (§5.11): exec through the agent with local `allow_exec` policy and execution of the
> Copilot's approved actions (`actions.execute`), audit of every attempt. Status, README and
> STATE aligned with the already completed phases F0–F7.
>
> **v0.21** — **F8, GitOps compose** (FR-81, backend): stacks from an https repository, deploy through the agent, signed webhook, run history, `gitops.manage` and `gitops.deploy` permissions. Migration `0015`. Web interface and workflows still to do. Details in §5.11.
>
> **v0.22** — **F8, SNMP v2c/v3** (FR-82): `snmp` check (one GET read of a numeric OID from the server,
> thresholds and states like the other checks), `snmp` capability with version, port, security level and
> v3 algorithms; `generic` (community) and `snmp_v3` (user and passphrase in the vault) credentials. `pysnmp`
> dependency (ADR-012). No migration for SNMP.
>
> **v0.23** — **SNMP OID catalogue** (migration `0016`): `snmp_oid` table with the entries of the
> Synology DiskStation MIB guide (system, disks, RAID, UPS). In the check form you pick a name from the catalogue;
> for table columns (disks, pools) you give the instance number. Entries are added and
> marked as verified from the UI. Read with `inventory.read`, edit with `monitoring.manage`, audit
> of every change. Details in §5.11.
>
> **v0.26** — **agent for the 32-bit Raspberry Pi** (`armv7l`, Pi 2 and 3 with a 32-bit system): build
> `GOARCH=arm GOARM=7` in the Makefile and in the Dockerfile, bootstrap that recognises `armv7l`. The 64-bit Pi
> already uses the `arm64` agent.
>
> **service** check (systemd through the agent): `systemctl is-active <unit>` run by the agent (needs a connected
> agent and `allow_exec`). The command is fixed, the parameter holds only the (validated) unit name: `active`
> is ok, `activating`/`reloading` warning, anything else critical. The `systemd` check remains for failed units.
>
> Inventory: **Agent** (version and architecture) and **Installed at** columns (the path of the binary,
> `/usr/local/bin/janus-agent`, or `—` if the agent is not installed or is revoked). Migration `0022`: the
> architecture constraint accepts `arm`, otherwise the agent of a 32-bit Pi could not register.
>
> **v0.25** — **F8, workflows** (FR-80, migration `0021`): definitions with manual, interval and
> alert triggers; exec (through the agent), notify (to a notification channel) and approval steps; approvals with rejection; completed steps are not repeated.
> Background execution with a 30-second scheduler. Details in §5.11.
>
> **v0.24** — **SNMP: discovery, device types, check templates** (migrations `0017`, `0018`).
> Read-only discovery of an SNMP node (GETNEXT under the chosen roots, at most three, only
> under `1.3.6.1`, 500 responses and 60 seconds per root): candidates are selected and only then
> enter the catalogue. Each catalogue entry has a device type (nas, ups, switch, router,
> server, generic). Preconfigured check templates (`snmp_template`): a basic Synology NAS and a basic
> UPS; applying one creates the checks on the node, one check per instance for table entries
> (disks, pools), and skips checks that already exist.
>
> **v0.28** — **check templates** (migration `0026`, F8): `snmp_template` becomes `check_template`, a
> bundle of checks of any kind with `{$MACROS}` (node values, `NODE_ADDRESS`/`NODE_NAME` built in), a
> required capability and `detect` rules. Built-in templates are JSON files in
> `monitoring/builtin_templates/` synced by slug and version (Linux, web endpoint, PostgreSQL, Apache,
> Proxmox VE; the SNMP ones are migrated). Templates page with editor, preview, nodes using each
> template and update marker. **Service discovery**: the agent reports its running systemd services and
> the catalog services it found (PostgreSQL, Apache: command line and a few settings read from their
> configuration, fixed allow-list, read only, no secret); a template's `detect` rules suggest it on the
> node's page, and a macro with an `auto` rule takes its value from there (a typed value always wins);
> when the node changes (a new port), the stored value and the checks made from it follow, and the
> change is audited. Needs an agent newer than 0.7.0.
>
> **v0.30** — **node services** (migration `0028`, 0.8.1): PostgreSQL and FRITZ!Box leave the capabilities
> (how Janus-Ops reaches a node: ssh, agent, proxmox, docker, snmp) and become **services**
> (`node_service`): a node has any number, each with a name, config and credential, so two PostgreSQL
> servers are two services. The kinds, their config and credential kinds are a catalog in code
> (`GET /service-types`) from which the web form is drawn: a new kind is an entry and its connector. A
> probe names the service it reads (`service` argument) when the node has several.
>
> **0.8.0 released** (agent signed): migrations `0026` and `0027` applied on the live stack, checked with a
> Rocky Linux 9 + PostgreSQL 13 test server (discovery, macros found by the agent, probes).
>
> **v0.29** — **service probes** (migration `0027`, F8): a check kind `probe` that reads one named
> measurement of a service through a connector in the server (`monitoring/probes/`): PostgreSQL
> (connections, long transactions, locks, cache hit, replication lag, wraparound: one fixed read-only
> query per snapshot, `postgres` capability with a `postgres_password` credential, role `pg_monitor`),
> Apache httpd (`mod_status` busy workers and traffic) and Proxmox VE (nodes offline, quorum, CPU,
> memory, storage). A snapshot is shared by the probes of one service for 15 s. The catalog is code:
> a template or a check only names the probe, it never carries SQL or a command. New built-in
> templates `postgresql-database` and `apache-httpd-status`; `proxmox-ve` v2. Limit: Apache restricts
> `server-status` to localhost by default, so the Janus-Ops server must be allowed to read it.
>
> **v0.27** — **workflows: webhook trigger and conditional branches** (migration `0024`): a signed
> `POST /workflows/{id}/webhook` starts a run (secret sealed in the vault, shown once, rotation, rate limit);
> steps may carry a `when` condition on an earlier step and `on_failure: continue`; skipped steps are recorded.
> Details in §5.11.
>
> **v0.28** — **external AD/LDAP (FR-07) and external secret manager (FR-86)**: configurable login attribute
> (`sAMAccountName`), UPN filter, verified StartTLS, no referrals, nested groups, and
> `deploy/compose.external-directory.yaml`; master key in OpenBao / Vault Transit (`openbao-transit`) or secret
> content in KV v2 (`openbao-kv`), `janus-ops secrets status|migrate|prune`, readiness that checks the secret
> manager. ADR-011 implemented and tested against real OpenBao 2.4.1 and Vault 1.20.
>
> **v0.36** — **release 0.10.0** (migrations `0032`–`0038`):
> - **Windows agent** (FR-85, now built): Windows service `JanusAgent` with recovery actions, metrics (CPU, memory, swap,
>   file systems, network without filter interfaces, uptime, processes), one-time enrollment and a PowerShell install
>   one-liner that pins the server CA (`POST /agents/{id}/install-command`, `/agent/install.ps1`). Shell, signed update,
>   listening ports and service discovery on Windows are not done.
> - **Workflows**: runs interrupted by a restart resume between steps (a run with an `exec` step in flight is marked failed).
>   **GitOps**: private https repositories with a vault `git_token` credential.
> - **AI providers and fallback routes** (ported from AIFRED, `docs/AI_PROVIDERS.md`, migration `0037`): providers with
>   presets, several sealed keys each, models read or added by name and tested, routes with a failover chain (quota, rate limit,
>   outage, timeout, bad key, missing model), health and usage, `janus-ops ai-import-providers`.
> - **MCP tools for the Copilot** (`docs/MCP_PLAN.md`, migrations `0033`, `0034`): `lordraw/linux-mcp` and `lordraw/proxmox-mcp`
>   started with `docker run -i`, configured from the inventory, read tools always, operational tools under `ai.operate`
>   plus a global switch and per-environment *action plan only*; every call recorded (`ai_step`) and audited.
> - **Operate** (migrations `0035`, `0036`): a chat that carries out instructions on up to ten servers (several Linux hosts
>   and Proxmox nodes in one run), model or route chosen per chat, archive/rename/delete.
> - **Archive and retention** (migration `0038`): archive after X days, delete after Y (1 and 30), keep flag, search including
>   the recorded output, bulk delete; permissions `archive.read`, `archive.manage`.
> - **Console**: Settings in tabs, grouped collapsible menu, sortable tables with host and group filters.
> - Release 0.10.0: agent signatures renewed (the agent bytes changed), image published on Docker Hub.
>
> **v0.35** — **continuous aggregates and the 500-node load test** (NFR-05): see the F1 row of the metrics and `docs/SIZING.md`. Release **0.7.0** published (the live stack runs on it): migration `0025` on the real database materialised the history of 16M points in about 2 minutes in the background; charts of 1 h to 90 d answer in 50-100 ms.
> **v0.34** — **Ctrl+K palette (pages, nodes, SSH, run a check, ask the AI); undo/redo in the workflow editor (Ctrl+Z, Ctrl+Shift+Z or Y; typing in a field is one step; 100 steps; `lib/history.ts`, 8 tests) and a browser test of a notification step; Copilot and Ansible tried in the browser on the live stack**: the Copilot with the real NVIDIA model, Ansible with a real dry run on a node (see FR-05 and the F7/F6 rows). Release **0.6.0** (graph workflow editor) published on Docker Hub; the live stack runs on the published image.
> **v0.33** — **graph workflow editor**: nodes and branches on a canvas (React Flow), drag to reorder and connect, side panel,
> read-only graph with run status; `ui` positions stored with each step; see §5.11.
>
> **v0.32** — **flow control for agent updates** (`update_ack`): windowed transfer so a slow link cannot make the agent drop its
> connection; see §5.11.
>
> **v0.31** — **signed agent shipped with the image**: reproducible agent build, signatures committed in `agent/release/`,
> `Dockerfile` verification (`REQUIRE_SIGNED`), `janus-ops agent-verify`, release script check, `make agent/sign`.
>
> **v0.30** — **documents in English, visual workflow editor, release-key procedure**: `PLAN.md`, `specs.md` and `OPENBAO.md`
> translated; the workflow page gets a visual editor (flow of cards with branch badges, JSON tab, edit of existing
> workflows) with tested rules in `frontend/lib/workflows.ts` (`npm test`, also in CI); `docs/AGENT_RELEASE.md` describes
> keeping the release private key off the server and signing from the release image.
>
> **v0.29** — **real-node test, OpenBao in the stack, release 0.4.0**: PTY, signed update (single and mass) and
> rollback tried on real nodes; the systemd unit is no longer sandboxed when a shell is allowed, can replace the
> agent binary and has a rollback guard that does not depend on the binary; `deploy/compose.openbao.yaml` and
> `scripts/openbao-setup.sh` run an OpenBao with automatic unseal (`docs/OPENBAO.md`); the live stack runs on it.

**Tier: L** — multi-service system (backend, worker, frontend, Go agent), external integrations
(SSH, Docker, Proxmox, SNMP, LLM), high security requirements (remote command execution).

---

## ASSUMPTIONS

Every assumption is versioned. If one is invalidated, the "Impacts" column says what to review.

| ID | Assumption | Impacts |
|---|---|---|
| A1 | **Self-hosted, single-tenant** deployment (one organisation per installation) | Data model, RBAC, ADR-001 |
| A2 | ✅ *confirmed "for the time being"* — MVP scale: **≤ 500 nodes**, **≤ 20 concurrent users**, a single Janus-Ops server (no HA in the MVP) | ADR-002, ADR-005, Timescale sizing |
| A3 | Target runtime: **Docker Compose** on a Linux host; k8s not in the MVP | DevOps, deploy/ |
| A4 | ✅ *confirmed* — MVP agent for **Linux amd64/arm64** with systemd only; Windows and OpenRC postponed to F8+ | Agent, bootstrap |
| A5 | ✅ *revised v0.2* — MVP authentication: **LDAP directory as a sidecar container in the stack** (users and groups), Janus roles mapped to LDAP groups, **TOTP managed by Janus-Ops, optional per user** (*revised v0.6*), one local *break-glass* account for emergencies. Connection to an external corporate AD/LDAP: later SHOULD. See ADR-008 | Auth, F0, deploy |
| A6 | ✅ *revised v0.2* — LLM: **self-hosted models on hosts outside the stack** (Ollama / vLLM / SGLang), configurable OpenAI-compatible endpoint; **no cloud provider**. The AI is allowed access to **all operational data** (logs, metrics, audit, inventory). Only the vault secrets stay excluded | AI, security |
| A7 | The Janus-Ops server can reach the Proxmox APIs and the SSH hosts directly (or through a jump host) | F1, F5 |
| A8 | ✅ *decided v0.3* — Janus-Ops is released under **Apache-2.0** (`LICENSE`, `NOTICE`). Dependencies must have a compatible licence; for TimescaleDB see C12. Ansible (GPLv3) is invoked as an external process through ansible-runner (Apache-2.0), not included in the Janus-Ops code | Supply chain |

---

## 1. Critical analysis of the specifications

Points of the specifications that must be corrected, clarified or scaled down before writing code.

| # | Point in specs.md | Problem | Proposal |
|---|---|---|---|
| C1 | General scope (8 modules) | Equivalent to rebuilding Checkmk + Portainer + PVE UI + Termius + AWX + n8n + Copilot. Without rigid phases the project never reaches a usable MVP. | Roadmap of incremental phases, each releasable (§5). Each module starts with a MUST subset. |
| C2 | Agent: "gRPC over HTTP/2 **or** WebSocket" | Two protocols = double cost. gRPC/HTTP2 is often broken by corporate proxies with TLS inspection. | **WSS only** on 443 with stream multiplexing (ADR-002). |
| C3 | Scheduler: "Taskiq / Celery" + workflow "Temporal or Celery Canvas" | Three candidate engines for two needs. Temporal adds a cluster to run. Celery is not natively async. | A single async engine for tasks + an internal workflow engine persisted in Postgres (ADR-003). Temporal reconsidered only if complex long-running workflows are needed. |
| C4 | "Proxmoxer (**asynchronous** Python wrapper)" | Proxmoxer is synchronous `[VERIFY: async support in current versions]`. | Thin async HTTP client (httpx) over the PVE REST APIs, or proxmoxer in a thread pool. Decision in F5. |
| C5 | Docker "through the agent tunnel **or** `ssh://`" with aiodocker | aiodocker does not handle `ssh://` natively `[VERIFY]`. | The agent exposes the Docker Engine API as a stream in the tunnel; the backend speaks Docker HTTP over that stream. `ssh://` only as a future fallback. |
| C6 | Proxy with **NTLM** authentication | Not supported by the Go stdlib; needs a third-party library `[VERIFY: maintained library]`. | MVP: proxy without auth + Basic + custom CA. NTLM as a SHOULD in a later phase. |
| C7 | Agent: token + CA certificate | A static clear-text token on disk is the only authentication secret. | Enrollment with a **single-use** token → issuance of a client certificate per agent (**mTLS**), periodic rotation. |
| C8 | Agent auto-update | Distributing binaries to the whole fleet is a mass-compromise vector. | **Signed** binaries (ed25519) with a release key separate from the server; the agent verifies signature and checksum before replacing itself. |
| C9 | "Remote Command / Task Executor" in the agent | In effect RCE by design: the Janus-Ops server becomes the most critical target of the infrastructure. | Local policy on the agent (`allow_exec`, `allow_pty`, `allow_docker`) that the server cannot override; everything audited. |
| C10 | AI RCA on logs | Logs are untrusted input → **prompt injection** (a log can contain instructions). | Read-only AI tools; output never executed without approval; vault secrets never exposed to AI tools; traffic to external LLM endpoints only over TLS (A6). |
| C11 | "Immutable Audit Log" | A SQL table is not immutable to a DB admin. | Append-only (no UPDATE/DELETE via grants) + hash chain to reveal tampering; optional external export. |
| C12 | TimescaleDB | ✅ *verified v0.2*: two editions. **Apache 2 Edition** (core, hypertable) and **Community Edition** under the **Timescale License (TSL)**, which includes compression and continuous aggregates. The TSL is free for self-hosted use, including commercial; it forbids offering TimescaleDB as a managed database service. | Use the Community Edition (official image), which is the default (`SHOW timescaledb.license;` → `timescale`). Janus-Ops uses the DB through SQL without modifying its code and does not sell it as DBaaS, so the TSL does not constrain the Janus-Ops licence. |
| C13 | Next.js "15" | Version to be verified at bootstrap time `[VERIFY: latest stable]`. Tailwind: the files in `res/` use `@import "tailwindcss"` → **Tailwind v4**. | Pin the versions in F0. |
| C14 | Files in `res/` | ✅ *resolved v0.2*: the files were renamed. | — |
| C15 | Brand name | ✅ *resolved v0.3*: official spelling **Janus-Ops**. In the UI wordmark: "Janus-" in `--text-primary`, "Ops" in `--accent-primary`. For technical identifiers: `janus-ops` / `janus-agent`. | — |

---

## 2. Product Owner — Requirements

### 2.1 Functional requirements (MVP = F0–F3, then per phase)

| ID | Requirement | Priority | Phase |
|---|---|---|---|
| FR-01 | Unified node inventory with tags, groups, environment (`prod/lab/dmz`) and activatable capabilities | MUST | F0 |
| FR-02 | Encrypted vault for SSH keys, sudo passwords, API tokens | MUST | F0 |
| FR-03 | Login with users of the sidecar LDAP directory, roles mapped to LDAP groups. **AMEND Product Owner (v0.6)**: **optional** TOTP per user (it was mandatory), at the user's request | MUST | F0 |
| FR-08 | Initial administrator created from `.env` at first start, only if absent | MUST | F0 |
| FR-09 | User management from the UI: create, edit, roles, disable, reset password, reset 2FA, delete | MUST | F0 |
| FR-10b | Configurable roles: LDAP group + set of permissions; custom roles; who reads the audit log is a permission (`audit.read`) | MUST | F0 |
| FR-11b | TOTP working without internet access (corporate proxies, air-gapped) | MUST | F0 |
| FR-06 | Local *break-glass* account, usable even when LDAP is unavailable, every use flagged in the audit | MUST | F0 |
| FR-07 | Optional connection to an external corporate AD/LDAP instead of the sidecar. **Done (v0.28)**: configurable login attribute (`sAMAccountName`), filter with UPN, verified StartTLS, referrals never followed, nested groups with the `LDAP_MATCHING_RULE_IN_CHAIN` rule, `deploy/compose.external-directory.yaml`. Tested with an in-memory LDAP server (`test_ldap_ad_unit.py`); **not tested on a real AD** | SHOULD | F8 |
| FR-04 | Audit log of every privileged action, searchable and filterable | MUST | F0 |
| FR-05 | `Ctrl+K` command palette to navigate to nodes and actions | SHOULD | F0 |
| FR-10 | 1-click web SSH session on a node, with credentials from the vault | MUST | F1 |
| FR-11 | Multi-tab / split, SFTP browser, upload/download | SHOULD | F1 |
| FR-12 | Command broadcast to several terminals, snippet library | SHOULD | F1 |
| FR-13 | Session recording (asciinema) | SHOULD | F1 |
| FR-14 | ProxyJump through a jump host | SHOULD | F1 |
| FR-20 | Agent bootstrap over SSH (detect arch, copy binary, config, systemd) | MUST | F2 |
| FR-21 | Agent connected outbound over WSS, HTTP(S) proxy with custom CA | MUST | F2 |
| FR-22 | Telemetry every 5–15 s: CPU, RAM/swap, disk I/O, filesystem, systemd units, ports, top processes | MUST | F2 |
| FR-23 | Local on-disk buffer during disconnection and deferred sending | MUST | F2 |
| FR-24 | NOC dashboard: hexagon widget, node cards, unhandled-problems table | MUST | F2 |
| FR-30 | OK/WARN/CRIT/UNKNOWN states with thresholds and hysteresis (N cycles) | MUST | F3 |
| FR-31 | Acknowledge, scheduled downtime | MUST | F3 |
| FR-32 | Parent/child dependencies with suppression of child alerts | MUST | F3 |
| FR-33 | Flapping detection | SHOULD | F3 |
| FR-34 | Telegram, Webhook, Email notifications; Slack/Discord | MUST (first 3) / SHOULD | F3 |
| FR-35 | Automatic service discovery with check proposal | SHOULD | F3 |
| FR-40 | Docker: container list per host, start/stop/restart/kill, inspect, log streaming, stats | MUST | F4 |
| FR-41 | Exec shell in the container from the browser | MUST | F4 |
| FR-42 | Compose stack: YAML editor, encrypted `.env`, deploy/redeploy | SHOULD | F4 |
| FR-43 | Volumes, networks, selective prune, image update checks | SHOULD | F4 |
| FR-50 | Proxmox: cluster/quorum state, nodes, storage | MUST | F5 |
| FR-51 | VM/LXC: start/shutdown/reboot/snapshot/clone/migration | MUST | F5 |
| FR-52 | VM/LXC console (xterm.js serial; noVNC) | SHOULD | F5 |
| FR-53 | vzdump/PBS backup audit: missing, failed, too old | MUST | F5 |
| FR-60 | Dynamic Ansible inventory from the DB | MUST | F6 |
| FR-61 | Playbook catalogue from several sources: Git, local file upload, built-in editor, network mount (NFS/SMB), SFTP/SCP; survey, execution with live output, history | MUST | F6 |
| FR-70 | AI "Diagnose" on a CRIT service: log/metric/audit collection → diagnosis + action plan | MUST | F7 |
| FR-71 | Action plan approvable/editable before execution | MUST | F7 |
| FR-72 | Contextual IaC generator | SHOULD | F7 |
| FR-80 | Visual workflow (cron/webhook/manual/alert triggers; SSH/Ansible/Docker/Proxmox/AI/approval/notification actions) | SHOULD | F8 |
| FR-81 | GitOps compose with auto-deploy webhook | SHOULD | F8 |
| FR-82 | SNMP (v2c/v3) for NAS, switches, UPS | SHOULD | F8 |
| FR-83 | PTY and remote commands through the agent tunnel | SHOULD | F8 |
| FR-84 | Signed air-gapped agent auto-update | SHOULD | F8 |
| FR-85 | Windows agent | SHOULD — service, metrics and install built in 0.10; shell, update and discovery pending | F8+ |
| FR-86 | Secrets and keys in an external secret manager (OpenBao, HashiCorp Vault or similar): master key (KEK) and/or secret content outside Janus-Ops. **AMEND Product Owner (v0.9)**: from WON'T to SHOULD, at the user's request; design in ADR-011. **Done (v0.28, option a of ADR-011)**: `JANUS_SECRET_BACKEND=openbao-transit`, the master key in the Transit engine of OpenBao/Vault (AppRole, minimal encrypt/decrypt policy, token renewal, DEK cache `JANUS_BAO_DEK_CACHE_S`), `janus-ops secrets status` and `secrets migrate --to ...` (DEK re-wrap only, idempotent, reversible, all or nothing, audit), readiness that checks the secret manager, TOTP included. Tested against a real OpenBao 2.4.1 and a real HashiCorp Vault 1.20 (Transit, key rotation without re-wrap, KV v2). **Option (b) `openbao-kv`** done: the content of each secret lives in KV v2, Postgres keeps only a reference (`kv1:` + mount, path and pinned version), the path derives from the record id and a reference pointing elsewhere is refused; `secrets migrate --to openbao-kv` and back, deleting a credential removes its content from KV, `secrets prune` for orphans (deleted GitOps stacks, workflows and channels do not do it by themselves). TOTP stays on the Transit or local key. **OpenBao in the stack** (`deploy/compose.openbao.yaml`, `scripts/openbao-setup.sh`, `docs/OPENBAO.md`): raft storage, TLS with its own CA, **auto-unseal with a static key** on restart (tested: restart of the container and of the whole stack), AppRole and minimal policy; the live stack has run on it since 2026-11-17 with `openbao-transit` (18 secrets migrated). A defect found in the real migration (channel secrets are sealed under `channel-<id>`) is fixed and covered by tests. **Not yet:** "by reference" credentials pointing to secrets of other teams (read-only) | SHOULD | F8+ |
| FR-87 | Multi-tenancy, server HA | WON'T (MVP) | — |

### 2.2 Non-functional requirements

| ID | Requirement | Verification |
|---|---|---|
| NFR-01 | Agent: single static binary, no runtime dependencies, no listening ports | `ldd` → "not a dynamic executable"; `ss -ltnp` on the node does not show the agent |
| NFR-02 | Agent working behind an HTTP(S) proxy with custom CA | E2E test with a proxy (e.g. squid) in the test compose |
| NFR-03 | No metric loss for disconnections ≤ 1 h | Test: 60-minute disconnection → no gap in the data |
| NFR-04 | Latency from ingest to dashboard state ≤ 30 s (p95) | End-to-end measurement in a load test |
| NFR-05 | Sustained ingest for 500 nodes at 10 s (~10k points/s estimated) | Synthetic load test |
| NFR-06 | Secrets never sent to the browser, never in the logs | Automated tests on responses/logs + review |
| NFR-07 | Every privileged action produces an attributed audit event | Test for each mutating endpoint |
| NFR-08 | Installation from a clean clone with documented commands only | "Fresh install" CI job |
| NFR-09 | UI usable at ≥ 1280 px; dashboard readable at 1920 px (NOC wall) | Visual review / Playwright screenshot |

### 2.3 MVP acceptance criteria (excerpt)

| ID | Criterion (observable from outside) | Method |
|---|---|---|
| AC-01 | An Operator adds a host with IP + SSH credential and sees the "SSH Target" capability active | Playwright E2E |
| AC-02 | An Auditor cannot open a shell or edit nodes (HTTP 403, buttons absent) | API test + E2E |
| AC-02b | A user added to the LDAP group `janus-operators` gets the Operator role at the next login; removed from the group, loses it | Integration test with an LDAP container |
| AC-02c | With the LDAP container stopped, only the break-glass account can sign in and the event appears in the audit | Integration test |
| AC-03 | When a shell is opened, the audit log records user, node, start/end timestamp | API test |
| AC-04 | Agent bootstrap on Debian and Rocky (amd64/arm64): the host becomes "online" within 60 s | E2E on test VMs/containers |
| AC-05 | Blocking the outbound traffic for 10 min, the time series is continuous after restoration | Integration test |
| AC-06 | CPU > CRIT threshold for 3 cycles → CRIT state and webhook notification; for 2 cycles → no alert | Integration test |
| AC-07 | With the parent node DOWN, the children are "UNREACHABLE" and generate no notifications | Integration test |
| AC-08 | During a scheduled downtime no notification is sent | Integration test |

---

## 3. Architect — Components, flows, ADRs

### 3.1 Components

```mermaid
flowchart TB
    subgraph Edge
        RP["Reverse proxy<br/>TLS termination :443"]
    end
    subgraph App
        FE["frontend<br/>Next.js"]
        API["api<br/>FastAPI REST + WS"]
        GW["agent-gateway<br/>WSS mTLS, multiplexing"]
        WK["worker<br/>task queue consumers"]
        SCH["scheduler<br/>cron, check timers"]
    end
    subgraph Data
        PG[("PostgreSQL + TimescaleDB")]
        RD[("Redis<br/>broker, pub/sub, cache")]
        LD[("LDAP directory<br/>sidecar")]
    end
    AG["janus-agent"]
    LLM["LLM self-hosted<br/>(external hosts)"]

    RP --> FE
    RP --> API
    RP --> GW
    AG -->|WSS| RP
    API --> PG
    API --> RD
    GW --> RD
    GW --> PG
    WK --> PG
    WK --> RD
    SCH --> RD
    API --> LD
    WK -->|TLS| LLM
```

| Component | Responsibility | Requirements covered |
|---|---|---|
| reverse proxy | TLS, path-based routing, single :443 entry point | NFR-01, NFR-02 |
| frontend | UI, real-time state over WS | FR-05, FR-24, all the views |
| api | Auth, RBAC, inventory, vault, audit, REST, WS to the browser, SSH bastion | FR-01..14, FR-40..72 (API side) |
| agent-gateway | Agent tunnel termination, enrollment, stream routing (docker/exec/pty), batch metric ingest | FR-20..23, FR-40, FR-83 |
| worker | Check evaluation, alerting, notifications, Ansible, Proxmox, AI, workflows | FR-30..35, FR-50..53, FR-60..80 |
| scheduler | Check timers, workflow cron, retention | FR-30, FR-80 |
| ldap (sidecar) | User and group directory; reachable only on the internal network of the compose | FR-03, FR-06 |
| janus-agent | Telemetry, docker proxy, exec/PTY, buffer, update | FR-20..23, FR-83, FR-84 |

In the MVP `agent-gateway` can be a module of the same `api` process; it remains separable
because it communicates only through Redis/PG (scalability beyond A2).

### 3.2 Repository structure

Current state (v0.4). Folders marked *(F…)* arrive with the indicated phase.

```
janus-ops/
├── backend/
│   ├── pyproject.toml, uv.lock     pinned dependencies (uv)
│   ├── src/janus_ops/
│   │   ├── cli.py                  janus-ops serve | migrate | version
│   │   ├── config.py               Settings from JANUS_* variables
│   │   ├── db.py, health.py        async engine, /health, /ready
│   │   ├── main.py                 app factory, security headers, static UI
│   │   ├── logsetup.py             JSON logs on stdout
│   │   ├── migrations/             Alembic (hand-written SQL, no autogenerate)
│   │   └── modules/  (F0+)         auth, inventory, vault, audit, ssh, monitoring, ...
│   └── tests/                      unit + integration (real TimescaleDB)
├── frontend/
│   ├── app/, components/           App Router, Sidebar, StatusBadge
│   └── scripts/sync-assets.mjs     copies icons and logo from res/ to public/
├── agent/  (F2)                    janus-agent in Go
├── proto/  (F2)                    agent ⇄ server contracts
├── deploy/                         compose.yaml, compose.build.yaml, Caddyfile, .env.example
├── scripts/                        gen-env.sh, integration-test.sh, release.sh
├── docs/                           specs, PLAN, dockerhub/
├── res/                            brand & design resources
├── Dockerfile, .dockerignore       single image (ADR-009)
├── .github/workflows/ci.yml        lint, test, integration, build; push on v* tags
└── Makefile
```

### 3.3 ADRs

**ADR-001 — Monorepo**
- *Options*: (a) single monorepo — atomic versioning of the agent⇄server contracts, single CI; (b) separate repos — independent releases, but duplicated contracts and drift.
- *Decision*: **(a)**. The `proto/` contracts must change together with the agent and the gateway.
- *Consequences*: CI with one job per language filtered by path.

**ADR-002 — Agent transport: WSS + multiplexing**
- *Options*: (a) bidirectional gRPC over HTTP/2 — typed, but fragile with TLS-inspecting proxies and some L7 LBs; (b) WebSocket over HTTP/1.1 Upgrade + a stream multiplexer (e.g. yamux `[VERIFY: library]`) + protobuf messages — maximum proxy compatibility; (c) SSH reverse tunnel — mature, but port 22 is often blocked and key management is more complex.
- *Decision*: **(b)**. One control channel + dedicated streams for metrics, docker, exec, PTY.
- *Consequences*: heartbeat, backpressure and reconnect (exponential backoff with jitter) are implemented by hand. mTLS authentication (per-agent cert from enrollment, see C7). If a TLS-inspecting proxy breaks mTLS: fallback to a short-lived signed token inside the `OPEN` tunnel.
- **AMEND Architect (v0.11)**: implemented in F2 with three deviations, contract in [`proto/agent-protocol.md`](../proto/agent-protocol.md).
  1. *Authentication*: the fallback becomes the main solution. The agent generates an ed25519 key on the node (it never leaves the node); on each connection it signs a random server nonce bound to the node id and the server host. The single-use bootstrap token registers the key (only the SHA-256 hash in the DB, TTL 15 min, invalidated by a new bootstrap). Reasons: TLS is terminated by Caddy (ADR-009), so a client certificate would not reach the app without a second port and trusted headers; it works on 443 behind proxies that inspect TLS; no CA to manage or rotate. Residual risk: whoever intercepts TLS (a CA trusted by the agent) and forwards the challenge in real time can impersonate the agent for that connection, as with any bearer; they cannot reuse the signature elsewhere or later. Revocation = refusal of the key (`revoked_at`).
  2. *Encoding*: JSON messages (field `t`) instead of protobuf: low volumes (≤ 500 nodes every 10 s), readable in the logs, no codegen in two languages; the `v` field allows changing the encoding.
  3. *Multiplexing*: postponed to F4, when the Docker streams will be needed; in F2 there is a single channel. It will be done with binary frames carrying a stream id over the same WebSocket (yamux `[VERIFY]` is not needed).

**ADR-003 — Task queue and workflow**
- *Options*: (a) Celery + Canvas — mature, but sync-first and Canvas poorly suited to workflows with human waits; (b) Taskiq (async) + an internal workflow engine (DAG persisted in PG, idempotent steps) — consistent with async FastAPI, state inspectable in SQL; (c) Temporal — excellent durability, but one more heavy service to operate.
- *Decision*: **(b)** `[VERIFY: Taskiq maturity at bootstrap; equivalent alternative: arq]`.
- *Consequences*: the workflow engine (F8) must be designed with explicit states (pending/running/waiting_approval/failed/done) and idempotent retries.

**ADR-004 — Secret vault**
- *Options*: (a) application-level AES-256-GCM encryption with envelope encryption (a DEK per secret, KEK from a master key in env/file) — no dependencies; (b) external OpenBao/Vault — more robust but one more service; (c) pgcrypto — key visible to the DB.
- *Decision*: **(a)** behind a `SecretBackend` interface, so that (b) becomes an additional implementation (FR-86).
- *Consequences*: documented KEK rotation procedure; the master key never goes into the DB or into the DB backups.
- *Implementation (v0.7)*: KEK = `JANUS_SECRET_KEY`; one random DEK per credential; secret and DEK encrypted with AES-256-GCM with AAD bound to the credential id (rows cannot be swapped); `kek_version` for the future KEK rotation (DEK re-wrap only). Write-only secrets through the API; `reveal()` reserved to the modules that will use them (F1+). SSH keys validated on saving (OpenSSH/PEM, passphrase through `cryptography[ssh]`/bcrypt), with type and fingerprint stored in `public_info`. KEK rotation: not implemented yet `OPEN`.

**ADR-005 — Metrics storage**
- *Options*: (a) TimescaleDB in the same Postgres — one DB, SQL and JOINs with the inventory; (b) Prometheus/VictoriaMetrics — excellent for metrics, but a second system and a separate data model.
- *Decision*: **(a)** with a hypertable, compression after 7 days, 1m/1h continuous aggregates, raw retention 30 days `OPEN` (see C12).
- *Consequences*: batch ingest (COPY / multi-row insert) from the gateway; "narrow" metrics schema (`time, node_id, metric_id, value`) + a `metric_series` table for the labels.

**ADR-006 — Frontend and session**
- *Options*: (a) Next.js as a mostly client-side SPA, FastAPI API on the same origin through the reverse proxy, session in an httpOnly cookie; (b) Next.js as a BFF proxying the APIs — more code, double auth.
- *Decision*: **(a)**. No secrets or authorisation logic in the Next layer.
- *Consequences*: CSRF token for mutating requests; WebSockets authenticated with the same cookie + Origin check.
- **AMEND Architect (v0.4)**: the frontend is built with `output: 'export'` (static files) and served by the FastAPI process, not by a Next.js server — to allow the single image (ADR-009). Consequences: no SSR, Server Actions or dynamic routes without `generateStaticParams`; detail pages will use query-string parameters or client-side routing. No requirement depends on it, because with (a) all the rendering was already client-side.

**ADR-009 — Packaging: single image**
- *Context*: a single Docker Hub repository (`lordraw/janus-ops`) and a simple deploy for air-gapped environments.
- *Options*: (a) **single image**: API + exported UI, the same image also for `migrate` and for the future workers (different command) — one artefact, one version; (b) separate `-api` and `-frontend` images — they scale separately, but two repositories and an extra Node container with no benefit, since the UI does no SSR.
- *Decision*: **(a)**. Multi-stage build: Node 24 for the UI export → uv for the virtualenv → `python:3.12-slim` runtime, non-root user `10001`.
- *Consequences*: TLS outside the image (Caddy in the reference stack); the future roles (worker, scheduler, agent-gateway) will be subcommands of `janus-ops` on the same image; the agent binaries (F2) can be included in the image for the air-gapped auto-update.

**ADR-010 — Configurable roles and permissions (v0.6)**
- *Context*: the user asks that access to the audit log (and in general to the modules) be configurable from user management.
- *Options*: (a) fixed roles in code (state v0.5) — simple but every change requires a release; (b) **permissions defined in code, roles in the database** — each role = LDAP group + set of permissions, editable from the UI; (c) policy engine (e.g. OPA/Casbin) — powerful but disproportionate for the MVP.
- *Decision*: **(b)**. Permission catalogue in `rbac.py` (`audit.read`, `users.read`, `users.manage`, `roles.manage`; it grows with the modules). Built-in roles `admin` (all permissions, not editable), `operator`, `auditor`; custom roles can be created. Several roles = union of the permissions.
- *Consequences*: the session stores the roles at login; permissions are recomputed on every request, so a change takes effect immediately even for those already connected. Anti-escalation: only an admin can assign/remove the admin role or act on an admin; nobody can delete or disable themselves or remove their own admin role. Whoever has `roles.manage` can however give themselves other permissions: that is why by default only admin has it. `JANUS_LDAP_GROUP_*` removed (AMEND ADR-008).

**ADR-011 — External secret manager: OpenBao / HashiCorp Vault (proposed v0.9; implemented v0.28–v0.29)**
- *Context*: today (ADR-004) secrets are encrypted in the database with a master key (`JANUS_SECRET_KEY`) that lives in the `.env` file. The user asks to be able to keep secrets and keys in a dedicated secret manager, as many organisations already do.
- *Options*:
  (a) **External KEK** — the *Transit* engine of OpenBao/Vault encrypts and decrypts the keys of the individual secrets (DEK); the secrets stay encrypted in Postgres. The master key never leaves the secret manager and is no longer needed in `.env`; rotation is Transit's (`rotate` + `rewrap`). Minimal change to the current model.
  (b) **External secrets** — the content of the secret lives in the *KV v2* engine (e.g. `janus-ops/credentials/<id>`); Postgres keeps only metadata and a reference (mount, path, version). It also allows "by reference" credentials pointing to secrets already managed by other teams, read-only.
  (c) stay on the local vault only — no dependency, but the master key remains a file to protect by hand.
- *Proposed decision*: **(a) and (b) both available, (c) remains the default**, selectable with `JANUS_SECRET_BACKEND=local|openbao-transit|openbao-kv` behind the `SecretBackend` interface already foreseen by ADR-004 (the current `Vault` class becomes its `local` implementation). First (a), simpler and already enough to remove the master key from `.env`; then (b).
- *Products*: **OpenBao** (the Linux Foundation fork of Vault, MPL-2.0 licence, compatible with Apache-2.0) as the reference and only candidate for a possible sidecar in the stack; **HashiCorp Vault** with the same API (BSL 1.1 licence since 2023: usable by the user as an external service, not distributable by us in the stack); other products (Infisical, CyberArk Conjur, cloud KMS) through the same interface, cloud KMS only where air-gapped operation is not needed.
- *Janus-Ops access to the secret manager*: AppRole (`role_id` + `secret_id` from a file or variable), automatically renewed tokens, TLS with a configurable CA, minimal policies (only `encrypt`/`decrypt` on the Transit key; only the `janus-ops/` prefix on KV).
- *Scope*: vault credentials, TOTP seeds, and later the private key of the agent CA (F2) and the notification secrets (F3).
- *Migration*: command `janus-ops secrets migrate --to <backend>` that moves or re-encrypts all the existing credentials, idempotent and recorded in the audit; going back with the same command.
- *Consequences*: the secret manager becomes a runtime dependency: if it does not answer, SSH and everything that uses credentials fail with `vault_unavailable` (login does not, if TOTP is not active). No clear-text cache; a possible in-memory cache of the decrypted DEKs for a few minutes `OPEN`. An OpenBao in production requires its own unseal management and backups: document it; "dev" mode is not allowed. `[VERIFY: Python client (hvac or direct HTTP) and Transit/KV API parity between OpenBao and Vault]`
- *Outcome (v0.28–v0.29)*: both options implemented and tested against real OpenBao 2.4.1 and Vault 1.20; the Python client is a standard-library HTTP client (no `hvac`); the DEK cache exists (`JANUS_BAO_DEK_CACHE_S`, default 300 s); API parity confirmed. The stack ships an OpenBao with automatic unseal (`docs/OPENBAO.md`).

**ADR-012 — SNMP client (F8, FR-82)**
- *Context*: NAS, switches and UPS are read through SNMP. A client is needed that speaks v2c and v3 with authentication and encryption.
- *Options*: (a) **pysnmp** (lextudio, BSD): pure Python, asyncio, complete v3 USM; the only transitive dependency is `pyasn1`, already in the lockfile; (b) `net-snmp` (`snmpget`) as a binary in the image: it works, but it is one process per read, text parsing of the output and v3 key handling on the command line (the passphrase would end up in the process arguments); (c) own implementation of BER and USM: no dependency, but hand-written cryptography and parsing of a protocol that is easy to parse wrongly is an unacceptable risk.
- *Decision*: **(a)**, `pysnmp>=7.1.33` (a line of justification in `backend/pyproject.toml`). A dependency to keep: without it, FR-82 cannot be built without (b) or (c).
- *Security choices*: only GET of numeric OIDs (no SET, walk, trap, MIBs to load); v3 with SHA/SHA-256 and AES-128/256; MD5 and DES not offered; v2c offered because many devices use it, but the community travels in clear text (documented residual risk); `noAuthNoPriv` and `authNoPriv` exist only as an explicit operator choice, the default is `authPriv`. Each read uses a new `SnmpEngine`: pysnmp binds an engine to the loop that uses it first.
- *Consequences*: pysnmp has no type stubs: in `pyproject.toml` the module is excluded from mypy (`ignore_missing_imports`), values stay `Any` at the boundary.

**ADR-007 — AI integration**
- *Decision*: OpenAI-compatible client towards **external self-hosted** endpoints (A6), with **read-only** tools exposed by the backend (get_logs, get_metrics, get_audit, get_inventory). LiteLLM only if routing between several endpoints/models is needed; otherwise the SDK directly `[VERIFY in F7]`. Actions are produced as a structured *Action Plan* and go through the normal RBAC + approval flow.
- *Consequences*: no redaction of operational data (allowed by A6); vault secrets are never reachable by the tools; every prompt/response recorded in the audit; several endpoints configurable (e.g. a small model for triage, a large one for RCA).

**ADR-008 — User directory: LDAP sidecar**
- *Context*: A5 requires LDAP as a sidecar container in the stack, so that users and groups are managed outside the application DB.
- *Options*: (a) **lldap** — a lightweight LDAP server with a web interface for users and groups; one container, little configuration, but it implements only a subset of LDAP `[VERIFY: group/memberOf attributes, LDAPS support]`; (b) **OpenLDAP** (Bitnami image or similar `[VERIFY: maintained image]`) — full LDAP, but schema and ACL management is burdensome and no interface is included; (c) **389-DS / FreeIPA** — complete enterprise directories, too heavy for a sidecar.
- *Decision*: **(a) lldap**, behind a `DirectoryBackend` interface based on standard LDAP (bind + search). So moving to OpenLDAP or to an external AD (FR-07) requires configuration only.
- *Details (implemented, v0.5)*:
  - **Flow**: bind with the `janus-svc` service account (group `lldap_strict_readonly`) → search of the user with a configurable filter and escaped value → exactly one result, otherwise invalid credentials → bind with the user's DN and the entered password → groups from `memberOf`.
  - **Roles**: from the CN of the groups (`JANUS_LDAP_GROUP_*`, default `janus-admins` / `janus-operators` / `janus-auditors`); the highest role wins; no group = access denied (403 `no_role`). The role is snapshotted in the session at login.
  - **Client**: **ldap3** 2.9.1 (pure Python) in a thread pool. Alternatives evaluated: *bonsai* (async and maintained, but no Linux wheel → compilation with libldap in the image) and *python-ldap* (C extension, same problem). **Residual risk**: last ldap3 release in 2021 and use of deprecated pyasn1 APIs (warnings filtered in the tests). Mitigations: versions pinned in `uv.lock`, integration tests on a real lldap in CI, `DirectoryBackend` interface to replace it.
  - **Provisioning**: the `ldap-init` job uses `/app/bootstrap.sh` of the lldap image (idempotent) to create groups and service accounts. The image's `curl` does not use Docker's DNS: the service name is resolved with `getent`.
  - **Sessions**: random 256-bit token in a `HttpOnly; Secure; SameSite=Strict` cookie, only the SHA-256 in the DB; duration 8 h, inactivity 1 h; logout and break-glass rotation revoke the sessions.
  - **Protections**: max 5 failures per username and 30 per IP in 15 minutes (in memory, single process as per A2); `/api` writes with an Origin/Referer different from the host are refused (CSRF); empty passwords refused before the bind; usernames limited to `[A-Za-z0-9._@-]{1,64}`.
  - **User management (v0.6)**: `janus-manager` account in the `lldap_admin` group; users and groups through lldap's GraphQL API (HTTP with the standard library), password through LDAP Password Modify (RFC 3062), because lldap stores passwords with OPAQUE and GraphQL cannot set them. With an external directory (`JANUS_LLDAP_HTTP_URL` not set) user management is read-only, disabling on the Janus side remains possible.
  - **TOTP (v0.6)**: RFC 6238 implemented with the standard library (verified with the official vectors), QR generated locally (`segno`), secrets encrypted with AES-256-GCM (`cryptography`) with `JANUS_SECRET_KEY` and bound to the user; ±1 step window, anti-replay, 10 single-use recovery codes, 5 failures close the pending session; after the second factor a new token is issued. No network calls: it works behind corporate proxies and air-gapped.
  - **Break-glass**: a single local account (`JANUS_BREAKGLASS_USERNAME`), scrypt hash (stdlib), set with `janus-ops breakglass`; its name never goes through LDAP.
- *Consequences*: LDAP becomes a dependency of login, mitigated by the break-glass account (FR-06). The LDAP container exposes no ports outside the compose; its web interface must be published only behind the reverse proxy and only for Admins `OPEN`. The LDAP data volume enters the backup procedure.
- *External directory (v0.28, FR-07)*: the same `DirectoryBackend` serves an external LDAP / Active Directory with a configurable login attribute (`sAMAccountName`), a filter that can also match the UPN, verified StartTLS, no referrals, and nested groups through the `LDAP_MATCHING_RULE_IN_CHAIN` rule; `deploy/compose.external-directory.yaml` runs the stack without the sidecar. Tested with an in-memory LDAP server; not yet on a real AD.

### 3.4 Main flows

**Agent bootstrap (F2)**
1. An Operator enables "Janus Agent" on a node with the SSH capability.
2. Worker: SSH → `uname -m`, detect the init system → SFTP of the binary to `/usr/local/bin/janus-agent`.
3. The server generates a **single-use enrollment token** (short TTL) + writes `/etc/janus-agent/config.yaml` (URL, CA, token, proxy, policy).
4. systemd unit started → the agent connects, presents the token, receives a client certificate (CSR generated on the agent: the private key never leaves the node).
5. The token is invalidated; the node goes "online".

**Telemetry → state → notification (F2–F3)**
agent (batch every N s, bbolt buffer) → gateway (validation, batch insert) → Redis pub/sub → evaluator in the worker (thresholds, hysteresis, dependencies, downtime) → `problem` opened/closed → notification router → WS to the browser.

---

## 4. Data Engineer — Data model (logical)

| Entity | Key fields | Notes |
|---|---|---|
| `user` | id, ldap_dn, username, totp_secret_enc, totp_enabled, totp_last_step, is_breakglass, pwd hash (scrypt, break-glass only), is_active, last_login | Local profile created at the first LDAP login |
| `role`, `role_permission` | name, description, ldap_group (unique), builtin; (role, permission) | **AMEND Data Engineer (v0.6)**: they replace `role_mapping` (migration `0002`) |
| `user_session` | token_hash, roles[], stage (`mfa_pending`/`full`), mfa_failures, expiries | `0002`: several roles per session and the TOTP step |
| `totp_recovery_code` | user_id, code_hash (SHA-256), used_at | Single-use codes |
| `node` | id, name, fqdn/ip, environment, parent_id, created_by | `parent_id` → dependencies (FR-32) |
| `node_capability` | node_id, type (`agent/ssh/proxmox/docker/snmp`), config jsonb, credential_id | |
| `tag`, `node_tag`, `group`, `node_group` | | Dynamic Ansible inventory |
| `credential` | id, type, ciphertext, dek_wrapped, kek_version | Never returned in clear through the API |
| `agent` | node_id, cert_fingerprint, version, arch, last_seen, policy jsonb | |
| `enrollment_token` | hash, node_id, expires_at, used_at | Only the hash in the DB |
| `metric_series` | id, node_id, name, labels jsonb | Unique index (node_id, name, labels) |
| `metric_point` (hypertable) | time, series_id, value | Index (series_id, time DESC) |
| `check` | id, node_id, type, params, warn/crit, cycles, interval | |
| `check_state` | check_id, state, since, consecutive, last_output | Current state |
| `check_result` (hypertable) | time, check_id, state, output | History |
| `problem` | id, check_id, opened_at, closed_at, ack_by, ack_comment | "Unhandled problems" table |
| `downtime` | scope (node/check/group), start, end, author, reason | |
| `notification_channel`, `notification_rule` | | Escalation |
| `audit_event` | id, ts, actor, action, target, payload, prev_hash, hash | Append-only, hash chain |
| `terminal_session` | id, user, node, started, ended, recording_path | |
| `playbook`, `job_run`, `job_event` | | ansible-runner events |
| `gitops_stack`, `gitops_run` | | F8 (GitOps, migration `0015`): webhook secret sealed in the vault; at most one `running` run per stack |
| `workflow`, `workflow_run`, `workflow_step_run`, `workflow_approval` | | F8 (migrations `0021`, `0024`): definition as JSON, runs, one row per step (`skipped` included), approvals; webhook secret sealed in the vault |
| `ai_conversation`, `action_plan` | status (draft/approved/rejected/executed) | |

Consistency: transactional (PG) for inventory/vault/audit; eventual (≤ one check cycle) for the monitoring states.
Migrations: Alembic, up/down, reversible. The **F0–F3 DDL schema** is in migration
[`0001_initial_schema.py`](../backend/src/janus_ops/migrations/versions/0001_initial_schema.py):
every index has a comment with the query pattern that justifies it. Notable choices: enumerations as
`text` + `CHECK` (no `ALTER TYPE`), hypertables `metric_point` (7 d compression, 30 d retention) and
`check_result` (7 d compression, 90 d retention) without FKs to the parent tables, `audit_event`
append-only through triggers (UPDATE/DELETE/TRUNCATE blocked), at most one open problem per check
and a single break-glass account (partial unique indexes). Continuous aggregates postponed to F2, when
the dashboard queries will exist. The F4+ tables will arrive with dedicated migrations.

---

## 5. Roadmap by phase

Each phase ends with an installable release and its own acceptance criteria green.

| Phase | Content | Exit |
|---|---|---|
| **F0 Foundations** | Monorepo, Makefile, CI (lint/test per language), compose (proxy, app, migrate, PG+TS, LDAP sidecar), LDAP login + TOTP + break-glass, RBAC from LDAP groups, inventory CRUD, vault, audit with hash chain, UI shell (sidebar, command palette, Obsidian Graphite design tokens) | AC-01, AC-02, AC-02b, AC-02c |
| **F1 SSH Bastion** | AsyncSSH ⇄ WS ⇄ xterm.js, credentials from the vault, multi-tab, SFTP, ProxyJump, recording | AC-03 |
| **F2 Agent & Telemetry** | Go agent (tunnel, telemetry, buffer), gateway, mTLS enrollment, SSH bootstrap, Timescale ingest, NOC dashboard (hexagon, node cards) | AC-04, AC-05 |
| **F3 Alerting** | Check engine, hysteresis, problems, ack, downtime, dependencies, flapping, notifications, discovery | AC-06..08 |
| **F4 Containers** | Docker proxy in the agent, container views, logs/stats/exec, compose stacks | |
| **F5 Proxmox** | PVE client, cluster overview, VM/LXC lifecycle, backup audit, console | |
| **F6 Ansible** | Dynamic inventory, playbook catalogue from Git, local upload, built-in editor, network mounts (NFS/SMB), SFTP/SCP; survey, live runs, history | |
| **F7 AI Copilot** | Diagnose on a problem, read-only tools, action plan + approval | |
| **F8+** | Visual workflow, GitOps, SNMP, PTY/exec through the agent, signed auto-update, NTLM, external AD/LDAP, OIDC, Windows agent, **external secret manager OpenBao / HashiCorp Vault (ADR-011)** | FR-86 |

Rationale for the order: F1 gives immediate value without an agent and builds the bricks (vault, WS, audit) reused by everything else; F2–F3 are the "Checkmk" core; Docker comes before Proxmox because it reuses the tunnel just built.

### 5.1 F0 progress

| Item | Status | Verification |
|---|---|---|
| Monorepo, Makefile, `.env.example`, scripts | ✅ | `make help`; `make env` idempotent |
| F0–F3 DDL schema (migration `0001`) | ✅ | 13 integration tests on a real TimescaleDB 2.32: up/down/up round trip, hypertables and policies, append-only trigger, constraints |
| Backend: config, JSON log, `/health`, `/ready`, CLI | ✅ | 21 unit tests, ruff, mypy strict |
| UI shell (sidebar from the mockup, tokens, favicon, control-plane status) | ✅ | ESLint, `tsc`, static export |
| Single image + Compose (Caddy, app, migrate, TimescaleDB, lldap) | ✅ | Stack started: HTTPS, `/ready` = ok, repeated migration without effects |
| GitHub Actions CI + Docker Hub publish on tag | ✅ | actionlint, shellcheck; first run at the push |
| LDAP login (username + password), roles from groups, sessions, `require_role` | ✅ | 26 integration tests on real lldap + TimescaleDB, including AC-02b (group change through the lldap API) and AC-02c (LDAP off) |
| Break-glass (`janus-ops breakglass`) | ✅ | Integration test + test in the Compose stack |
| Audit with hash chain (write and verify) | ✅ | `verify_chain` in the tests; append-only trigger |
| Login page, guard on the pages, user and logout in the sidebar | ✅ | ESLint, `tsc`, static export; HTTPS test through Caddy |
| Initial administrator from `.env` (`janus-ops setup`, only if absent) | ✅ | Integration test + Compose stack (idempotent re-run) |
| User management (API + Users & roles page) | ✅ | Test: full lifecycle, validations, anti-escalation, anti-lockout |
| Configurable roles and permissions, custom roles (ADR-010) | ✅ | Test: `audit.read` permission granted/revoked with immediate effect on active sessions |
| Optional TOTP (setup with QR, two-step login, recovery, admin reset) | ✅ | RFC 6238 vectors; integration test; HTTPS test through Caddy |
| Audit viewer (API + UI, filters, chain verification) | ✅ | AC-03 |
| Inventory: nodes, tags, groups (Ansible variables), dependencies, validated capabilities | ✅ | AC-01; tests on capability/credential rules, cycles, filters, audit |
| Credential vault (envelope encryption, write-only secrets) | ✅ | Test: row-bound encryption, rotation, no secret in API/DB/audit; fingerprint equal to `ssh-keygen` |
| `Ctrl+K` command palette | ✅ | `Ctrl+K` (or `Cmd+K`, or the Search button in the sidebar) opens a palette over every console page: pages the user may see, nodes, "Open SSH on …" and "Browse files on …" (only with the permission and an enabled SSH capability). Ranking is pure and tested (`lib/palette.ts`, 6 tests); browser test 6/6. Actions too: "Run check … on …" (needs `monitoring.manage`, schedules the check now) and "Ask the AI about <node> · <check>" (needs `ai.use`, opens the Copilot with that problem selected; nothing is sent to the model until Diagnose is pressed). Found by a test: a hyphen now separates words, so `debian` finds `e2e-debian` |

### 5.3 F2 progress

| Item | Status | Verification |
|---|---|---|
| Static Go agent (CGO off), no listening ports (NFR-01) | ✅ | CI: `file` → statically linked; E2E: `ldd` → not a dynamic executable, no LISTEN socket of the agent |
| Bootstrap over SSH (FR-20): root or `sudo -n`, `uname -m`, systemd, binary copy, config 0600, unit with hardening | ✅ | 8 tests with in-process SSH (token never on a command line, errors `needs_root`, `unsupported_arch`, `systemd_required`, re-bootstrap); E2E on Debian 13 amd64 with systemd: online in 0.9 s |
| Outbound WSS tunnel, HTTP(S) proxy with CONNECT, custom CA (FR-21) | ✅ | Go tests with a TLS server and a CONNECT proxy; untrusted certificate → no data sent. E2E behind squid not done (NFR-02 `OPEN`) |
| Enrollment with a single-use token, ed25519 key, revocation, uninstall | ✅ | Tests: wrong signature, different nonce/host, expired or reused token, key cloned on another node, capability disabled (connection closed at once), revocation and re-enrollment |
| Telemetry every 5–15 s (FR-22): CPU, load, RAM/swap, disk I/O, file systems, network, uptime, processes, failed systemd units, listening ports, top processes | ✅ | Go tests on a fake procfs (rates, counter resets, process names with parentheses, IPv4/IPv6 sockets); values equal to `df` on the host |
| On-disk buffer (bbolt, 24 h) and deferred sending (FR-23, AC-05) | ✅ | Go tests: unconfirmed samples resent after reconnection; idempotent ingest (unique index `series_id, time`). E2E: server stopped for 3 min → 27 points, maximum gap 10.04 s |
| NOC dashboard (FR-24): host hexagon, node cards with CPU/RAM/disk and sparkline, "needs attention" | ✅ | API tests; screenshot. "Needs attention" uses fixed thresholds until F3 brings checks, problems and notifications |
| Node page: agent, actions, 1h/6h/24h/7d charts, ports, processes, systemd | ✅ | API tests of the charts; screenshot |
| AC-04 on Debian **and Rocky**, amd64 **and arm64** | ⚠️ | Verified on Debian 13 amd64; Rocky and arm64 not tried yet |
| 1m/1h continuous aggregates, 500-node load test (NFR-05) | ✅ | Migration `0025`: `metric_1m` (14 days) and `metric_1h` (400 days), not real-time (a real-time view was 1 s instead of 2 ms with 17,500 series); charts of 6 h or more read them plus the raw tail of their own series, 30 and 90 day ranges added; dashboard sparklines and `latest` rewritten. Load test on a throwaway TimescaleDB: **500 agents x 200 series = 10k points/s for 3 minutes, p99 131 ms, no errors**; it found the connection pool (5+10) starving at that rate: `JANUS_DB_POOL_SIZE`/`JANUS_DB_MAX_OVERFLOW`. A 3.3 GB working set on the test host (swap, rotational disk) gave multi-second tails: see `docs/SIZING.md`. The web socket layer and compression ratio were not measured |

Decisions (Security):
- **Agent as root with systemd hardening**: root is needed to read the sockets of all the processes (listening ports) and to query systemd; `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`, `RestrictAddressFamilies` and writing only to `/etc/janus-agent` (token removal) and `/var/lib/janus-agent`.
- **The token never appears on a command line**: it travels only in the configuration file (0600) uploaded over SFTP; the agent deletes it from the file after enrollment.
- **Local policy** (`allow_exec`, `allow_pty`, `allow_docker`, all `false`): written to the config by the bootstrap, reported to the server and displayed, never modifiable by the server (C9). In F2 the agent executes nothing on behalf of the server.
- **Agent CA**: the proxy publishes only the *public* certificate of Caddy's internal CA in a shared volume (`pki_public`); the CA key stays in Caddy's volume, not mounted in the app. With public ACME certificates the agents use the system CAs.
- **Robust ingest**: an invalid point is discarded, never the whole batch (it would block the agent's buffer); limit of 2000 series per node against label explosions; timestamps accepted between −48 h and +5 min.
- **One connection per node**: a new connection with the same key closes the previous one (`replaced`); a key already registered on another node (cloned VM) is refused.
- **Redis not introduced**: the dashboard polls the API every 10 s (NFR-04 ≤ 30 s met); Redis will arrive if needed with the F3 worker.

### 5.5 F4 progress

| Item | Status | Verification |
|---|---|---|
| Multiplexed streams on the agent WebSocket (binary frames, `streams` feature announced in the hello) | ✅ | Go tests (relay, refusal by policy, missing socket); Python mux tests; old agents receive no binary frames (`agent_outdated`) |
| Docker through the agent only with `allow_docker: true` in the local policy, chosen at installation over SSH | ✅ | Tests; E2E on the real `docker` node: refusal before, access after the reinstallation |
| Docker over SSH (`direct-streamlocal` to `/var/run/docker.sock`, reused connections) | ✅ | E2E on the real node |
| Containers: list, inspect (environment variable names only), logs and live logs, statistics, start/stop/restart/kill/pause/remove | ✅ | Tests with simulated Docker through the gateway; read-only E2E on a real Docker 29 |
| Compose stacks from labels: list of services, start/stop/restart of the whole project | ✅ | Tests; E2E (7 stacks) |
| Exec in the browser (xterm.js), start and end audit with command and duration | ✅ | E2E: `echo` run in a real container |
| Stack deploy from a Compose file | ⏳ | Requires the compose CLI on the node or a server-side parser: postponed |

Decisions (Security):
- **Docker = root on the node**: that is why it is opt-in in the agent's local policy (C9), written only by whoever installs over SSH; the server cannot enable it remotely.
- **No raw Docker API exposed**: the browser calls Janus-Ops endpoints; the server chooses the calls. Container and project names validated; environment variables shown without values.
- **Separate permissions**: `containers.manage` (actions) and `containers.exec` (shell, like `ssh.connect`); reading uses `inventory.read`. Every action and exec in the audit.
- **Flow**: at most 64 streams per agent; a stream with too much queued data is closed so as not to block the tunnel (per-stream flow control for `OPEN` is missing).

### 5.6 F5 progress

| Item | Status | Verification |
|---|---|---|
| Async PVE client (httpx2): ticket with renewal and CSRF, or API token; typed errors | ✅ | Unit tests with a simulated transport (login, CSRF, expired ticket, token, 401/403/5xx, unreachable host) |
| Cluster from the nodes with the `proxmox` capability (same cluster = same view), failover between members | ✅ | Tests with a fake PVE server over HTTPS: a member down does not hide the cluster; E2E on a real 2-node cluster (PVE 9.2.25) |
| Overview: quorum, nodes, storage (shared ones counted once), VMs/LXCs (FR-50) | ✅ | Tests; E2E on the real cluster (12 guests) |
| Lifecycle: start/shutdown/reboot/stop/suspend/resume, snapshots (create/delete/rollback), clone, migration, tasks with logs (FR-51) | ✅ partial | Tests with a fake PVE (reboot) and unit tests; on the real cluster **no** actions that modify the VMs were run |
| LXC console and node shell through termproxy (xterm.js); VMs through the serial port (FR-52) | ✅ partial | E2E: real LXC console with a login prompt; noVNC (graphical) not included |
| Backup audit: missing, failed, too old, without a job; threshold deduced from each job's schedule (FR-53) | ✅ | Unit tests on the rules; E2E: 2 guests without a backup detected out of 12 |
| Backups in PBS and replication | ⏳ | PBS contents are read like any `backup` storage (tests with PVE's PBS shape, fixture); **to be tried with a real PBS**: the server was not reachable from this environment |
| Alerts from the backups (`backup` check towards the F3 engine) | ✅ | Unit tests: no backup or failed backups → CRIT, old backups or guests outside every job → WARN, PVE error → UNKNOWN, `max_age_h` respected. Audit shared between API and engine (`proxmox/backup_audit.py`) |

Decisions (Security):
- **Credentials in the vault**, never in the browser; a user with 2FA is not supported with a password: use an API token.
- **The browser does not call PVE**: Janus-Ops endpoints with validated node names, snapshots and UPIDs; the cloud-init password is never returned.
- **Permissions**: reading with `inventory.read`; actions with `proxmox.manage`; console with `proxmox.console`, and the node shell also requires `ssh.connect`. Every action and every console (start/end, duration) in the audit.

### 5.7 Environments and node scope

| Item | Status | Verification |
|---|---|---|
| Configurable environments (fixed name, label, colour, order); not deletable while in use | ✅ | Integration test; used by nodes, notification-rule filters and the UI |
| Per-role scope: `all` or rules (environment, tag, group, capability, nodes) in AND within a rule and OR between rules; union across roles, a role without limits prevails | ✅ | Test with three restricted roles and one without limits: lists, 404 on single nodes, dashboard, checks, downtime, containers |
| Whoever can manage nodes cannot create or move them outside their own scope (403, cancelled) | ✅ | Test |
| Scope editor in the roles page (rules as buttons with a summary in words) | ✅ | Build and manual test |
| Audit log, notifications and channels | ⚠️ | They remain global (administrative permissions needed): the audit is not filtered by node |
| Group downtime | ⚠️ | Only for roles without limits, because they touch nodes outside the scope |
| Proxmox | ⚠️ | The scope applies to the PVE hosts of the inventory: a cluster is visible if one of its hosts is, and shows all its guests |

Decisions (Security):
- **404 and not 403** for nodes outside the scope: their existence is not revealed.
- **Filtering on the server**, not in the UI: every node query receives the scope condition; actions on a node check it first (WebSocket and SFTP too).
- **References by id** (tags, groups, nodes): if a tag is deleted the rule stays restrictive, it does not widen.
- A changed role takes effect at the next login (roles are fixed in the session).

### 5.4 F3 progress

| Item | Status | Verification |
|---|---|---|
| OK/WARN/CRIT/UNKNOWN states, thresholds, hysteresis over N cycles (FR-30, AC-06) | ✅ | State-machine unit tests; integration: CRIT 2 cycles → soft, no problem or notification; 3 cycles → hard, problem and webhook; recovery notified only once |
| Check types: agent, metric (also window average, worst series), systemd, listening port, TCP and HTTP(S) from the server | ✅ | Tests; E2E: TCP to a closed port → CRIT in 10 s |
| Ack with comment; a worsening of the state cancels the ack; downtime per node, check or group (FR-31, AC-08) | ✅ | Tests: no notification during downtime, notification at the end of the downtime if the problem is still open |
| Parent/child dependencies: children UNREACHABLE, no notification (FR-32, AC-07) | ✅ | Test with the parent node down; the dispatcher rechecks the parent at sending time |
| Flapping (FR-33) | ✅ | Last 21 results: start ≥ 50% of changes, end < 25%; notifications suspended |
| Notifications (FR-34): email with and without authentication, Telegram, webhook with HMAC signature, Discord, Slack, ntfy; rules by state, environment, nodes, check type, escalation | ✅ | Tests with local HTTP and SMTP servers; E2E with the user's real channels (mail, Telegram, Discord, ntfy): problem and recovery delivered |
| Check discovery (FR-35) | ✅ | Proposals from capabilities, file systems, listening ports and systemd; applied to the real nodes (62 checks) |
| Problems, downtime and notifications in the UI; checks in the node page; dashboard with unhandled problems and service count | ✅ | Screenshot |

Decisions:
- **Engine in the API process** (A2, like the SFTP reaper): no worker or Redis until needed; can be disabled with `JANUS_MONITORING_ENABLED=false`.
- **Dispatcher as a reconciler**: it compares problems with `notification_delivery`; a restart neither loses nor repeats notifications, escalations are rules with a delay, 3 attempts per sending.
- **Channel secrets in the vault** (ADR-004, AAD bound to the channel), write-only; never in logs, audit or responses. Standard libraries only for the senders (proxy from `HTTPS_PROXY`).
- **Checks run by the server** (TCP, HTTP): they start from the network of the Janus-Ops server; useful for nodes without an agent (e.g. NAS, appliances).

### 5.2 F1 progress

| Item | Status | Verification |
|---|---|---|
| 1-click web terminal with credentials from the vault (FR-10) | ✅ | 12 integration tests with an in-process SSH server; E2E through Caddy (WSS) to a real OpenSSH |
| Host key: pinning at first use before authentication, block on change, audited reset | ✅ | Test changed key → `host_key_mismatch`; fingerprint equal to `ssh-keygen` |
| Jump host (ProxyJump, one level) (FR-14) | ✅ | Test with a tunnel through the test server |
| Session recording (asciicast v2, output only) and replay in the UI (FR-13) | ✅ | Tests on header, output/resize events, 0600 permissions, own access vs `audit.read` |
| Audit of open/close/failures (AC-03) | ✅ | Tests on `ssh.session_started` / `ssh.session_ended` |
| Terminals closed on logout, disabling, revoked permission, inactivity; per-user limit | ✅ | Logout and limit tests |
| Multi-tab (FR-11, part) | ✅ | UI |
| SFTP browser (FR-11): navigate, download, upload (drag & drop, progress), rename, create folders, delete files and empty folders | ✅ | 7 integration tests with in-process SFTP: multi-chunk round trip, UTF-8 names, upload limit (with and without Content-Length), no truncated files, connection reuse, closing at logout, audit |
| Key broadcast to several terminals (FR-12) | ✅ | UI: selectable tabs, highlighted; always-visible warning |
| Library of personal and shared snippets, run by the server (FR-12) | ✅ | Tests: visibility, permissions, names unique per owner, execution over WebSocket, audit with the text sent |

Decisions (Security):
- **TOFU instead of static known_hosts**: no key inventory to manage by hand; the first connection is the weak point (documented). Mitigation: fingerprint visible in the *Host keys* tab and in the audit, to compare with `ssh-keygen -l` on the server. Preference ED25519 → ECDSA → RSA-SHA2; `ssh-rsa` (SHA-1) only as a last resort.
- **Recording of the output only**: typed passwords (e.g. `sudo`) are not recorded.
- **Origin mandatory on the WebSocket**: cookies also travel on cross-site handshakes, so without an Origin of the same host the connection is refused.
- **SFTP as a separate permission** (`sftp.transfer`): transferring files is potential exfiltration, distinct from opening a shell. The boundary on paths is the remote account, not Janus-Ops. Download, upload, rename, deletion and new folders are in the audit with the path; listings are not (noisy and without effects). SSH connections reused per (Janus-Ops session, node), closed after `JANUS_SFTP_IDLE_S` or at the end of the session; same host-key verification and jump host as the terminals.
- **Atomic upload**: written to a temporary name and renamed at the end of the transfer; never an overwrite without an explicit request; `JANUS_SFTP_MAX_UPLOAD_MB` limit also checked while streaming. No recursive deletion from the browser.
- **Snippets run by the server**: the browser sends only the id; the text comes from the database, so the audit (`ssh.snippet_run`) records exactly what was typed and where. Shared snippets require `snippets.manage` (not granted to operators by default): a shared snippet is code that others will run.
- **Browser-side broadcast**: the keys are forwarded by the browser to each selected terminal, like normal typing (recorded in the output of each session); no new server channel. Snippets launched in broadcast are marked `broadcast: true` in the audit.

Postponed to the phase that uses them, per the rule "no component without a requirement": **Redis** and
**worker/scheduler** (F2–F3), **agent-gateway** (F2).

### 5.8 F6 — Playbook catalogue sources (decisions)

A playbook is always a folder (or a file) with a stable relative path inside the source.
Each source has: name, type, base path, environment/scope (as for nodes) and write permission.

| Source | Read | Write | Security notes |
|---|---|---|---|
| **Git** | clone/pull (branch or tag) | no | URL and deploy key in the vault; checkout in a Janus-Ops folder, not in the working path |
| **Local upload** | file uploaded from the browser | yes (in the internal catalogue) | size limit; only `.yml`/`.yaml` and `.tar.gz`/`.zip` archives extracted without symlinks or absolute paths |
| **Built-in editor** | yes | yes (in the internal catalogue) | saving with a version; diff before saving; the run always uses the saved version |
| **Network mount** (NFS/SMB) | yes | optional, read-only by default | the mount is managed by the Janus-Ops host or by the agent; the path is validated against traversal and symlinks outside the root |
| **SFTP / SCP** | yes (listing and download) | optional | credentials from the vault; reuse of the SFTP client already present (F1); host key with TOFU pinning |

Common rules:
- Every source is written to the audit (creation, change, sync, deletion); the content of the executed
  playbook is frozen in the `job_run` (SHA-256 hash and copy), so that the history stays reproducible
  even if the source changes.
- The run never reads the remote source directly: the catalogue is synchronised first, with a
  verified hash.
- Sources with a node scope respect the same scope as §5.7: a playbook is not run on
  nodes outside the scope.
- Permissions: `ansible.read` (reading), `ansible.manage` (sources and writing),
  `ansible.run` (execution); the editor requires `ansible.manage`.

### 5.9 F6 progress

| Item | Status | Verification |
|---|---|---|
| Migration `0012`: sources, files, immutable versions (sha256 + content), frozen runs; permissions for operator | ✅ | Upgrade, downgrade to `0011` and upgrade on a real TimescaleDB 2.32 |
| Sources: `editor` and `upload` writable; `git` and `network` read-only, synchronised into versions | ✅ | API smoke test: creation, name conflict, mount outside the roots refused, saving with `expected_version` (409 if different) |
| Upload `.yml`, `.zip`, `.tar.gz`: symlinks, devices, absolute paths, `..`, binaries, sizes refused | ✅ | Unit tests on the archives and on the paths |
| Versions and diff; an identical save does not create a version | ✅ | SQL and API smoke test |
| Git: only `https://`, `ssh://` or `user@host:path`, no credentials in the URL, deploy key from the vault, validated ref | ⚠️ | Unit tests on the URLs; real clone not tried (no repository reachable from this environment). Git host key in `accept-new` for a temporary clone: no persistent pinning |
| Network mount: only under the roots of `JANUS_ANSIBLE_MOUNT_ROOTS` (default: disabled), symlinks ignored | ✅ | Unit tests with symlinks leaving the root |
| Run: nodes in scope (404 outside the scope), pinned host key, only SSH keys without a passphrase; key in a 0600 file in a temporary folder, removed at the end of the run | ✅ | End-to-end run with a fake `ansible-playbook`: inventory, `known_hosts`, 600 permissions, secret absent from log and history |
| Run log updated in the background, history and detail per scope | ✅ partial | Polling through the API; no WebSocket streaming |
| Real execution with Ansible | ✅ partial | `ansible-core` 2.21.5 in the venv and in the image (`git` and `openssh-client` installed); `ansible-playbook` verified in the container as user 10001. Run on a real host still to be tried |
| SSH password (`sshpass`) | ✅ | Unit tests: inventory with `ansible_password` and without BatchMode |
| Jump host: one hop, key only, `ProxyCommand` with the jump host's key | ✅ | Unit tests: refusals (not pinned, with a password, chained), inventory and known_hosts with the jump host. Not tried on two real hosts |
| Survey: `<name>.survey.json` next to the playbook (text, integer, boolean, choice); values frozen in `job_run.survey_values`, passed with `-e @file` 0600 | ✅ | Migration `0013`; unit tests on parsing, defaults and types; integration tests: API refusals and values saved in the run |
| SFTP source (read-only, `sftp://` with a credential from the vault, TOFU host key saved in the source) | ⚠️ | Unit tests on the URLs; integration tests on the refusals (credential, URL, type). SCP not implemented. Real sync against an SFTP server not tried |
| UI: sources, upload, editor with versions and diff, run start with node selection, history with log | ✅ | `tsc`, ESLint, static build; browser test against the live stack: new editor source, new file, edit and save a version, dry run on a real node (`succeeded`, exit 0), clean up |
| Dedicated integration tests in the suite | ✅ | `tests/test_ansible_integration.py`, `tests/test_ansible_*_unit.py`; full suite with TimescaleDB 2.32 and lldap through `scripts/integration-test.sh` |

---

### 5.10 F7 progress

| Item | Status | Verification |
|---|---|---|
| Migration `0014`: `ai_diagnosis` (model prompt and response, action plan in JSON); `ai.use`, `ai.approve` permissions for operator | ✅ | Upgrade and downgrade in the integration suite on TimescaleDB 2.32 |
| OpenAI-compatible client (`/chat/completions`), https only, bearer key from `JANUS_AI_API_KEY` | ✅ | Unit tests with a fake transport (request, headers, stable error codes). **Tried against the real NVIDIA endpoint** (`nvidia/nemotron-3-ultra-550b-a55b`, 2026-11-18): a diagnosis of a real open problem came back with a summary and four read-only action proposals |
| Read-only tools: inventory (without secrets), last-hour metrics, check history as a log, node audit | ✅ | Unit tests on redaction; no access to the vault. Container and journal logs not collected by the agent: not available |
| Asynchronous diagnosis on an open problem, in scope; pending, done, error states | ✅ | Integration tests: Copilot off (503), permissions, decisions on the action plans |
| Operational data encapsulated as data, not instructions; output never executed, only parsed (C10) | ✅ partial | Unit tests: a log cannot close the data block; effectiveness against the real model is not measured |
| Action plan: approve, reject, edit while `proposed`; decision only once; audit | ✅ | Integration and unit tests |
| Audit of every request and response (SHA-256 hash of the prompt and of the response) | ✅ | Integration test: the `ai.diagnosis.finished` and `ai.action.decided` events |
| Execution of approved actions | ✅ partial | Implemented in F8 (§5.11): exec through the agent with local `allow_exec` policy. Unit and integration tests; run on a real node not tried yet |
| Contextual IaC generator (FR-72, SHOULD) | ✅ partial | Migration `0030` (`ai_iac`). Ansible, Compose or shell code for one node, from its inventory (no secrets), metrics and checks; the output is text, never run. Syntax check (YAML) and warnings on risky patterns. Unit tests; integration tests written but not run here (no test database); not tried against the real model |
| Second opinion with comparison (LLM-as-judge) | ✅ partial | `JANUS_AI_REVIEW_MODEL` (own endpoint/key optional). A second model answers the same prompt, then the main model compares the two (agreement, differences, preferred, verdict). The first diagnosis is untouched. Unit tests on parsing and fencing; not tried against real models. The judge shares biases with the models it judges: a hint, not a measurement |
| AI endpoints, models and keys in Settings (`ai.manage`, admin) | ✅ partial | Migration `0031` (`ai_config`, slots `main` and `review`); a NULL field inherits `JANUS_AI_*`. The key is sealed by the vault (same path as other secrets, included in KEK rotation via `secrets_migrate`) and never returned by the API. List the models the endpoint offers, test with one tiny request, back to the environment. Unit tests on the overlay; integration test written, not run here; not tried in the browser |
| Copilot interface (open problems, diagnosis, decisions) | ✅ | `tsc`, ESLint; browser test against the live stack: select a problem, Diagnose, wait for the model, read the summary, the proposals and "what was sent" (the proposals were not executed) |
| Quality evaluation of the diagnoses with local models | ⏳ | Not measured: a set of cases with an expected outcome is needed |

### 5.11 F8 progress

Scope of F8 (from the STATE below): exec through the agent, workflows, GitOps, SNMP. PTY through the agent (FR-83), signed auto-update
(FR-84), external AD/LDAP (FR-07), OIDC, NTLM and external secret manager (FR-86) stay in F8+ (the first two and FR-07/FR-86 are now done, see the rows below and §2.1).

| Item | Status | Verification |
|---|---|---|
| Exec through the agent: server-side runner, agent-side execution without a shell, streams with limits, `allow_exec` policy in the bootstrap | ✅ partial | `tests/test_exec_unit.py`, `agent/internal/tunnel/exec_test.go`. Real node not tried |
| Execution of the Copilot's approved actions (`actions.execute`): command split without a shell, shell syntax refused, return to `approved` if the run does not start, audit of every attempt | ✅ partial | Unit and integration tests in `test_ai_integration.py`. Real node not tried |
| Workflows (FR-80) | ✅ partial | Migration `0021` (`workflow`, `workflow_run`, `workflow_step_run`, `workflow_approval`). JSON definitions (trigger and steps), validated with closed-parameter models. `exec` (through the agent, without a shell), `notify` (audit) and `approval` steps. A destructive exec, i.e. one not declared `read_only`, and every `approval` step wait for a person; a completed step is not repeated after a wait. Manual, interval (minimum 60 s) and alert triggers (an open problem starts the workflows of the check, within 30 s). Scope on exec nodes at saving, at start and at approval. Runs interrupted by a restart become `failed`. Permissions `workflow.manage`, `workflow.run`, `workflow.approve`. The `notify` step sends the message to a notification channel (mandatory, encrypted in the vault like the other channels) and records it in the audit; a missing or disabled channel fails the step with the reason. **Since v0.27 (migration `0024`):** **webhook** trigger (`POST /workflows/{id}/webhook`, JSON body signed with HMAC-SHA256 in `X-Janus-Signature`, secret sealed in the vault and shown once, rotation, at most one start every 5 s, the body goes into the run context; destructive steps still ask for approval) and **conditional branches** (`when` on a previous step: ok/failed/skipped outcome and optional text in the output; `on_failure: continue` to tolerate an error; a step not run = `skipped`). Integration tests on a real DB (`test_workflows_integration.py`). **Visual editor (v0.30) and graph editor (v0.33):** the workflow dialog has three tabs over the same definition. **Graph** (React Flow, `@xyflow/react`, MIT, bundled: the UI stays air-gapped) draws the trigger and one node per step, the run order as arrows and each `when` as a dashed amber branch labelled with its outcome; you add steps from a palette, drag them (the run goes top to bottom, so dragging past another step reorders), drag the amber dot of one step onto another to make a branch (a step has at most one; a branch always looks back, so connecting to an earlier step moves it below), change or remove a branch on the edge, edit the selected step in a side panel, and "Tidy up" lays the column out again; steps with a problem are outlined. **Form** is the previous list of cards and **JSON** the raw definition. Node positions are stored in each step's optional `ui` (layout only, never read by the engine). The run view draws the same graph read-only, each node coloured by how that run went. The rules (graph model, gestures, validation, normalisation) are pure functions in `frontend/lib/workflowGraph.ts` and `frontend/lib/workflows.ts`, covered by 27 tests (`npm test`, in CI), and the editor was driven in a headless browser (build, drag, connect, reorder, remove, save, run). **Not yet:** automatic resume after a restart, live (while running) refresh of the drawn status beyond the 3-second poll, undo/redo in the editor, trigger and notify nodes in the graph tests (a notify step needs a real channel) |
| GitOps compose with webhook (FR-81) | ✅ partial (with interface) | Migration `0015` (`gitops_stack`, `gitops_run`; permissions `gitops.manage` admin only, `gitops.deploy` operator). Repository `https://` only, without credentials in the URL; checkout in `/var/lib/janus-ops/gitops/<id>`: `git fetch` and `reset` at every deploy, then `docker compose -p <name> -f <file> up -d --remove-orphans`, all through the agent with `allow_exec`. Webhook signed with HMAC-SHA256 (`X-Hub-Signature-256`, GitHub/Gitea/Forgejo), push on the branch only, secret in the vault shown once. One deploy per stack at a time (partial unique index). Unit and integration tests (`test_gitops_*`). Web interface (GitOps page: stack list, creation, deploy, history with output, webhook secret shown once). **Not yet:** private repositories (deploy key), deploy over SSH without an agent, `compose down` when a stack is removed, test on a real node |
| SNMP v2c/v3 (FR-82) | ✅ partial | `snmp` check (`monitoring/snmp_poll.py`, `checks.py`), `snmp` capability with configurable v3 security, `snmp_v3` credentials (user, authentication and privacy passphrase, minimum 8 characters). Catalogue: `snmp_oids_integration` (seed, permissions, duplicates, verification, audit). Tests: `test_snmp_unit.py` (parameters, classification, thresholds, authentication), `test_snmp_live.py` (local SNMP agent on loopback: v2c, v3 authPriv SHA-256/AES-128, wrong passphrase, wrong community, non-existent OID, timeout), `test_snmp_integration.py` (API, vault and a real engine tick). Interface updated, not tried by hand in the browser. **Not yet:** SNMP on a real host (NAS/UPS), MIBs with names, traps, v1, creation of templates from the UI (API only), `template` source for checks (the table accepts only `manual` and `discovery`: the provenance is in the audit) |
| PTY through the agent (FR-83) | ✅ partial | `pty` stream (`proto/agent-protocol.md`): login shell on a pseudo-terminal, only with `allow_pty` in the local policy (chosen at installation, "Allow shell" checkbox), `RESIZE` frame, at most 4 sessions and 8 hours. The web bastion accepts `transport=agent` (same `ssh.connect` permission, no SSH credential, recording and audit like SSH sessions, `transport` in the payload). Tests: `agent/internal/tunnel/pty_test.go` (refusal by policy, size, input/resize/exit on a real shell), `tests/test_pty_unit.py`, `tests/test_terminal_agent_integration.py` (`transport=agent` WebSocket on a real DB with a simulated agent: full flow, policy, offline agent, permission; the SSH terminal tests stay green after the refactor). **Test on real nodes (2026-11-17, e2e-debian, centos, docker4):** root shell through the browser working (size and `systemctl` ok). With `allow_pty` the systemd unit is no longer sandboxed: the shell inherited `ProtectSystem=strict`, `NoNewPrivileges` and a private `/tmp`, i.e. a read-only file system. **Not yet:** choice of the shell's user (today the agent's), terminal for agents without the SSH capability in the file browser |
| Signed agent auto-update (FR-84) | ✅ partial | ed25519 release key **outside the server** (`janus-ops agent-keygen`, `agent-sign` → `janus-agent-linux-<arch>.sig`; procedure in `docs/AGENT_RELEASE.md`: the private key is kept off the Janus-Ops host and signing runs from the same image on the release machine); the server knows only the public key (`JANUS_AGENT_RELEASE_PUBLIC_KEY`, written as `update_public_key` in the agent config at bootstrap) and distributes. `update` stream: signature over version, architecture and SHA-256 verified by the agent before receiving the bytes, only newer versions (no downgrade), checksum, atomic replacement and restart through systemd; **automatic rollback**: the old binary stays as `.old` until the new one connects, and goes back in place after three failed starts or 3 minutes without a connection (`rollback_test.go`). Mass update: `POST /agents/update-all` (three agents at a time, skips the offline ones or those already on the signed version, per-node result, audit `agent.update_all`), "Update agents" button in the inventory. Endpoint `POST /agents/{id}/update` (`agents.manage`, audit `agent.update`/`agent.update_failed`), "Update (signed)" button. Tests: `agent/internal/tunnel/update_test.go` (including a signature vector produced by the Python side), `tests/test_agent_update_unit.py`. **Test on real nodes (2026-11-17, e2e-debian, centos, docker4):** single and mass update succeeded (0.3.0→0.3.3), downgrade refused by the agent (`not_newer`), signed but corrupted binary (exec format error): after four starts the **safety net in the systemd unit** restores the old binary (the rollback inside the agent cannot fire if the binary does not start). The sandboxed unit prevented the agent from replacing its own binary: `ReadWritePaths` now includes `/usr/local/bin`. The directory of signed binaries can be mounted with `deploy/compose.release.yaml` for hand-signed tests. **Shipping with the image (v0.31):** the agent build is reproducible (`-trimpath -buildvcs=false`; the binaries built by `make agent/build` and by the `Dockerfile` have identical SHA-256 on all three architectures), so signatures made on the release machine (`make agent/sign`, key in the git-ignored `secrets/release/`) are committed in `agent/release/` and the `Dockerfile` copies them next to the binaries, refusing any signature for other bytes or another version; with `REQUIRE_SIGNED=true` (CI on release tags) it also refuses to build without one. `scripts/release.sh` verifies the signatures against the public key (`janus-ops agent-verify`) before it tags. The private key never enters CI or the image. All 12 agents of the live stack were reinstalled with the key and updated to the 0.4.x line. **Flow control (v0.32):** found on a node with a slow Wi-Fi link (~60 KB/s, `pi`): an 8 MB update outlasted the agent's 15 s pong timeout and the agent dropped the connection. The agent now announces `update_ack` and confirms what it has written every 64 KiB; the server keeps at most 256 KiB unconfirmed and stalls out (`update_stalled`) after 60 s without a confirmation; agents without the feature behave as before. An agent that predates it still needs one reinstall over SSH to get it. **Not yet:** signing inside CI (deliberately not done: the key stays on the release machine) |

## 6. Security — Initial threat model (STRIDE)

Trust boundaries: Browser ⇄ Server · Agent ⇄ Server · Server ⇄ target (SSH/PVE/SNMP) · Server ⇄ LLM · Server ⇄ Git.

| Threat | Vector | Mitigation | Residual risk |
|---|---|---|---|
| S — Agent impersonated | Theft of the enrollment token or of the key | Single-use token with a short TTL (15 min) only as a hash, never on a command line; after enrollment an ed25519 key on the node (0600) and signature of a nonce per connection; key revocation; a key duplicated on another node is refused | Low |
| S — User session stolen | XSS, cookie theft | httpOnly/Secure/SameSite cookies, strict CSP, TOTP | Low |
| T — Agent binary tampered | Server or channel compromised during an update | ed25519 signature with an offline key, verification on the agent | Medium (if the release key is compromised) |
| T — Audit tampering | DB admin | Append-only through grants, hash chain, external export | Medium |
| R — Unattributable actions | Shared accounts, AI actions | Audit for every action with the actor; AI actions attributed to the approver | Low |
| I — Secret leak | API, logs, LLM prompts | Secrets never serialised to the client; structured logs with redaction; AI tools without access to the vault | Low |
| I — Operational data to LLM hosts | Logs/metrics sent to hosts outside the stack | Self-hosted models only (A6), TLS to the endpoints, endpoints configurable only by Admin and audited | Low (data accepted by A6) |
| S — LDAP login bypassed | LDAP injection in the filters, anonymous bind or with an empty password | Filter escaping, refusal of empty passwords, anonymous bind disabled | Low |
| I — LDAP credentials exposed | Clear-text traffic, service-account password | LDAP reachable only on the internal compose network (LDAPS if the traffic leaves the host), read-only service account from a secret | Low |
| D — LDAP unavailable | Container stopped or corrupted | Break-glass account, healthcheck, backup of the LDAP volume | Low |
| E — Escalation through user management | Whoever has `users.manage` makes themselves admin | Only admin assigns/removes admin or edits an admin; dedicated tests | Low |
| E — `janus-manager` account compromised | It is the directory administrator | Password only in `.env` (600) and in the app container; never exposed; internal `backend` network | Medium (intrinsic) |
| I — Loss of `JANUS_SECRET_KEY` | DB backup without the key | TOTP seeds unreadable → 2FA reset by an admin; key to be saved together with the DB (README) | Low |
| D — Flood from agents | Compromised or buggy agent | Per-agent rate limit, maximum batch size, backpressure | Low |
| E — Mass RCE from the server | Server compromised → exec on the whole fleet | Local policy on the agent that cannot be overridden remotely, RBAC, approval for broadcast | **High: intrinsic to the product** — document it |
| E — Human error in broadcast | Destructive command sent to several nodes | Explicit per-terminal broadcast, highlighted borders, warning with the number of terminals; shared snippets only with `snippets.manage`; audit with the text | Medium |
| I — File exfiltration through SFTP | User with `sftp.transfer` downloads sensitive data | Dedicated permission, audit of every download with path and size, permissions of the remote account | Medium (intrinsic to the function) |
| E — Prompt injection through logs | Logs containing instructions for the AI | Read-only tools, no execution without a human, action plan shown in full | Medium |
| I — SNMP v2c community intercepted | Clear-text SNMP traffic on the network | v3 (`authPriv`) as default and recommended; community in the vault, never returned; no SET | Medium (intrinsic to v2c, operator's choice) |
| S — SNMP device spoofed | Forged response over UDP | v3 authenticated with SHA-256; the check works only on configured numeric OIDs | Low (v3), Medium (v2c) |
| I — v3 passphrase in logs or errors | pysnmp exceptions with parameters | Errors reduced to stable codes; passphrases never enter messages, logs or responses | Low |
| E — RBAC escalation | Endpoint without a check | Deny-by-default, mandatory FastAPI dependency per role, test per endpoint | Low |
| T — Forged agent update | Compromised server pushes a malicious binary | The agent verifies an offline ed25519 signature over version, architecture and hash against a key in its own config; no downgrade; automatic rollback | Medium (if the release key is compromised) |
| I — Master key next to the data | `JANUS_SECRET_KEY` in `.env` on the same host | External secret manager (FR-86): the key lives in OpenBao/Vault Transit; with the static auto-unseal the unseal key is still on the host (documented in `docs/OPENBAO.md`) | Medium (intrinsic to automatic unsealing) |

Security requirements for the Engineer: pinned dependency versions (lockfile), SBOM in CI, non-root containers,
no secrets in images/logs, input validation with Pydantic on every boundary.

---

## 7. Project risks

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Scope creep (8 modules) | High | High | Releasable phases, explicit WON'Ts |
| Complexity of the agent tunnel (multiplexing, proxy) | Medium | High | Technical spike at the start of F2 with a real proxy |
| ldap3 no longer released (last release 2021) | Medium | Medium | Pinned versions, tests on a real lldap in CI, replaceable behind `DirectoryBackend` (ADR-008) |
| External secret manager unavailable (ADR-011) | Low | High | `local` backend by default; health check in `/ready`; clear messages; document OpenBao HA and unseal (`docs/OPENBAO.md`) |
| Timescale ingest performance | Low (with A2) | Medium | Batch insert, load test in F2 |
| Proxmox API compatibility across versions | Medium | Medium | Tests against PVE 8.x `[VERIFY: target versions]` |
| Quality of AI diagnosis with local LLMs | Medium | Medium | Deterministic prompts and tools, evaluation metrics |

---

## 8. Graphic resources (`res/`)

`res/` is the **single design-assets folder** (v0.3). The mockup was taken from the `janusops-design-kit/`;
everything else in the kit was already identical to `res/` or missing. The complete inventory is in
[res/README.md](../res/README.md).

### 8.1 Content

| Path | Content | Origin |
|---|---|---|
| `Janus-Ops-logo.png` | 2048×2048 master logo, transparent background | provided |
| `logos/janus-ops-logo-{256,512}.png` | Logo cropped to the content, transparent | generated from the master |
| `logos/janus-ops-app-icon-512.png` | Rounded icon on `#090A0F` with a `#10B981` border | generated |
| `favicons/` | `favicon.ico` (16–64), PNG 16/32, `apple-touch-icon.png` 180, `android-chrome` 192/512, `site.webmanifest` | generated |
| `design-tokens.json`, `globals.css` | Tailwind v4 tokens and base CSS; added `text.nav` / `--text-nav` = `#94A3B8` | provided + v0.3 |
| `obsidian-graphite-palette-sheet.png` | Visual palette | provided |
| `HexagonHealthWidget.tsx`, `NodeEnvironmentCard.tsx` | Reference components | provided |
| `mockups/janus-ops-noc-preview.html` | Preview of the app shell, brand corrected to "Janus-Ops" | from the design kit |

Known limits:
- The logo is very detailed: at 16×16 it is hardly legible. A simplified mark for small sizes is a possible improvement, not blocking.
- The `janusops-dashboard-final.jpg` render cited by the kit does not exist. In the mockup it is replaced by a placeholder and the reference for the NOC layout remains §9 of [specs.md](specs.md) plus §8.2 below.
- `janusops-design-kit/` is a duplicate of `res/` and is not included in the repository.

### 8.2 Elements taken from the NOC mockup

- **App shell**: fixed sidebar on the left (`w-56`, `--bg-surface`, right border `--border-default`) + main area with 24 px padding.
- **Brand in the sidebar**: 40 px logo (`logos/janus-ops-logo-256.png`) + wordmark "Janus-" in `--text-primary` and "Ops" in `--accent-primary`, 10 px mono subtitle "NOC CONTROL PLANE" in `--text-muted`.
- **Navigation order**: Dashboard · Containers · Proxmox VE · Ansible & Workflows · SSH Bastion · Janus AI Copilot. It matches the modules; Inventory, Alerts/Problems, Audit, Settings are missing, to be added.
- **Active item**: background `--bg-elevated`, text `--accent-primary`, border `--border-default`. **Inactive item**: `--text-nav` (`#94A3B8`, added to the tokens in v0.3). The AI item always uses `--accent-ai`.
- **Sidebar footer**: design-system version in mono ("Obsidian Graphite v1.0").
- The mockup loads Tailwind from a CDN: fine for a preview, not for the app, which must work air-gapped (all assets and fonts local).

### 8.3 Notes for the design system (F0)

- The JSON tokens also contain `semantic.*.subtleBg` and `brand.primaryHover`, absent from the CSS: unify by generating the CSS from the tokens.
- The example components use inline hex colours (`bg-[#12141D]`): in F0 they must be converted into classes based on the Tailwind v4 variables/theme.
- `HexagonHealthWidget` always fills the inner hexagon in red (`rgba(239,68,68,0.18)`) even if the state is OK: the fill must be derived from the state like the stroke.
- Fonts: Inter (UI) + JetBrains Mono (metrics, tabular-nums), to be distributed self-hosted (air-gapped environments).
- Favicon and PWA icons in `res/favicons/` must be copied to `frontend/public/` (or `app/`) in F0 and linked in the Next.js metadata `[VERIFY: file-based icon conventions of the chosen version]`.

---

## 9. Questions

### 9.1 Resolved

| # | Question | Answer | Effect |
|---|---|---|---|
| 1 | Target scale | ≤ 500 nodes, ≤ 20 users, "for the time being" | A2 confirmed |
| 2 | Authentication | LDAP as a Docker sidecar, directory in the stack | A5 revised, ADR-008, FR-03/06/07 |
| 3 | Windows agent in the MVP | No | A4 confirmed, Windows in F8+ |
| 4 | Data towards the AI | All operational data; local models on external hosts | A6 revised, ADR-007, threat model |
| 5 | TimescaleDB licence | Apache 2 (core) + TSL (Community) | C12 verified |
| 6 | Janus-Ops licence | Apache-2.0 | A8, `LICENSE`, `NOTICE` |
| 7 | Brand spelling | Janus-Ops | C15 |
| 8 | Logo assets | Master `Janus-Ops-logo.png`; favicon to be generated | `res/logos/`, `res/favicons/` |
| 9 | Assets folder | `res/`, taking from the design kit what is needed | §8 |

### 9.2 Open

No blocking question. The `[VERIFY]` points remain to be closed during the individual phases.

---

```
STATE
  Tier:        L
  Phase:       Engineer — F0–F7 completed (F7 and F6 with partial checks, §5.10 and §5.9); F8 completed in code with partial checks (§5.11); F2 with AC-04 verified only on Debian amd64
  Delivered:   plan v0.30, migrations 0001–0024, release 0.4.0; F0–F5 (F3 alerts, F4 containers, F5 Proxmox); F0–F2: web SSH bastion, SFTP, snippets;
               Go agent (WSS tunnel, telemetry, buffer), enrollment with an ed25519 key,
               SSH bootstrap, idempotent Timescale ingest, NOC dashboard and node page;
               F6 Ansible, F7 AI Copilot; F8: exec/PTY through the agent, signed auto-update with rollback and mass rollout,
               workflows (webhook trigger, conditional branches), GitOps, SNMP, external AD/LDAP (FR-07),
               external secret manager (FR-86: OpenBao/Vault Transit and KV, OpenBao in the stack with auto-unseal)
  Open:        Ansible run on a real host (jump host included); SFTP sync on a real server; real Git clone (F6); real PBS test (F5); CSP for the exported UI; retention of the recordings;
               external AD/LDAP on a real AD; "by reference" credentials (KV, read-only); visual workflow editor and automatic resume after a restart;
               SNMP templates from the UI, traps, MIBs; OIDC; NTLM proxy; Windows agent;
               AC-04 on Rocky; E2E behind squid (NFR-02); load test (NFR-05);
               continuous aggregates; manual agent installation without SSH; manual browser tests of the F8 interfaces;
               [VERIFY] Proxmoxer async, aiodocker ssh, NTLM Go, Taskiq
  Next:        manual browser tests of the F8 interfaces (visual workflow editor, resume after a restart and GitOps web UI are done)
```
