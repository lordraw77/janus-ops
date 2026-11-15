"""Playbook runs: plan the nodes, materialise the frozen source, run ansible-playbook, keep the log.

A run is planned when it is requested (scope, pinned host keys, credential kind), so the user
sees at once which nodes are skipped and why. Execution happens in a background task that
reads the frozen file versions, reveals the SSH keys from the vault for the time of the run only,
and removes everything it wrote in a private temporary directory afterwards.

Known limits of this release: only SSH key credentials (passphrase-less), no jump hosts, host
keys must already be pinned by the SSH bastion (ssh_host_key), and ansible-playbook must be
installed on the Janus-Ops host.
"""

import asyncio
import json
import logging
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from janus_ops import audit
from janus_ops.ansible.catalog import validate_rel_path
from janus_ops.crypto import DecryptionError
from janus_ops.vault import Vault, VaultUnavailable

log = logging.getLogger(__name__)

RUN_TIMEOUT_S = 3600
MAX_LOG_CHARS = 1024 * 1024
FLUSH_EVERY_S = 2.0
ANSIBLE_BINARY = "ansible-playbook"

_tasks: set[asyncio.Task[None]] = set()


@dataclass(frozen=True)
class Runnable:
    node_id: str
    name: str
    address: str
    port: int
    username: str
    credential_id: str
    host_key: str  # OpenSSH public key line, as pinned by the bastion


def _entry(address: str, port: int) -> str:
    return address if port == 22 else f"[{address}]:{port}"


async def plan_nodes(
    conn: AsyncConnection, node_ids: list[str]
) -> tuple[list[Runnable], list[dict[str, str]]]:
    """Nodes that can run Ansible now, and the ones skipped with a stable reason code."""
    rows = await conn.execute(
        text(
            "SELECT n.id, n.name, n.address, c.enabled, c.config, c.credential_id,"
            " cr.kind AS cred_kind, cr.username AS cred_user, h.public_key AS host_key"
            " FROM node n"
            " LEFT JOIN node_capability c ON c.node_id = n.id AND c.kind = 'ssh'"
            " LEFT JOIN credential cr ON cr.id = c.credential_id"
            " LEFT JOIN ssh_host_key h ON h.node_id = n.id"
            " WHERE n.id = ANY(CAST(:ids AS uuid[]))"
        ),
        {"ids": node_ids},
    )
    found = {str(r.id): r for r in rows}
    runnable: list[Runnable] = []
    skipped: list[dict[str, str]] = []
    for node_id in node_ids:
        row = found.get(node_id)
        if row is None:
            skipped.append({"node": node_id, "reason": "not_found"})
            continue
        reason = _skip_reason(row)
        if reason is not None:
            skipped.append({"node": row.name, "reason": reason})
            continue
        config: dict[str, Any] = row.config or {}
        runnable.append(
            Runnable(
                node_id=str(row.id),
                name=row.name,
                address=row.address,
                port=int(config.get("port", 22)),
                username=config.get("username") or row.cred_user,
                credential_id=str(row.credential_id),
                host_key=row.host_key.strip(),
            )
        )
    return runnable, skipped


def _skip_reason(row: Any) -> str | None:
    if row.enabled is None:
        return "no_ssh_capability"
    if not row.enabled:
        return "ssh_disabled"
    if row.credential_id is None:
        return "no_credential"
    if row.cred_kind != "ssh_key":
        return "password_credentials_not_supported"
    config: dict[str, Any] = row.config or {}
    if config.get("jump_host_id"):
        return "jump_host_not_supported"
    if not (config.get("username") or row.cred_user):
        return "no_username"
    if not row.host_key:
        return "host_key_not_pinned"
    return None


def inventory(nodes: list[Runnable], key_paths: dict[str, Path], known_hosts: Path) -> str:
    """JSON is valid YAML, so the file is read by ansible's YAML inventory plugin."""
    hosts: dict[str, Any] = {}
    for node in nodes:
        hosts[f"h_{node.node_id.replace('-', '')}"] = {
            "ansible_host": node.address,
            "ansible_port": node.port,
            "ansible_user": node.username,
            "ansible_ssh_private_key_file": str(key_paths[node.node_id]),
            "ansible_ssh_common_args": (
                f"-o UserKnownHostsFile={known_hosts} -o StrictHostKeyChecking=yes"
                " -o IdentitiesOnly=yes -o BatchMode=yes"
            ),
        }
    return json.dumps({"all": {"hosts": hosts}}, indent=2)


def known_hosts_text(nodes: list[Runnable]) -> str:
    lines = sorted({f"{_entry(n.address, n.port)} {n.host_key}" for n in nodes})
    return "\n".join(lines) + "\n"


def write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as handle:
        handle.write(content)


def materialise(root: Path, files: dict[str, str]) -> None:
    """Write the frozen source under `root`. Paths are validated again before any write."""
    for raw_path, content in files.items():
        rel = validate_rel_path(raw_path)
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _runner_config(home: Path) -> str:
    return (
        "[defaults]\n"
        "host_key_checking = True\n"
        "retry_files_enabled = False\n"
        "nocolor = True\n"
        "gathering = explicit\n"
        f"local_tmp = {home / '.ansible-local'}\n"
    )


async def execute(engine: AsyncEngine, vault: Vault, run_id: str) -> None:
    """Background task: one run from `queued` to a final status. Never raises."""
    try:
        await _execute(engine, vault, run_id)
    except Exception:
        log.exception("ansible run %s crashed", run_id)
        await _finish(engine, run_id, "error", None, "run_error")


