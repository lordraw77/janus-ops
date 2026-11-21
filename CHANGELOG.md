# Changelog

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
