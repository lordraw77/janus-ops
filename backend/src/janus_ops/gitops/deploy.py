"""GitOps deploys (F8, FR-81): a Compose file from a Git repository, run on a node via its agent.

A deploy is a fixed sequence of commands without a shell (see `janus_ops.agents.exec`): clone or
update the checkout under `GITOPS_ROOT`, record the commit, then `docker compose up -d`. The
values that reach argv are validated here and again by the database. A deploy stops at the first
command that fails. Its output is kept, capped, in the run row.
"""

import asyncio
import base64
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops import audit
from janus_ops.agents import exec as node_exec
from janus_ops.agents.hub import AgentHub
from janus_ops.vault import Vault

GITOPS_ROOT = "/var/lib/janus-ops/gitops"
OUTPUT_KEEP = 64 * 1024
GIT_TIMEOUT_S = 120
GIT_SHORT_TIMEOUT_S = 30
COMPOSE_TIMEOUT_S = 300
DEFAULT_COMPOSE_FILE = "docker-compose.yml"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
COMPOSE_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,254}$")
URL_MAX = 2048
log = logging.getLogger(__name__)


class GitopsError(Exception):
    """Invalid input; `code` is stable and shown to the user."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Stack:
    id: str
    name: str
    node_id: str
    repo_url: str
    branch: str
    compose_file: str
    credential_id: str | None = None


def validate_name(value: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise GitopsError("invalid_name")
    return value


def validate_repo_url(value: str) -> str:
    """https only, no credentials in the URL (they would be stored in clear on the node)."""
    if len(value) > URL_MAX or any(c.isspace() for c in value):
        raise GitopsError("invalid_repo_url")
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        raise GitopsError("invalid_repo_url") from None
    if parts.scheme != "https" or not host or "@" in parts.netloc or parts.query or parts.fragment:
        raise GitopsError("invalid_repo_url")
    if parts.path in ("", "/"):  # a repository path is required (the database checks the same)
        raise GitopsError("invalid_repo_url")
    return value


def validate_branch(value: str) -> str:
    if not BRANCH_RE.fullmatch(value) or ".." in value or value.endswith(("/", ".")):
        raise GitopsError("invalid_branch")
    return value


def validate_compose_file(value: str) -> str:
    if not COMPOSE_RE.fullmatch(value) or value.startswith("/"):
        raise GitopsError("invalid_compose_file")
    if any(part in ("", "..") for part in value.split("/")):
        raise GitopsError("invalid_compose_file")
    return value


def checkout_dir(stack_id: str) -> str:
    # the id is a UUID: it cannot carry a path separator into argv
    return f"{GITOPS_ROOT}/{uuid.UUID(stack_id)}"


@dataclass(frozen=True)
class Step:
    label: str
    argv: list[str]
    timeout_s: int


def _env_no_prompt(*args: str) -> list[str]:
    # git must fail instead of waiting for credentials that nobody can type
    return ["env", "GIT_TERMINAL_PROMPT=0", *args]


def auth_header(username: str | None, token: str) -> str:
    """The HTTP header that carries a git token (Basic: GitHub, GitLab, Gitea, Forgejo accept it).
    It is passed with `git -c`, so it is never written to the checkout's config."""
    raw = f"{username or 'git'}:{token}".encode()
    return "Authorization: Basic " + base64.b64encode(raw).decode()


def plan(stack: Stack, checkout_exists: bool, header: str | None = None) -> list[Step]:
    """The commands of a deploy, in order. Only validated fields are used. `header` is the
    authentication of a private repository, given only to the commands that reach the network."""
    auth = ["-c", f"http.extraHeader={header}"] if header else []
    directory = checkout_dir(stack.id)
    steps = [Step("mkdir", ["mkdir", "-p", GITOPS_ROOT], GIT_SHORT_TIMEOUT_S)]
    if checkout_exists:
        steps += [
            Step(
                "git remote",
                _env_no_prompt(
                    "git", "-C", directory, "remote", "set-url", "origin", stack.repo_url
                ),
                GIT_SHORT_TIMEOUT_S,
            ),
            Step(
                "git fetch",
                _env_no_prompt(
                    "git",
                    *auth,
                    "-C",
                    directory,
                    "fetch",
                    "--depth",
                    "1",
                    "origin",
                    stack.branch,
                ),
                GIT_TIMEOUT_S,
            ),
            Step(
                "git reset",
                ["git", "-C", directory, "reset", "--hard", "FETCH_HEAD"],
                GIT_SHORT_TIMEOUT_S,
            ),
        ]
    else:
        steps.append(
            Step(
                "git clone",
                _env_no_prompt(
                    "git",
                    *auth,
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    stack.branch,
                    "--",
                    stack.repo_url,
                    directory,
                ),
                GIT_TIMEOUT_S,
            )
        )
    steps += [
        Step("git commit", ["git", "-C", directory, "rev-parse", "HEAD"], GIT_SHORT_TIMEOUT_S),
        Step(
            "compose up",
            [
                "docker",
                "compose",
                "-p",
                stack.name,
                "-f",
                f"{directory}/{stack.compose_file}",
                "up",
                "-d",
                "--remove-orphans",
            ],
            COMPOSE_TIMEOUT_S,
        ),
    ]
    return steps