async def _finish(
    engine: AsyncEngine, run_id: str, status: str, rc: int | None, error: str | None
) -> None:
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "UPDATE job_run SET status = :s, return_code = :rc, error = :e,"
                    " finished_at = now() WHERE id = :id RETURNING source_name, path, started_by"
                ),
                {"s": status, "rc": rc, "e": error, "id": run_id},
            )
        ).first()
        if row is not None:
            await audit.append(
                conn,
                action="ansible.run.finished",
                actor_name=row.started_by,
                target_type="playbook",
                target_id=f"{row.source_name}:{row.path}",
                payload={"run": run_id, "status": status, "return_code": rc, "error": error},
            )


async def _append_log(engine: AsyncEngine, run_id: str, chunk: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE job_run SET log = CASE WHEN length(log) >= :max THEN log"
                " ELSE left(log || :c, :max) END WHERE id = :id"
            ),
            {"c": chunk, "max": MAX_LOG_CHARS, "id": run_id},
        )


async def _execute(engine: AsyncEngine, vault: Vault, run_id: str) -> None:
    async with engine.begin() as conn:
        job = (
            await conn.execute(
                text(
                    "SELECT source_name, path, playbook_content, manifest, node_ids, check_mode,"
                    " skipped FROM job_run WHERE id = :id AND status = 'queued'"
                ),
                {"id": run_id},
            )
        ).first()
        if job is None:
            return
        await conn.execute(
            text("UPDATE job_run SET status = 'running', started_at = now() WHERE id = :id"),
            {"id": run_id},
        )
        runnable, _ = await plan_nodes(conn, [str(n) for n in job.node_ids])
        manifest: dict[str, dict[str, Any]] = job.manifest
        version_ids = [str(v["id"]) for v in manifest.values()]
        versions = await conn.execute(
            text(
                "SELECT v.id, f.path, v.content FROM ansible_file_version v"
                " JOIN ansible_file f ON f.id = v.file_id WHERE v.id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": version_ids},
        )
        source_files = {r.path: r.content for r in versions}
        secrets: dict[str, dict[str, Any]] = {}
        credentials = await conn.execute(
            text(
                "SELECT id, ciphertext, nonce, dek_wrapped FROM credential"
                " WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": list({n.credential_id for n in runnable})},
        )
        cred_rows = {str(r.id): r for r in credentials}

    skipped: list[dict[str, str]] = list(job.skipped)
    usable: list[Runnable] = []
    for node in runnable:
        cred = cred_rows.get(node.credential_id)
        try:
            if cred is None:
                raise DecryptionError("missing")
            secret = vault.reveal(node.credential_id, cred.ciphertext, cred.nonce, cred.dek_wrapped)
        except VaultUnavailable:
            skipped.append({"node": node.name, "reason": "vault_unavailable"})
            continue
        except DecryptionError:
            skipped.append({"node": node.name, "reason": "credential_unreadable"})
            continue
        if secret.get("passphrase"):
            skipped.append({"node": node.name, "reason": "passphrase_key_not_supported"})
            continue
        secrets[node.node_id] = {"secret": secret["secret"]}
        usable.append(node)

    if not usable:
        await _append_log(engine, run_id, "No node can run this playbook.\n")
        await _finish(engine, run_id, "error", None, "no_runnable_nodes")
        return

    skip_note = "".join(f"skipped {s['node']}: {s['reason']}\n" for s in skipped)
    if skip_note:
        await _append_log(engine, run_id, skip_note)

    with tempfile.TemporaryDirectory(prefix="janus-ansible-") as tmp:
        home = Path(tmp)
        src = home / "src"
        src.mkdir()
        keys: dict[str, Path] = {}
        for node in usable:
            key = home / f"key_{node.node_id.replace('-', '')}"
            write_private(key, secrets[node.node_id]["secret"].rstrip("\n") + "\n")
            keys[node.node_id] = key
        known = home / "known_hosts"
        write_private(known, known_hosts_text(usable))
        inv = home / "inventory.yml"
        write_private(inv, inventory(usable, keys, known))
        write_private(home / "ansible.cfg", _runner_config(home))
        materialise(src, source_files)
        secrets.clear()

        command = [ANSIBLE_BINARY, "-i", str(inv), job.path]
        if job.check_mode:
            command.append("--check")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "ANSIBLE_CONFIG": str(home / "ansible.cfg"),
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_FORCE_COLOR": "0",
            "ANSIBLE_HOST_KEY_CHECKING": "True",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(src),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            await _append_log(engine, run_id, f"{ANSIBLE_BINARY} is not installed.\n")
            await _finish(engine, run_id, "error", None, "ansible_not_installed")
            return

        deadline = time.monotonic() + RUN_TIMEOUT_S
        pending = ""
        last_flush = time.monotonic()
        if process.stdout is None:
            raise RuntimeError("ansible-playbook started without a stdout pipe")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                await process.wait()
                await _append_log(engine, run_id, pending + "\n[timeout]\n")
                await _finish(engine, run_id, "error", None, "timeout")
                return
            try:
                line = await asyncio.wait_for(process.stdout.readline(), remaining)
            except TimeoutError:
                continue
            if not line:
                break
            pending += line.decode("utf-8", errors="replace")
            if time.monotonic() - last_flush >= FLUSH_EVERY_S:
                await _append_log(engine, run_id, pending)
                pending = ""
                last_flush = time.monotonic()
        rc = await process.wait()
        if pending:
            await _append_log(engine, run_id, pending)
    await _finish(engine, run_id, "succeeded" if rc == 0 else "failed", rc, None)


def start(engine: AsyncEngine, vault: Vault, run_id: str) -> None:
    task = asyncio.create_task(execute(engine, vault, run_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def new_manifest(files: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {path: {"id": str(v["id"]), "sha256": v["sha256"]} for path, v in files.items()}
