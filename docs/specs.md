The architecture of **Janus-Ops** rests on one fundamental principle: **a single central Inventory around which decoupled modules revolve**, fed by a self-contained micro-agent designed to survive in segmented networks, DMZs, or behind restrictive HTTP/HTTPS proxies.

Below is the complete breakdown into macro-blocks, with detailed functional specifications, technologies and protocols for each component.

---

## 1. The Micro-Agent: `janus-agent` (Zero-Dependency & Proxy-Ready)

To avoid problems with Python/pip dependencies, shared libraries (`glibc` mismatch between CentOS/Debian/Alpine) or firewall blocks on isolated networks, the agent is not written in Python but compiled as a **single static binary** (or bundle) sent directly by the Janus-Ops server over SSH during bootstrap.

* **Recommended Technology:** **Go (Golang)** compiled with `CGO_ENABLED=0` (produces a single static executable of ~12-18 MB for `linux/amd64`, `linux/arm64`, `windows/amd64` with no external dependencies).
* **Deploy Mechanism (Push from the Server):**
* The Janus-Ops server keeps the precompiled binaries for the various architectures in the `/opt/Janus-Ops/agent-bundles/` directory.
* When a host is added, the backend uses `asyncssh` to detect the OS and architecture (`uname -m`), copies the binary over SFTP to `/usr/local/bin/janus-agent`, generates the configuration file `/etc/janus-agent/config.yaml` (with a unique token and the server's CA certificate) and registers/starts the `systemd` service (or OpenRC/Windows Service).
* **Air-Gapped Auto-Update:** When you update Janus-Ops, the server notifies the connected agents, which download the new binary directly from the internal endpoint of the Janus-Ops server (`/api/v1/agent/binary/{arch}`), without ever touching the internet.


* **Network Architecture (Outbound-Only for Strict Proxies):**
* The agent **opens no listening ports** on the managed node. It is always the agent that initiates a persistent connection to the Janus-Ops server through **gRPC over HTTP/2** or a secure **WebSocket (`wss://`)** on port `443`.
* Native support for `HTTP_PROXY` / `HTTPS_PROXY` (including Basic/NTLM authentication and custom corporate CA certificates) and automatic reconnection with exponential backoff and a local on-disk buffer (SQLite/bbolt) so that no metrics are lost during a temporary disconnection.


* **Features Built into the Agent:**
* **System Telemetry (Checkmk-grade):** High-frequency collection (every 5-15 s) of CPU (per core, steal, iowait), RAM/Swap, Disk I/O, filesystem space, `systemd` unit state, listening ports, hardware temperatures/SMART and top processes.
* **Local Docker Proxy (Hawser-style):** When enabled, the agent talks locally to `/var/run/docker.sock` and acts as a secure bridge to the Janus-Ops server, removing the need to expose TCP port 2375 or to open multiple SSH tunnels to manage containers, logs and stats.
* **Remote Command / Task Executor:** Controlled execution of scripts, custom health checks (Checkmk local-plugin style) and PTY sessions (interactive terminal) encapsulated in the same `wss://` tunnel (useful when direct SSH to the node is blocked by the firewall).



---

## 2. Core Platform: Unified Inventory, Auth & Secret Vault

This is the relational brain that links every IP address or FQDN to all of its operational capabilities.

* **Technologies:** **Python 3.12+**, **FastAPI**, **SQLAlchemy 2.0 (async)**, **PostgreSQL 16 + TimescaleDB**, **Redis**.
* **Features:**
* **Unified Host Inventory:** Every `Node` record has dynamic attributes (Tags, Groups, `prod/lab/dmz` Environment) and activatable *Capabilities*: `[x] Janus Agent`, `[x] SSH Target`, `[x] Proxmox Node`, `[x] Docker Host`, `[x] SNMP Device`.
* **Secret & Credential Vault:** Encrypted store (AES-256-GCM / Fernet with a derived master key, or optional integration with **OpenBao / HashiCorp Vault**) for SSH private keys, `sudo` passwords, Proxmox API tokens, Docker registry tokens and SNMPv3 credentials.
* **RBAC & Immutable Audit Log:** Role management (Admin, Operator, Read-Only Auditor) and complete tracking of who opened an SSH shell, who restarted a container or launched an Ansible playbook (with optional recording of terminal sessions in asciinema format).



---

## 3. Observability & Alerting Module (Checkmk Engine)

It replicates the diagnostic precision and information density of Checkmk, while overcoming its graphical rigidity.

* **Technologies:** **TimescaleDB** (hypertables for high-performance time series), **Taskiq / Celery** (distributed scheduler), **pysnmp** (for NAS, switches, UPS, printers), **ECharts** (for hexagonal status charts and zoomable historical series).


* **Features:**
* **Automatic Service Discovery:** When you add a host (via agent or SNMP), Janus-Ops automatically detects disks, network interfaces, critical `systemd` services, ZFS pools, Docker containers and Proxmox VMs, proposing the checks to enable with one click.
* **Multi-Level Thresholds & Hysteresis:** `OK`, `WARN`, `CRIT`, `UNKNOWN` states with tolerance windows (e.g. "alert only if CPU > 90% for 3 consecutive cycles") to avoid false positives.


* **Operational Incident Management:** NOC-style functions such as *Acknowledge* (taking ownership with a comment), *Scheduled Downtime* (planned maintenance that silences alerts), *Flapping Detection* and *Parent/Child Dependencies* (if a switch or a Proxmox node goes down, it suppresses the alerts of the 20 child VMs, marking only the parent node as `DOWN`).
* **Notification Router:** Alert delivery via Telegram, Webhook, Email, Slack/Discord with escalation rules.



---

## 4. Container & Stack Manager Module (Dockhand Engine)

Complete management of the Docker lifecycle on single hosts or multi-node clusters.

* **Technologies:** **Docker SDK for Python (`aiodocker`)**, communicating through the `janus-agent` tunnel or through a native `ssh://` tunnel.
* **Features:**
* **Multi-Environment View:** Per-Docker-host card dashboard (count of running/stopped/unhealthy containers, resource usage, recent events) and a global aggregated view of all the containers in the infrastructure.


* **Advanced Container Management:** Start/Stop/Restart/Kill, full JSON inspection, live stats (CPU/RAM/Net/Block I/O), real-time log streaming with regex search/filter, and an interactive **Exec Shell** in the browser.
* **Compose Stacks & GitOps:** Built-in YAML editor (Monaco Editor with `docker-compose` schema validation), encrypted `.env` variable management, stack deploy/redeploy, and direct synchronisation from Git repositories (GitHub/self-hosted Gitea) with auto-deploy webhooks.
* **Maintenance & Registry:** Volume management (with file/backup browser), Networks, smart cleanup (selective *Prune* of dangling images/orphaned volumes) and image update checks against registries (Docker Hub, GHCR, private registries).



---

## 5. Virtualisation & Cluster Module (Proxmox VE Manager)

Centralised control of one or more Proxmox VE clusters and standalone nodes, without jumping between different web interfaces.

* **Technologies:** **Proxmoxer** (asynchronous Python wrapper over the official Proxmox REST APIs), **noVNC / xterm.js** (for graphical and serial consoles).
* **Features:**
* **Cluster & Node Overview:** Quorum status, node load, shared storage usage (NFS, Ceph, local ZFS), temperature and disk status.
* **VM & LXC Lifecycle:** Quick creation from Cloud-Init templates, Start, Shutdown, Reboot, Snapshot, Clone, live migration between cluster nodes and on-the-fly resource resizing.
* **Integrated Console:** Direct access to the LXC/VM serial console through `xterm.js`, or to the VNC/SPICE graphical console, right inside a Janus-Ops tab.
* **Backup & Replication Audit:** Dedicated monitoring of the state of `vzdump` / Proxmox Backup Server (PBS) jobs — exactly as shown in your Checkmk dashboard — with immediate evidence of missing, failed or too-old backups.





---

## 6. SSH Connection Manager & Bastion Terminal Module

A web-based replacement for MobaXterm / Termius, integrated into the inventory.

* **Technologies:** **AsyncSSH** (backend), **xterm.js** + WebSockets (frontend).
* **Features:**
* **1-Click Access & Multi-Tab:** Instant opening of SSH sessions on any node in the inventory (using the credentials saved in the Vault, never exposing private keys to the browser) in side-by-side tabs or split-screen.
* **Integrated SFTP File Browser:** Side panel to browse the remote filesystem, upload/download files by drag-and-drop and edit configuration files (`/etc/...`) directly with the portal's editor.
* **Command Broadcast & Snippet Library:** The ability to send the same command simultaneously to 5 open terminals (e.g. during a manual update) and a shared library of parametrised quick commands.
* **Transparent ProxyJump / Bastion:** If a host is reachable only through a jump host or through the reverse tunnel of `janus-agent`, Janus-Ops routes the session automatically.



---

## 7. Automation Module: Ansible & Visual Workflow Engine

The operational engine that turns monitoring events or user requests into concrete actions.

* **Technologies:** **ansible-runner** (for asynchronous playbook execution with JSON event streaming), **React Flow** (frontend for the visual node builder), **Temporal** or **Celery Canvas** (workflow orchestration).
* **Features:**
* **Ansible Control Center:**
* Dynamic generation of the Ansible inventory directly from the Janus-Ops database (groups, tags and host variables always in sync).
* Playbook catalogue (imported from Git or created in the editor), dynamic forms generated from variables (`survey`), execution with live output formatted per task/host, and execution history.


* **Visual Workflow Manager (Event-Driven & Scheduled):**
* Node-based editor in the style of n8n/Node-RED, designed for system administration operations.
* **Triggers:** Cron schedule, Webhook, manual button, or **Monitoring Alert** (e.g. *If the `/var/log` disk on a node exceeds 90%*).
* **Action Nodes:** Run an SSH command, launch an Ansible playbook, restart a Docker container, create a Proxmox snapshot, ask the AI for analysis, wait for human approval (human-in-the-loop), send a Telegram notification.





---

## 8. Janus AI Copilot Module (Troubleshooting & Infrastructure-as-Code)

A contextual operations assistant that knows the real topology of your infrastructure.

* **Technologies:** **LiteLLM / OpenAI-compatible SDK** (connected to your local Ollama / vLLM / SGLang endpoints or to cloud APIs), internal **Tool Calling / MCP (Model Context Protocol)**, read-only by default.
* **Features:**
* **Root Cause Analysis (RCA) on Alerts:** By clicking *"Diagnose"* on a `CRIT` service in the dashboard, the AI automatically gathers the latest system logs (`journalctl`), the container logs, the recent metrics and the latest changes recorded in the audit log, returning a precise diagnosis and the recommended command/playbook to fix it.


* **Contextual IaC Generator:** Assisted generation of `docker-compose.yml`, Ansible playbooks, `systemd` units or regex filters, already calibrated to the IPs, storage paths and conventions of your inventory.
* **Safe Execution Guardrails:** The AI **never runs** destructive commands on its own; it generates an *Action Plan* (e.g. a playbook or a sequence of commands) that the administrator can review, edit and approve with one click.



---

## 9. Frontend Architecture (Checkmk + Dockhand Mix)

* **Technologies:** **Next.js 15 (App Router)**, **TypeScript**, **Tailwind CSS**, **Shadcn UI**, **TanStack Query + Zustand** (real-time state management over WebSocket), **ECharts** (NOC charts), **Monaco Editor** (YAML/JSON/Bash), **xterm.js** (terminal), **React Flow** (workflow).
* **UI Structure:**
* **Global Command Palette (`Ctrl+K`)**: Instant navigation ("go to pve01", "open ssh on docker2", "run backup check", "ask the AI why nginx is down").


* **Hybrid NOC Dashboard:**
* At the top: Checkmk-style hexagonal charts (Host & Service stats) + real-time event feed.


* In the middle: Dockhand-style grid of Node Cards with CPU/RAM bars, container/VM status and quick buttons (`SSH`, `Ansible`, `AI`).


* At the bottom: A dense table of *Unhandled Problems* with high-contrast badges (`CRIT`, `WARN`, `DOWN`) and a quick AI-diagnosis button.
