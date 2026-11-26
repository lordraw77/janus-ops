# Changelog

## 0.13.0 — 2026-11-26

**Added**
- **Short-range telemetry charts.** The node page has 5 and 15 minute ranges (10 s buckets, one point per agent sample,
  seconds on the time axis) that refresh every 10 s instead of 30.
- **The Copilot knows where a VM runs.** When the node of a problem is a Proxmox VM or container (matched by node name
  or agent hostname against the guest name, first label only), the diagnosis also gets a `hypervisor` block: cluster,
  guest status, lock and HA state, guest load, and the host's status, CPU, memory and number of running guests. Read-only,
  cached 30 s, and silent when nothing matches or the API is unreachable. Several guests with the same name are listed
  as candidates.
- **SNMP templates from the UI.** In the OID catalog, tick entries and "New template from N selected" creates a check
  template (one SNMP check per OID, table columns as per-instance checks). In the template editor an SNMP check has a
  catalog picker that fills the OID, the table flag and the name, so the parameters JSON is only needed for scale and
  timeout.
- **SNMP traps (v2c) and MIB files.** An optional receiver (`JANUS_SNMP_TRAP_PORT`,
  `JANUS_SNMP_TRAP_COMMUNITIES`; ready-made `deploy/compose.snmp-traps.yaml` publishes it as 162/udp) stores the traps
  of inventory nodes, matched by source address (no SNMP capability or credential needed). Anything else is dropped and counted,
  each source is limited to 120 traps a minute, traps are kept 30 days. New **SNMP traps** page: the traps with
  names, their values, the receiver status, and **MIB files** (upload `.mib`/`.txt`: only names and OIDs are kept, no
  compiler and nothing executed; standard names such as linkDown and ifOperStatus are built in). New probe
  `snmp_traps.recent` (checks of kind probe, any inventory node): the number of traps in the last N
  minutes, optionally only under an OID, graded with the usual thresholds, so a trap becomes a problem that closes by
  itself. Each trap in the list has an **Alert** button that creates that check already filled in (this trap type or
  any trap, how long the problem lasts, warning or critical). The Copilot gets the node's traps of the last 24 hours
  with a diagnosis. SNMPv3 traps are not accepted yet (migration `0043`).
- **Export and import of the configuration** (Settings → Backup & restore; permissions `config.export` and
  `config.import`, admin by default). One JSON file with the sections you tick: environments, credentials, hosts (tags,
  capabilities, services, parent), groups, your check templates, checks, the SNMP OID catalog, notification channels and
  rules, shared snippets, workflows, AI providers with models and routes. Ids are written as names (`@node:web01`), so
  the file moves between installations. **Secrets** (credential values, notification secrets, AI keys) are included
  only on request (needs `credentials.reveal`) and travel in one block encrypted with a passphrase of at least 12
  characters (scrypt + AES-256-GCM); the passphrase is never stored. Import: choose the file, **Preview** (the whole
  import runs and is rolled back, nothing reaches the vault), then apply; existing items are left alone or overwritten,
  each item is imported on its own so one failure does not stop the rest. Credentials in the file also need
  `credentials.manage`. Not included: users and roles, the audit log, metrics, agents, GitOps stacks, Ansible sources,
  downtimes, discovery. Checked on the real data: export, wipe in a transaction, import, export again gave identical
  sections for all eleven, and the rollback left the database as it was.
- **Show a credential's secret, with its own permission.** New permission `credentials.reveal` (the admin role has it by
  default; give it to other roles in Users & roles). On Credentials a **Show** button opens the secret in clear
  (password, token, key with its passphrase) for 30 seconds, with a Copy button. Every view is written to the audit
  log (`credentials.revealed`, who and which credential, never the value) before the secret is returned, and the answer
  is never cached. Without the permission there is no button and the endpoint answers 403.

**Changed**
- **AI providers: add and choose in one click.** "Add provider" is now a gallery of the supported providers, each with its
  address already filled in (a second provider of the same kind gets the next free name, for two accounts). Every
  provider has a **Use this** button next to a list of its tested models: that model becomes the first choice of the
  default route and what answered before stays behind it as fallback; the provider in use is marked "in use" and the
  others "fallback".
- **Settings → AI models & tools: pick the main and the second-opinion model from a list.** Two combos offer the tested
  models of your enabled providers; "Use as main model" / "Use as second-opinion model" make the choice the first entry of
  the `default` / `review` route and keep the previous entries as fallback. The old URL/model/key form is now "Single
  endpoint (without providers)", used only while the routes have no model.
- **Spinners where something loads.** Changing the range of a telemetry chart dims the old charts and shows a spinner
  with the range being loaded (a 30 or 90 day chart can take seconds); every "Loading…" placeholder has a spinner;
  the Refresh button of the Docker lists spins while it reloads; Containers → All hosts shows a spinner until the hosts
  have answered.
