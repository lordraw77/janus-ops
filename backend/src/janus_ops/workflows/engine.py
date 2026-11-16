"""Workflow engine (F8, FR-80): runs steps in order, waits for approvals, resumes, and triggers.

State lives in the database, so a run can wait for days. Each step has one row per run; a step
whose row is `done` is never executed again, which makes a resume safe. A run is advanced by one
background task at a time: the transition into `running` is a conditional update, so two
approvals or a scheduler tick cannot start the same run twice.

Triggers: manual (the API), interval (the scheduler, every SCHEDULER_TICK_S) and alert (the
scheduler looks for problems opened since its last pass, so an alert starts a run within one tick).
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops import audit
from janus_ops.agents import exec as node_exec
from janus_ops.agents.hub import AgentHub
from janus_ops.monitoring import notify
from janus_ops.vault import Vault
from janus_ops.workflows.model import needs_approval

log = logging.getLogger(__name__)

SCHEDULER_TICK_S = 30
OUTPUT_KEEP = 64 * 1024


async def _set_run(engine: AsyncEngine, run_id: str, status: str, error: str | None = None) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE workflow_run SET status = :s, error = coalesce(:e, error),"
                " finished_at = CASE WHEN :s IN ('done', 'failed', 'cancelled')"
                " THEN now() ELSE finished_at END WHERE id = CAST(:id AS uuid)"
            ),
            {"s": status, "e": error, "id": run_id},
        )


async def start_run(
    engine: AsyncEngine,
    hub: AgentHub,
    jobs: set[asyncio.Task[None]],
    workflow_id: str,
    name: str,
    trigger: str,
    actor: str,
    context: dict[str, Any],
    vault: Vault | None = None,
) -> str:
    """Records a run and advances it in the background. Returns the run id."""
    async with engine.begin() as conn:
        run_id = str(
            await conn.scalar(
                text(
                    "INSERT INTO workflow_run (workflow_id, trigger, status, context, requested_by)"
                    " VALUES (CAST(:w AS uuid), :t, 'running', CAST(:c AS jsonb), :by)"
                    " RETURNING id"
                ),
                {"w": workflow_id, "t": trigger, "c": _json(context), "by": actor},
            )
        )
        await audit.append(
            conn,
            action="workflow.run.started",
            actor_name=actor,
            target_type="workflow",
            target_id=name,
            payload={"run": run_id, "trigger": trigger},
        )
    _spawn(jobs, advance(engine, hub, run_id, vault))
    return run_id


def _spawn(jobs: set[asyncio.Task[None]], coro: Any) -> None:
    task = asyncio.create_task(coro)
    jobs.add(task)
    task.add_done_callback(jobs.discard)


def _json(value: Any) -> str:
    return json.dumps(value, default=str)


async def advance(
    engine: AsyncEngine, hub: AgentHub, run_id: str, vault: Vault | None = None
) -> None:
    """Runs the next steps of a run until it is done, failed, cancelled, or waiting for a person."""
    try:
        while True:
            run = await _load(engine, run_id)
            if run is None or run.status != "running":
                return
            steps: list[dict[str, Any]] = run.definition["steps"]
            async with engine.begin() as conn:
                finished = {
                    r.step_id: r.status
                    for r in await conn.execute(
                        text(
                            "SELECT step_id, status FROM workflow_step_run"
                            " WHERE run_id = CAST(:r AS uuid)"
                        ),
                        {"r": run_id},
                    )
                }
            step = next((s for s in steps if finished.get(s["id"]) != "done"), None)
            if step is None:
                await _set_run(engine, run_id, "done")
                return
            await _ensure_step(engine, run_id, step, steps.index(step))
            if needs_approval(step):
                state = await _approval_state(engine, run_id, step["id"])
                if state is None:
                    await _request_approval(engine, run, step)
                    return
                if state.status == "pending":
                    await _wait(engine, run_id, step["id"])
                    return
                if state.status == "rejected":
                    await _step_failed(engine, run_id, step["id"], "rejected")
                    await _set_run(engine, run_id, "failed", f"{step['id']}: rejected")
                    return
            outcome = await _execute(engine, hub, run, step, vault)
            await _step_finished(engine, run_id, step["id"], outcome)
            if not outcome["ok"]:
                await _set_run(engine, run_id, "failed", f"{step['id']}: {outcome['error']}")
                await _audit_run(engine, run, "workflow.run.failed", {"step": step["id"]})
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("workflow run failed unexpectedly: run=%s", run_id)
        await _set_run(engine, run_id, "failed", "internal_error")


class _Run:
    def __init__(self, row: Any) -> None:
        self.id = str(row.id)
        self.status: str = row.status
        self.workflow_id = str(row.workflow_id)
        self.name: str = row.name
        self.definition: dict[str, Any] = row.definition
        self.context: dict[str, Any] = row.context or {}
        self.requested_by: str = row.requested_by


async def _load(engine: AsyncEngine, run_id: str) -> _Run | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT r.id, r.status, r.workflow_id, r.context, r.requested_by, w.name,"
                    " w.definition FROM workflow_run r JOIN workflow w ON w.id = r.workflow_id"
                    " WHERE r.id = CAST(:id AS uuid)"
                ),
                {"id": run_id},
            )
        ).first()
    return _Run(row) if row is not None else None


async def _ensure_step(
    engine: AsyncEngine, run_id: str, step: dict[str, Any], position: int
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO workflow_step_run (run_id, step_id, position, action, status)"
                " VALUES (CAST(:r AS uuid), :s, :p, :a, 'pending') ON CONFLICT DO NOTHING"
            ),
            {"r": run_id, "s": step["id"], "p": position, "a": step["action"]},
        )


class _Approval:
    def __init__(self, status: str, decided_by: str | None) -> None:
        self.status = status
        self.decided_by = decided_by


async def _approval_state(engine: AsyncEngine, run_id: str, step_id: str) -> _Approval | None:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT status, decided_by FROM workflow_approval"
                    " WHERE run_id = CAST(:r AS uuid) AND step_id = :s"
                ),
                {"r": run_id, "s": step_id},
            )
        ).first()
    return _Approval(row.status, row.decided_by) if row is not None else None


def _reason(step: dict[str, Any]) -> str:
    if step["action"] == "approval":
        return str(step["params"].get("reason", ""))
    return f"exec on {step['params']['node_id']}: {step['params']['command']}"[:500]


async def _request_approval(engine: AsyncEngine, run: _Run, step: dict[str, Any]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO workflow_approval (run_id, step_id, reason, status)"
                " VALUES (CAST(:r AS uuid), :s, :reason, 'pending') ON CONFLICT DO NOTHING"
            ),
            {"r": run.id, "s": step["id"], "reason": _reason(step)},
        )
        await audit.append(
            conn,
            action="workflow.approval.requested",
            actor_name="workflow",
            target_type="workflow",
            target_id=run.name,
            payload={"run": run.id, "step": step["id"]},
        )
    await _wait(engine, run.id, step["id"])


async def _wait(engine: AsyncEngine, run_id: str, step_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE workflow_step_run SET status = 'waiting_approval'"
                " WHERE run_id = CAST(:r AS uuid) AND step_id = :s"
            ),
            {"r": run_id, "s": step_id},
        )
        await conn.execute(
            text(
                "UPDATE workflow_run SET status = 'waiting_approval'"
                " WHERE id = CAST(:r AS uuid) AND status = 'running'"
            ),
            {"r": run_id},
        )


async def _execute(
    engine: AsyncEngine, hub: AgentHub, run: _Run, step: dict[str, Any], vault: Vault | None
) -> dict[str, Any]:
    """Runs one step and says how it went: ok, the output, the exit code and an error code."""
    params = step["params"]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE workflow_step_run SET status = 'running', started_at = now()"
                " WHERE run_id = CAST(:r AS uuid) AND step_id = :s"
            ),
            {"r": run.id, "s": step["id"]},
        )
    if step["action"] == "approval":
        state = await _approval_state(engine, run.id, step["id"])
        by = state.decided_by if state else "?"
        return {"ok": True, "output": f"approved by {by}", "exit_code": None, "error": None}
    if step["action"] == "notify":
        return await _notify(engine, run, step, vault)
    # exec
    async with engine.connect() as conn:
        if not await node_exec.policy_allows_exec(conn, str(params["node_id"])):
            return {"ok": False, "output": "", "exit_code": None, "error": "exec_not_allowed"}
    try:
        argv = node_exec.argv_from_command(params["command"])
        result = await node_exec.run(hub, str(params["node_id"]), argv, params["timeout_s"])
    except node_exec.ExecError as exc:
        return {"ok": False, "output": "", "exit_code": None, "error": exc.code}
    return {
        "ok": result.exit_code == 0,
        "output": result.output[:OUTPUT_KEEP],
        "exit_code": result.exit_code,
        "error": None if result.exit_code == 0 else f"exit {result.exit_code}",
    }


async def _notify(
    engine: AsyncEngine, run: _Run, step: dict[str, Any], vault: Vault | None
) -> dict[str, Any]:
    """Sends the step's message to its channel. A missing or disabled channel fails the step."""
    params = step["params"]
    if not params.get("channel_id"):
        # a definition saved before notify steps named a channel: the step fails, with a reason
        return {"ok": False, "output": "", "exit_code": None, "error": "channel_not_set"}
    channel_id = str(params["channel_id"])
    async with engine.connect() as conn:
        channel = (
            await conn.execute(
                text(
                    "SELECT id, kind, config, enabled, secret_ciphertext, secret_nonce, secret_dek"
                    " FROM notification_channel WHERE id = CAST(:id AS uuid)"
                ),
                {"id": channel_id},
            )
        ).first()
    if channel is None:
        return {"ok": False, "output": "", "exit_code": None, "error": "channel_not_found"}
    if not channel.enabled:
        return {"ok": False, "output": "", "exit_code": None, "error": "channel_disabled"}
    if vault is None:
        return {"ok": False, "output": "", "exit_code": None, "error": "vault_unavailable"}
    message = notify.Message(
        event="workflow",
        state="ok",
        node=run.name,
        check=step["id"],
        output=params["message"],
        problem_id=run.id,
    )
    try:
        await notify.deliver(vault, channel, message, channel_id)
    except (notify.SendError, OSError, KeyError, ValueError) as exc:
        return {
            "ok": False,
            "output": "",
            "exit_code": None,
            "error": f"delivery_failed: {type(exc).__name__}",
        }
    async with engine.begin() as conn:
        await audit.append(
            conn,
            action="workflow.notify",
            actor_name="workflow",
            target_type="workflow",
            target_id=run.name,
            payload={"run": run.id, "step": step["id"], "channel": channel_id},
        )
    return {"ok": True, "output": params["message"], "exit_code": None, "error": None}