def stack_from_row(row: Any) -> Stack:
    return Stack(
        id=str(row.id),
        name=row.name,
        node_id=str(row.node_id),
        repo_url=row.repo_url,
        branch=row.branch,
        compose_file=row.compose_file,
        credential_id=str(row.credential_id) if row.credential_id else None,
    )


async def refusal(engine: AsyncEngine, hub: AgentHub, stack: Stack) -> str | None:
    """The reason a deploy cannot start now, or None. Checked before a run is recorded."""
    link = hub.get(stack.node_id)
    if link is None:
        return "agent_offline"
    if link.mux is None:
        return "agent_outdated"
    async with engine.connect() as conn:
        if not await node_exec.policy_allows_exec(conn, stack.node_id):
            return "exec_not_allowed"
    return None


async def _header(engine: AsyncEngine, vault: Vault | None, stack: Stack) -> str | None:
    """The authentication header of the stack's credential, or None for a public repository.
    Raises GitopsError when the credential cannot be read."""
    if stack.credential_id is None:
        return None
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT username, ciphertext, nonce, dek_wrapped FROM credential"
                    " WHERE id = CAST(:id AS uuid) AND kind = 'git_token'"
                ),
                {"id": stack.credential_id},
            )
        ).first()
    if row is None or vault is None:
        raise GitopsError("credential_unavailable")
    try:
        secret = vault.reveal(stack.credential_id, row.ciphertext, row.nonce, row.dek_wrapped)
    except Exception:  # noqa: BLE001 - sealed vault, wrong key, KV down: the run is refused
        raise GitopsError("credential_unavailable") from None
    return auth_header(row.username, str(secret["secret"]))


async def execute(
    engine: AsyncEngine,
    hub: AgentHub,
    run_id: str,
    stack: Stack,
    vault: Vault | None = None,
) -> None:
    """Runs the deploy of an already recorded `running` run and records the outcome."""
    output: list[str] = []
    size = 0
    ran = False
    status = "ok"
    reason: str | None = None
    exit_code: int | None = None
    commit: str | None = None
    try:
        header = await _header(engine, vault, stack)
        probe = await node_exec.run(
            hub,
            stack.node_id,
            ["test", "-d", f"{checkout_dir(stack.id)}/.git"],
            GIT_SHORT_TIMEOUT_S,
        )
        exists = probe.exit_code == 0
        for step in plan(stack, exists, header):
            result = await node_exec.run(hub, stack.node_id, step.argv, step.timeout_s)
            ran = True
            shown = result.output
            if header:  # git may echo the command line in an error: the token never reaches the run
                shown = shown.replace(header, "[redacted]").replace(
                    header.removeprefix("Authorization: Basic "), "[redacted]"
                )
            chunk = f"$ {step.label}\n{shown}"
            if result.truncated:
                chunk += "\n[output truncated]"
            if size < OUTPUT_KEEP:
                output.append(chunk[: OUTPUT_KEEP - size])
            size += len(chunk)
            exit_code = result.exit_code
            if step.label == "git commit" and result.exit_code == 0:
                commit = result.output.strip()[:64] or None
            if result.exit_code != 0:
                status = "failed"
                reason = f"{step.label}: {result.reason}"
                break
    except GitopsError as exc:
        status, reason = "refused", exc.code
    except node_exec.ExecError as exc:
        status = "failed" if ran else "refused"
        reason = exc.code
    except asyncio.CancelledError:
        # shutdown: the run must not stay `running` forever
        status, reason = "failed", "server_stopped"
        await _finish(engine, run_id, stack, status, reason, exit_code, commit, output)
        raise
    except Exception:
        # an unexpected error must not leave the run `running` (it would block later deploys)
        log.exception("gitops deploy failed unexpectedly: stack=%s run=%s", stack.name, run_id)
        status, reason = "failed", "internal_error"
    await _finish(engine, run_id, stack, status, reason, exit_code, commit, output)


async def _finish(
    engine: AsyncEngine,
    run_id: str,
    stack: Stack,
    status: str,
    reason: str | None,
    exit_code: int | None,
    commit: str | None,
    output: list[str],
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE gitops_run SET status = :status, reason = :reason, exit_code = :exit,"
                " commit_sha = :commit, output = :output, finished_at = now()"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {
                "status": status,
                "reason": reason,
                "exit": exit_code,
                "commit": commit,
                "output": "".join(output),
                "id": run_id,
            },
        )
        await conn.execute(
            text(
                "UPDATE gitops_stack SET last_run_at = now(), last_status = :status"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"status": status, "id": stack.id},
        )
        await audit.append(
            conn,
            action="gitops.deploy.finished",
            actor_name="gitops",
            target_type="gitops_stack",
            target_id=stack.name,
            payload={
                "run": run_id,
                "status": status,
                "reason": reason,
                "exit_code": exit_code,
                "commit": commit,
            },
        )