- **UI polish.** Muted text raised to AA contrast, one visible keyboard focus ring everywhere, fixed full-height
  sidebar with an accent on the active page, modals that take focus (Escape works), lock the page scroll and restore
  focus, red/amber borders on down/unreachable node cards, loading skeleton and "all clear" state on the dashboard.

## 0.12.2 — 2026-11-25

**Added**
- **Describe it, the model drafts it** (needs `ai.use`). *Stacks*: "New stack" and the Compose tab have a box where you
  say what you want; the model writes the Compose file, or changes the one in the editor, and you review it (with a diff
  and Undo) before saving. Literal passwords and tokens are replaced by placeholders before the file leaves the server
  and put back in the answer; the model also sees the host ports already in use. *Checks*: "New check" has a
  "Describe what to watch" box; the model drafts checks from the node's inventory, running units, ports and metric names,
  each draft is validated like a hand-made check, and you load one into the form or create them all. The model is chosen
  among the configured routes and models. Both are audited (model, hash of the request); nothing is saved or run for you.
- **Check kinds `process` and `logs`**. `process`: at least N processes with a name (or text in the command line) run on the
  node, from `pgrep`. `logs`: the journal of a systemd unit (last minutes) or the tail of a file under `/var/log` has no
  line with the error words (default error, fatal, exception, panic); the last matching line is shown. Both run a fixed
  read-only command through the agent (needs "Allow exec", Linux).
- **Backup check: guests not backed up on purpose.** The check has a list of VM/CT ids it leaves out
  (`ignore_vmids`). On an open backup problem the new "ignore guests" button lists what Proxmox complains about so you
  tick the ones you do not back up; the check runs again at once and the problem closes by itself. Audited.

## 0.12.1 — 2026-11-25

**Added**
- **Stacks: view and edit the Compose file, and create new stacks.** The stack dialog has a Compose tab that reads the
  file from the host, validates your change (`docker compose config`), saves it (the old one is kept as `.bak`) and,
  on request, applies it (`docker compose up -d`). "New stack" pastes a Compose file, saves it under
  `/var/lib/janus-ops/stacks/<name>` on the host and starts it. Works on agent and SSH hosts alike: a throwaway
  `docker:28-cli` container (pulled on first use) mounts the stack directory; nothing is installed on the host.

**Fixed**
- Volumes and Disk looked empty while loading: `/system/df` takes seconds on a host with many volumes. Volumes now
  load at once (sizes on request with "Show sizes"), and every section shows a loading message.

## 0.12.0 — 2026-11-24

**Added**
- **Docker area: Stacks, Images, Volumes, Networks and Disk** next to Containers, for each Docker host. Stacks are the
  Compose projects found from container labels: services, containers, networks, volumes and images, with start,
  restart, stop, pull + recreate and the new `down` (containers and networks; "down + volumes" also deletes the data).
  Images: pull, remove, prune dangling or unused. Volumes: size, stack and use, remove, prune unused. Networks: subnet,
  gateway, attached containers, remove, prune. Disk: space used and reclaimable, remove stopped containers, clear
  build cache. Changes need `containers.manage` and are audited. The Compose file itself is not read or edited.

## 0.11.2 — 2026-11-24

**Added**
- **Problems history shows how each one ended**: "Recovered" when the check came back OK by itself, or
  "Closed by <user>: <comment>" when someone closed it from the console (single or bulk). Migration 0042 adds
  `problem.closed_by` and `close_comment`; problems closed by hand before this release show as recovered.

## 0.11.1 — 2026-11-24

**Added**
- **Close a problem by hand** (Problems → Open, the new button on each row; `POST /problems/{id}/close`, needs
  `monitoring.manage`). The problem moves to the history and the check runs again at once: if it still fails, a new
  problem opens and notifies again, so to silence a real problem use Acknowledge or a downtime. An optional comment
  is kept in the audit log (`monitoring.problem_closed`).
- **Select several problems** (Problems → Open): a checkbox per row and "select all" on what the filters show, then
  **Acknowledge** or **Close** them in one step (`POST /problems/bulk`, up to 200). Each problem is audited as if it were
  done alone (with `bulk: true`); problems already closed or outside your scope are skipped and counted.

## 0.11.0 — 2026-11-24

**Added**
- **Windows agent: interactive shell** in the web terminal (Terminal → the node, through the agent, no SSH credential). It
  opens PowerShell on a pseudo console (ConPTY, Windows 10 1809 or later) as SYSTEM, only if the node's local policy
  allows it: the install dialog has a new "Allow shell" option, off by default (a reinstall changes it).
- **Windows agent: listening ports and service discovery**. The agent reads the listening TCP and UDP sockets with
  their owning process, the running Windows services (the counterpart of the running systemd units) and finds the
  catalog products by process name (PostgreSQL, Apache, nginx, HAProxy, MySQL, MariaDB, Redis, Valkey, MongoDB,
  Memcached, Grafana, Prometheus, Docker, sshd, Mosquitto, InfluxDB) with their port. Template suggestions and
  macros that follow a port, a process or a unit now work on Windows nodes. Configuration files are not read on
  Windows. Needs the new agent.

