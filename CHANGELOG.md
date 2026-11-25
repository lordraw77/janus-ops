# Changelog

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