async def _step_finished(
    engine: AsyncEngine, run_id: str, step_id: str, outcome: dict[str, Any]
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE workflow_step_run SET status = :st, output = :o, exit_code = :x,"
                " finished_at = now() WHERE run_id = CAST(:r AS uuid) AND step_id = :s"
            ),
            {
                "st": "done" if outcome["ok"] else "failed",
                "o": outcome["output"],
                "x": outcome["exit_code"],
                "r": run_id,
                "s": step_id,
            },
        )


async def _step_failed(engine: AsyncEngine, run_id: str, step_id: str, why: str) -> None:
    await _step_finished(
        engine, run_id, step_id, {"ok": False, "output": why, "exit_code": None, "error": why}
    )


async def _audit_run(engine: AsyncEngine, run: _Run, action: str, payload: dict[str, Any]) -> None:
    async with engine.begin() as conn:
        await audit.append(
            conn,
            action=action,
            actor_name="workflow",
            target_type="workflow",
            target_id=run.name,
            payload={"run": run.id, **payload},
        )


async def decide(
    engine: AsyncEngine,
    hub: AgentHub,
    jobs: set[asyncio.Task[None]],
    run_id: str,
    step_id: str,
    approve: bool,
    actor: str,
    reason: str,
    vault: Vault | None = None,
) -> str:
    """Approves or rejects the pending approval of a step. Returns the new status of the approval.

    Raises LookupError when there is no pending approval for that step."""
    status = "approved" if approve else "rejected"
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "UPDATE workflow_approval SET status = :st, decided_by = :by,"
                    " decided_at = now(),"
                    " reason = CASE WHEN :why = '' THEN reason ELSE :why END"
                    " WHERE run_id = CAST(:r AS uuid) AND step_id = :s AND status = 'pending'"
                    " RETURNING run_id"
                ),
                {"st": status, "by": actor, "why": reason[:500], "r": run_id, "s": step_id},
            )
        ).first()
        if row is None:
            raise LookupError("no pending approval for this step")
        await audit.append(
            conn,
            action=f"workflow.approval.{status}",
            actor_name=actor,
            target_type="workflow_run",
            target_id=run_id,
            payload={"step": step_id, "reason": reason[:500]},
        )
    if not approve:
        await _step_failed(engine, run_id, step_id, "rejected")
        await _set_run(engine, run_id, "failed", f"{step_id}: rejected")
        return status
    async with engine.begin() as conn:
        resumed = (
            await conn.execute(
                text(
                    "UPDATE workflow_run SET status = 'running'"
                    " WHERE id = CAST(:r AS uuid) AND status = 'waiting_approval' RETURNING id"
                ),
                {"r": run_id},
            )
        ).first()
    if resumed is not None:
        _spawn(jobs, advance(engine, hub, run_id, vault))
    return status