**Fixed**
- The Windows install command did not give the agent the release public key, so a Windows agent installed that way
  refused signed updates (`update_not_configured`). It does now. Agents installed before this fix must be
  reinstalled once.

## 0.10.2 — 2026-11-24

**Added**
- **Signed update for the Windows agent**: "Update agent" and "Update all" now work on Windows nodes. The Windows binary has
  its own signature (`janus-agent-windows-amd64.exe.sig`, platform `windows-amd64`), so a signature made for the Linux
  binary of the same architecture is never accepted. The running file is renamed to `.old`, the service recovery
  restarts it, and a rollback puts the old binary back if the new one does not connect. `make agent/sign`,
  `make release` and the image build now sign and check the `.exe` as well. A Windows agent older than this release
  must be reinstalled once (it cannot update itself).

## 0.10.1 — 2026-11-22

**Added**
- **Deeper service discovery on the agent**: besides PostgreSQL and Apache, the Linux agent now finds nginx, HAProxy,
  MySQL, MariaDB, Redis, Valkey, MongoDB, Memcached, Elasticsearch, RabbitMQ, Grafana, Prometheus, Docker, sshd,
  Postfix, dnsmasq, Mosquitto, InfluxDB and Nginx Proxy Manager (its ports and the domains of its proxy hosts), and reads their configuration files (ports, bind address, data directory,
  config file path, a few security settings such as `PermitRootLogin`). It is read only, from a fixed list of keys:
  no password, token or key is ever read, and a value that looks like a URL with credentials is dropped. The node
  page shows them under "Services found"; the nginx template takes its HTTP/HTTPS ports from the configuration.
  Needs the new agent version: nodes keep working with the old one.
- **Host discovery** (Inventory → Discovery): scan an IPv4 network (up to 4096 addresses) for hosts answering on TCP
  port 22, or any port you choose. Each host shows its reverse DNS name and SSH banner. Scan once on demand, or save
  the network and scan it every 15 minutes to a week: new hosts wait in a list until you add them to the inventory
  (pick an existing SSH credential or create one on the spot) or ignore them. Needs `inventory.manage`; every scan
  and import is audited. Migration `0041`.
- **Templates for the well-known server software**: about 140 built-in templates (databases, web servers and proxies,
  message brokers, container runtimes including Docker, monitoring, mail, DNS/LDAP/VPN/file services, CI and
  repositories), one per product, from one table (`monitoring/service_catalog.py`). Each has a service check, a port
  check per listening port, an HTTP health check where the product has one, discovery rules (unit, process, port)
  and macros the agent fills in. Six new template categories (migration `0039`). Reading a product's own numbers
  (queries, status pages) still needs a connector: only PostgreSQL, Apache and Proxmox have one.
- **Every one of those products is also a node service** (Inventory → Services, grouped by category): its port and,
  where it has one, its health URL, with no credential. New probes read them from the server, without the agent:
  `service_tcp.latency_ms`, `service_http.latency_ms`, `service_http.status`; the templates use them. Migration `0040`
  (the kind of a service is checked by code, not by a fixed list).

## 0.10.0 — 2026-11-21

**Added**
- **Windows agent**: a Windows service (`JanusAgent`) with metrics and one-time enrollment, installed with a PowerShell
  one-liner from the node page.
- **AI providers and fallback routes**: providers with presets (Groq, OpenRouter, NVIDIA, Cloudflare, Mistral, Cerebras,
  Hugging Face, Gemini, Ollama, local, custom), several vault-sealed keys per provider, tested models, routes that fall over to
  the next model on a limit, outage, timeout, bad key or missing model, health and usage. `janus-ops ai-import-providers`.
- **MCP tools for the Copilot**: `linux-mcp` and `proxmox-mcp` run with `docker run -i` (optional `deploy/compose.mcp.yaml`),
  configured from the inventory. Operational tools need the new permission `ai.operate`.
- **Operate**: a chat to carry out an instruction on the servers you pick, with a trace of every tool call.
- **Archive and retention**: recorded SSH sessions and Operate chats are archived after X days and deleted after Y
  (defaults 1 and 30); searchable archive page, *keep* flag, bulk delete. Permissions `archive.read`, `archive.manage`.
- **Console**: Settings in tabs, a menu grouped by area and collapsible, sortable tables with host and group filters
  (inventory, credentials, problems, audit log, users, notifications, SSH sessions, containers, Proxmox, Ansible, templates).
- Workflow runs resume after a restart; GitOps supports private https repositories.

**Changed**
- The Copilot's second opinion and the code generator also go through the routes.
- Migrations `0032`–`0038`.

**Fixed**
- Downgrade of migration 0025 no longer fails when TimescaleDB's policy job races the drop of an aggregate.

## 0.9.0 and earlier

See the status notes in [docs/PLAN.md](docs/PLAN.md).
