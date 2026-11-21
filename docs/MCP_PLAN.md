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
- **How to spawn (decided)**: `docker run --rm -i` of the published image, so MCP updates are not tied
  to Janus-Ops releases (the image tag is a setting, default `latest`, pinnable). The backend needs the
  Docker CLI and a Docker socket (or a `DOCKER_HOST` pointing at a socket proxy limited to
  `containers/create|start|attach|wait|delete` and `images/create|json`). Secrets travel as `-e NAME`
  with the values in the `docker` client's environment, never on argv; private keys are written by a
  wrapper entrypoint from env vars into a tmpfs inside the container (`KEY_DIR`), because a path of the
  Janus-Ops container does not exist on the host. The runner reads the image's original
  Entrypoint/Cmd with `docker image inspect` and wraps it.
- One MCP session per Copilot run, closed at the end (no long-lived process holding credentials).
  Each run gets only the nodes in the problem's scope (`scope.py` is applied before building the spec).

## 4. Safety (the important part)

- **Read tools** are offered to the model for anyone with `ai.use`. **Operational (write) tools** (e.g.
  `service_control`, `vm_stop`, `node_reboot`, `execute_command`) need the new permission **`ai.operate`**:
  configurable per role, **on by default for the `admin` role** (admin holds every permission) and off
  for every other role. The tool is classified read/write by an allow-list; an unclassified tool is
  never offered, and a test fails the build on a new one.
- Operational tools may be called **directly** by the model, within the run of a user who has
  `ai.operate`, without the Action Plan. Safeguards: scope (only nodes the user may reach), audit of
  every call with the user as actor, a per-run cap on write calls, a global switch in the AI settings
  ("allow operational tools", default on), and the environment rule (a `production` node can require
  the Action Plan instead; setting per environment, default off).
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

## 7. Decisions taken

- Spawn with `docker run` (MCP updates independent of Janus-Ops releases).
- Operational tools allowed directly, under `ai.operate` (admin by default, configurable per role).
- Still open: tool-calling support of the configured model (capability flag in the AI settings).

## 8. Progress

- Step 1 done: `ai/mcp/spec.py` (mapper) with unit tests; permission `ai.operate`.
- Step 2 done: `ai/mcp/runner.py` (`docker run -i`, secrets only through the client's environment, keys
  written into a tmpfs by a wrapper, cap-drop ALL; official `mcp` SDK stdio client). Tested against the
  real images (91 Proxmox tools, 48 Linux tools). The image now carries the docker CLI; the socket comes
  from the optional `deploy/compose.mcp.yaml`.
- Step 3 done: `ai/mcp/catalog.py` allow-list (read / write / hidden) with a snapshot test of the 139
  published tools (48 + 91): a tool in the images but not in the catalog fails the test.
- Step 4 done: tool calling in `llm.complete`, loop in `diagnose._converse` (8 rounds, 4 calls per round,
  12 calls and 3 write calls per run, fallback to a plain request when the model rejects tools or no
  server starts), `ai_step` table (0033), audit `ai.mcp.call`, trace in the Copilot page. v1 scope: the
  servers see only the node of the problem (its `ssh` and `proxmox` capabilities).
- Enable with `JANUS_AI_MCP_ENABLED=true` (set by `deploy/compose.mcp.yaml`).
- Live test done (stack on 0.9.0 built from source + `compose.mcp.yaml`, NVIDIA model, node e2e-debian):
  the model read the node's journal, processes and ports through linux-mcp (8 calls, 140-1100 ms each) and
  diagnosed the agent disconnections. Found and fixed: MCP containers need the backend's network
  (`JANUS_AI_MCP_NETWORK=self`), and the prompt now says a tool error is not evidence about the node.
  Write tools not exercised live (no permission needed to test the read path).
- Write path tried live: with `ai.operate` the model is offered 46 tools, without it 28; a write tool
  (create_directory, delete_path) ran on e2e-debian and was removed again.
- Settings card (Settings > Copilot tools): on/off, global switch for operational tools, environments
  where only an action plan is allowed, Docker status, per-node reachability (`ai_mcp_config`, 0034).
  Saved values win over `JANUS_AI_MCP_*`.
- Not done, by decision: a wider scope (the hypervisor of a VM). The inventory has no VM-to-hypervisor
  link, so it needs a data-model change first (a `hypervisor` field on a node); until then a run sees
  only its own node.
