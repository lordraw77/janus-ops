# Plan: MCP servers for the Copilot (linux-mcp, proxmox-mcp)

Goal: give the Copilot real tools (today it only gets a context bundle assembled up front by
`ai/diagnose.py`). The two MCP servers (`lordraw/linux-mcp`, 48 tools over SSH; `lordraw/proxmox-mcp`,
91 tools over the PVE API) run in **stdio mode**, spawned by Janus-Ops, with their configuration
**computed from the inventory** and injected at spawn time. Nothing is configured by hand in `.env`.

## 1. What the two images expect (from Docker Hub)

| | linux-mcp | proxmox-mcp |
|---|---|---|
| Transport | `UXMCP_TRANSPORT=stdio` (default) | `PROXMOX_MCP_TRANSPORT=stdio` (default) |
| Config | numbered blocks `UXMCP_SERVER_N_{LABEL,HOST,PORT,USER,KEY_PATH,PASSWORD,SUDO_PASSWORD,TAGS,VMID}` | one cluster: `PROXMOX_MCP_{HOST,PORT,USER,TOKEN_ID,TOKEN_SECRET,PASSWORD,VERIFY_SSL}` |
| Secrets | key file path (needs a mounted file) or password | token (preferred) or password |
| Fleet | many servers per process | one Proxmox per process |

## 2. Inventory → configuration mapping

- **linux-mcp**: every node with an enabled `ssh` capability becomes block N. `LABEL` = node name,
  `HOST` = node address, `PORT`/`USER` from `SshConfig`, `TAGS` from environment and groups. Credential
  `ssh_key` → a key file written to a tmpfs (mode 0400) and mounted read-only, `KEY_PATH` points at it;
  `ssh_password` → `PASSWORD`; the node's `sudo_password` credential → `SUDO_PASSWORD`. If the node has a
  `proxmox` VMID in inventory, `VMID` is set. Nodes behind a jump host are skipped in v1 (the image has
  no ProxyJump): reported as "not reachable by MCP".
- **proxmox-mcp**: one process per node with an enabled `proxmox` capability. `HOST`/`PORT`/`VERIFY_SSL`
  from `ProxmoxConfig`; `proxmox_token` → `TOKEN_ID`+`TOKEN_SECRET`, `proxmox_password` → `PASSWORD`.
- The mapper is a pure function `build_env(inventory_rows, decrypted_credentials) -> McpSpec` with unit
  tests; it never logs values. Secrets are decrypted only at spawn and live in the child's environment
  and a tmpfs, never in the database or in prompts.

## 3. Running the servers (stdio)

- A new `ai/mcp/` package: `spec.py` (mapper), `runner.py` (spawns the process and speaks JSON-RPC over
  stdin/stdout with the official `mcp` Python SDK's stdio client), `registry.py` (tool catalogue,
  allow-list).
- **How to spawn**: two options, decide in step 0.
  1. *Sidecar via Docker socket*: `docker run --rm -i --env-file <tmpfs> lordraw/linux-mcp`. Needs the
     socket mounted into Janus-Ops (a broad privilege) — avoid unless needed.
  2. **Recommended**: bake both servers into the Janus-Ops image (pip-install from their repos, pinned
     versions, run as `python -m ...` child processes). No socket, no extra containers, secrets stay in
     process memory. Costs image size; both are Python (`python:3.12-slim` base, same as ours).
- One MCP session per Copilot run, closed at the end (no long-lived process holding credentials).
  Each run gets only the nodes in the problem's scope (`scope.py` is applied before building the spec).

## 4. Safety (the important part)

- **Allow-list, not block-list**: the registry maps each MCP tool to `read` or `write`. Only `read` tools
  are exposed to the model by default (e.g. `list_processes`, `disk_usage`, `docker_ps`, `tail_log`,
  `vm_status`, `cluster_resources`). `write` tools (`service_control`, `vm_stop`, `delete_path`,
  `node_reboot`, ...) are never offered to the model; they can only appear as a proposed action in the
  Action Plan and go through the existing approval flow (`actions.execute`, audit).
- `execute_command`/`lxc_exec`/`vm_agent_exec` are `write` by definition (arbitrary code).
- Every tool call is audited (tool, node, arguments; result truncated). Output is cut to a fixed size and
  wrapped as untrusted data, as today (prompt-injection risk C10/E).
- A per-run budget: max tool calls, max wall time, max bytes.
- Credentials: the existing rule that AI tools never read the vault is relaxed only inside the runner,
  which is not reachable from the model. A test asserts no secret appears in any message sent to the LLM.

## 5. Copilot: function-calling loop ("advanced Copilot")

- `llm.py` gains tool calling (OpenAI-compatible `tools` / `tool_calls`), with a fallback to the current
  one-shot context for models without it (many self-hosted ones).
- Loop in `diagnose.py`: context bundle → model → tool calls → results → … → Action Plan JSON. Steps are
  stored (`ai_step` table: tool, node, args, truncated result, duration) and shown in the UI
  ("what the AI looked at"), next to "what was sent".
- Chat-style follow-up on a diagnosis, multi-node investigation, and IaC suggestions reuse the same loop.

## 6. Delivery steps

0. Decide spawn strategy (recommended: bundled) and pin versions (linux-mcp v2.0.3, proxmox-mcp v1.3.0).
1. `ai/mcp/spec.py` mapper + unit tests (fixtures for key, password, sudo, tags, jump host skipped, Proxmox
   token/password).
2. Runner with stdio client; integration test against the real images and the e2e sshd (`tests/_sshd.py`).
3. Tool catalogue with read/write classification, generated from the servers' `tools/list` and reviewed;
   test that every tool is classified (a new unclassified tool fails the build).
4. Tool calling in `llm.py` + loop in `diagnose.py`, budgets, audit, `ai_step` migration (0033).
5. UI: tool trace, settings page "MCP" (enable per server type, read-only switch, see which nodes are
   reachable and why not).
6. Live test on the stack with the real NVIDIA model (preferred provider), docs, release 0.10.0.

## 7. Open decisions

- Bundled vs Docker-socket spawn (see §3).
- Whether `write` tools may ever be called directly after a human approves in the UI, or always via the
  Action Plan only (recommended: Action Plan only).
- Tool-calling support of the configured model: needs a capability flag in the AI settings.
