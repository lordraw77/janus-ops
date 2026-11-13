 **Janus-Ops** is a unified administrative portal designed to eliminate context-switching. Instead of jumping between Checkmk for monitoring, Dockhand/Portainer for containers, Proxmox UI for virtualization, MobaXterm for SSH, and terminal windows for Ansible, JanusOps brings your entire infrastructure lifecycle into a single, cohesive interface.

Built with a modern stack (**FastAPI** + **Next.js**) and a zero-dependency **Go Micro-Agent**, it's designed to securely manage even air-gapped or proxy-restricted environments.

### 🚀 Core Capabilities
- **👁️ Observability (Checkmk-Style):** High-frequency metrics, smart alerting, and hex-badge NOC dashboards for host and service health.
- **🐳 Container Engine (Dockhand-Style):** Multi-node Docker management, Compose GitOps deployments, and live container terminal/stats.
- **🛡️ Virtualization (Proxmox VE):** Centralized cluster control, VM/LXC lifecycle management, and backup audits.
- **⚡ Automation Hub:** Native Ansible playbook execution and a visual event-driven workflow manager.
- **🔑 SSH Bastion:** Built-in web terminal (xterm.js) with connection manager, credential vault, and multi-tab broadcasting.
- **🤖 Janus AI Copilot:** A context-aware assistant that automatically retrieves logs and metrics for failing services, providing instant Root Cause Analysis (RCA) and safe remediation playbooks.
