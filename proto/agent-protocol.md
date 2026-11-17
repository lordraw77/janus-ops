# Agent ⇄ server protocol, version 1

Contract between `janus-agent` (`agent/internal/proto`) and the agent gateway
(`backend/src/janus_ops/agents/gateway.py`). Both sides change together (ADR-001).

## Transport

- One WebSocket per agent: `GET <server_url>/api/v1/agent/ws` over TLS (port 443, through the
  same reverse proxy as the UI). The agent dials out; nothing listens on the node (NFR-01).
- HTTP(S) proxies are reached with `CONNECT`; the server certificate is verified against the
  system CAs plus the optional `ca_file` (Caddy's internal root by default).
- Text frames, one JSON object per frame, at most 1 MiB. Every object has a type `t`.
- Keepalive: WebSocket ping from the agent every 30 s. The server closes a connection that sent
  nothing for `3 × interval_s + 30` seconds.

## Authentication (ADR-002, AMEND v0.11)

Each agent owns an ed25519 key pair generated on the node (`<state_dir>/agent.key`, mode 0600).
The private key never leaves the node.

1. server → agent: `{"t": "challenge", "v": 1, "nonce": "<base64, 32 bytes>"}`
2. agent → server: `hello`

   ```json
   {"t": "hello", "v": 1, "node_id": "<uuid>", "public_key": "<base64, 32 bytes>",
    "signature": "<base64>", "enroll_token": "<only until enrolled>",
    "version": "0.2.0", "arch": "amd64", "os": "Ubuntu 24.04.3 LTS", "kernel": "6.8.0-…",
    "hostname": "docker", "interval_s": 10,
    "policy": {"allow_exec": false, "allow_pty": false, "allow_docker": false},
    "features": ["streams"]}
   ```

   `signature` = ed25519 over the UTF-8 bytes of
   `"janus-agent-auth-v1\n" + nonce + "\n" + node_id + "\n" + host`, where `nonce` is the base64
   string as received and `host` is the host[:port] of `server_url`.
3. The server accepts when the signature is valid and either
   - the public key equals the one registered for `node_id` and the agent is not revoked, or
   - `enroll_token` is a valid, unused, unexpired token for `node_id`: the token is consumed and
     the key is registered (first enrollment or re-bootstrap; a revoked agent is reinstated).
4. server → agent: `{"t": "welcome", "v": 1, "interval_s": 10, "enrolled": true}`.
   `enrolled: true` tells the agent to drop `enroll_token` from its configuration.

Refusals: `{"t": "error", "code": "..."}` then close with

| Close code | `code` | Agent behaviour |
|---|---|---|
| 4400 | `bad_message`, `unsupported_version` | retry with backoff |
| 4401 | `unauthorized` (bad signature, unknown key, invalid token) | retry slowly (5 min) |
| 4403 | `revoked` | retry slowly (5 min) |
| 4409 | `replaced` (another connection for the same node) | retry with backoff |

## Telemetry

agent → server, every `interval_s` (5–15 s) one *sample* is collected and stored in the local
buffer (bbolt) before sending; batches carry up to 60 samples, oldest first:

```json
{"t": "metrics", "seq": 1042, "samples": [
  {"ts": 1763112000000, "points": [
    {"n": "cpu.usage_pct", "v": 12.5},
    {"n": "fs.used_pct", "l": {"mount": "/"}, "v": 41.2}
  ]}
]}
```

- `seq` is the buffer key of the last sample in the batch; samples are strictly ordered.
- `ts` is Unix time in milliseconds. `n` matches `^[a-z][a-z0-9_.]{0,63}$`; `l` (labels) has at
  most 4 string values of at most 128 characters.
- server → agent `{"t": "ack", "seq": 1042}` once the batch is committed. The agent then deletes
  every sample up to `seq`. Unacknowledged samples are resent after a reconnect; the server
  ignores duplicates (unique `(series_id, time)`), so resending is safe (NFR-03, AC-05).
- Buffer limits on the agent: 24 h of samples; older ones are dropped first.

agent → server, every 60 s, the latest *facts* (not buffered, only the newest matters):

```json
{"t": "facts", "ts": 1763112000000, "data": {
  "uptime_s": 86400, "failed_units": ["foo.service"],
  "listening": [{"proto": "tcp", "addr": "0.0.0.0", "port": 22, "process": "sshd"}],
  "top_cpu": [{"pid": 1830, "name": "kvm 101 (web01)", "cmd": "/usr/bin/kvm -id 101 …",
               "user": "root", "cpu_pct": 50.5, "rss_bytes": 15538626560}],
  "top_mem": [{"pid": 1, "name": "systemd", "cmd": "/sbin/init", "user": "root",
               "cpu_pct": 0.1, "rss_bytes": 12000000}]}}
```

`top_cpu` and `top_mem` hold the 10 busiest processes; CPU is averaged since the previous facts
(60 s). `name` is the kernel command name, completed from the command line when the kernel cut
it at 15 characters; QEMU/KVM guests are named `kvm <vmid> (<name>)`. `cmd` (at most 256
characters) and `user` are optional; kernel threads have no `cmd`.

## Metric catalogue (v1)

| Name | Labels | Unit |
|---|---|---|
| `cpu.usage_pct`, `cpu.iowait_pct`, `cpu.steal_pct` | | % |
| `cpu.count` | | |
| `load.1`, `load.5`, `load.15` | | |
| `mem.used_pct`, `swap.used_pct` | | % |
| `mem.total_bytes`, `mem.available_bytes`, `swap.used_bytes` | | B |
| `disk.read_bytes_ps`, `disk.write_bytes_ps` | `device` | B/s |
| `disk.util_pct` | `device` | % |
| `fs.used_pct`, `fs.inodes_used_pct` | `mount` | % |
| `fs.free_bytes`, `fs.size_bytes` | `mount` | B |
| `net.rx_bytes_ps`, `net.tx_bytes_ps` | `iface` | B/s |
| `system.uptime_s` | | s |
| `proc.count` | | |
| `systemd.failed_units` | | |

## Streams (F4)

Feature `streams`, announced by the agent in `hello.features` (older agents are never sent
binary frames). Binary WebSocket frames: `[type: 1 byte][stream id: 4 bytes big-endian][payload]`.

| Type | Direction | Payload |
|---|---|---|
| 1 `OPEN` | server → agent | JSON `{"kind": "docker"}`, `{"kind": "exec", "argv": [...], "timeout_s": 60}` `{"kind": "pty", "cols": 80, "rows": 24}` or `{"kind": "update", "version", "sha256", "signature", "size"}` |
| 4 `OPEN_OK` | agent → server | — |
| 5 `OPEN_FAIL` | agent → server | reason: `docker_not_allowed`, `exec_not_allowed`, `pty_not_allowed`, `update_not_configured`, `bad_signature`, `not_newer`, `update_unavailable`, `docker_unavailable`, `exec_unavailable`, `pty_unavailable`, `bad_request`, `unknown_kind`, `too_many_streams` |
| 2 `DATA` | both | bytes (at most 32 KiB per frame) |
| 3 `CLOSE` | both | optional reason |
| 6 `RESIZE` | server → agent | `pty` streams only: columns and rows, 2 bytes big-endian each |

Kind `docker` relays the Docker Engine API socket (`docker_socket`, default
`/var/run/docker.sock`) only when the local policy has `allow_docker: true`. The agent never
interprets the bytes; the server decides which API calls are made. At most 64 open streams;
a stream whose write backlog overflows is closed so that it cannot stall the tunnel.

Kind `exec` (F8) runs `argv` directly, **without a shell**, only when the local policy has
`allow_exec: true`. `argv` has 1 to 64 arguments of at most 4096 bytes, no NUL bytes; the working
directory is `/`, the environment is minimal and stdin is empty. Output (stdout and stderr together)
goes back as `DATA` frames, at most 256 KiB, then the agent closes the stream with a `CLOSE` whose
payload is the reason: `exit:<code>`, `timeout` (`timeout_s`, default 60, at most 300),
`output_limit`, or `server_gone`. At most 4 commands run at once. The server never sends input.

Kind `pty` (F8, FR-83) starts a login shell (`/bin/bash`, else `/bin/sh`) on a pseudo-terminal, as the
agent's user, only when the local policy has `allow_pty: true`. `cols` (10–500) and `rows` (5–200)
default to 80×24. `DATA` from the server is the keystrokes, `DATA` from the agent is the terminal
output, `RESIZE` changes the window size. The agent closes the stream with `CLOSE` `exit:<code>` when
the shell ends; a session lasts at most 8 hours, and at most 4 run at once. Idle timeout, recording
and audit are the server's job (the same as for SSH terminals).

Kind `update` (F8, FR-84, C8) replaces the agent binary. The agent accepts it only if its configuration
has `update_public_key` (base64 ed25519). `signature` is the base64 ed25519 signature, made offline with
the release key, over `janus-agent-update-v1\n<version>\n<GOARCH>\n<sha256 hex>`; it is checked at OPEN
(`bad_signature`), and `version` must be newer than the running one (`not_newer`: no downgrade). The
server then sends exactly `size` bytes (at most 64 MiB) as `DATA`; the agent checks the SHA-256, installs
the file over its own executable, answers `CLOSE` `updated:<version>` (or `failed:<size|write|checksum|install>`)
and exits so that systemd restarts it. The server cannot forge a signature: it only distributes.

## Local policy (C9)

`policy` in the agent configuration is reported in `hello` and stored by the server for display.
The server cannot change it: exec, PTY and Docker streams (F4, F8) are refused by the agent unless
the local policy allows them. `allow_exec` is chosen at installation (bootstrap) and is only
changed by reinstalling the agent on the node.