async def cancel(engine: AsyncEngine, run_id: str, actor: str) -> bool:
    """Stops a run that is not finished. A command already running on a node is not interrupted."""
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "UPDATE workflow_run SET status = 'cancelled', finished_at = now()"
                    " WHERE id = CAST(:r AS uuid) AND status IN ('running', 'waiting_approval')"
                    " RETURNING workflow_id"
                ),
                {"r": run_id},
            )
        ).first()
        if row is None:
            return False
        await conn.execute(
            text(
                "UPDATE workflow_approval SET status = 'rejected', decided_by = :by,"
                " decided_at = now() WHERE run_id = CAST(:r AS uuid) AND status = 'pending'"
            ),
            {"r": run_id, "by": actor},
        )
        await audit.append(
            conn,
            action="workflow.run.cancelled",
            actor_name=actor,
            target_type="workflow_run",
            target_id=run_id,
        )
    return True


async def recover(engine: AsyncEngine) -> int:
    """At startup, a run that was running is marked failed: its step may have half happened, and a
    person decides whether to start it again. Returns how many runs were marked."""
    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "UPDATE workflow_run SET status = 'failed', error = 'server_restarted',"
                " finished_at = now() WHERE status = 'running' RETURNING id"
            )
        )
        return len(list(rows))


async def tick(
    engine: AsyncEngine,
    hub: AgentHub,
    jobs: set[asyncio.Task[None]],
    cursor: datetime,
    vault: Vault | None = None,
) -> tuple[int, datetime]:
    """One scheduler pass: interval workflows that are due, and alerts opened since `cursor`.
    Returns the number of runs started and the cursor for the next pass."""
    started = 0
    async with engine.connect() as conn:
        due = list(
            await conn.execute(
                text(
                    "SELECT id, name FROM workflow WHERE enabled"
                    " AND definition->'trigger'->>'type' = 'interval'"
                    " AND (last_fired_at IS NULL OR last_fired_at"
                    "  + make_interval(secs => (definition->'trigger'->>'interval_s')::int)"
                    " <= now())"
                )
            )
        )
    for wf in due:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE workflow SET last_fired_at = now() WHERE id = CAST(:id AS uuid)"),
                {"id": str(wf.id)},
            )
        await start_run(engine, hub, jobs, str(wf.id), wf.name, "interval", "scheduler", {}, vault)
        started += 1
    async with engine.connect() as conn:
        problems = list(
            await conn.execute(
                text(
                    "SELECT p.id, p.check_id, p.opened_at FROM problem p"
                    " WHERE p.opened_at > :c ORDER BY p.opened_at LIMIT 200"
                ),
                {"c": cursor},
            )
        )
    next_cursor = cursor
    for problem in problems:
        next_cursor = max(next_cursor, problem.opened_at)
        async with engine.connect() as conn:
            hits = list(
                await conn.execute(
                    text(
                        "SELECT id, name FROM workflow WHERE enabled"
                        " AND definition->'trigger'->>'type' = 'alert'"
                        " AND (definition->'trigger'->>'check_id' IS NULL"
                        "  OR definition->'trigger'->>'check_id' = CAST(:c AS text))"
                    ),
                    {"c": str(problem.check_id)},
                )
            )
        for wf in hits:
            await start_run(
                engine,
                hub,
                jobs,
                str(wf.id),
                wf.name,
                "alert",
                "scheduler",
                {"problem_id": str(problem.id), "check_id": str(problem.check_id)},
                vault,
            )
            started += 1
    return started, next_cursor


async def scheduler(
    engine: AsyncEngine, hub: AgentHub, jobs: set[asyncio.Task[None]], vault: Vault | None = None
) -> None:
    """Background loop of the app: starts the interval and alert runs. Stopped with the app."""
    cursor = datetime.now(UTC)
    while True:
        try:
            _, cursor = await tick(engine, hub, jobs, cursor, vault)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("workflow scheduler pass failed")
        await asyncio.sleep(SCHEDULER_TICK_S)
