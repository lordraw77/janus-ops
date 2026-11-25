"""Check engine: runs due checks, keeps soft/hard states, opens and closes problems (FR-30..33).

Single process (assumption A2): an asyncio task in the API process, like the SFTP reaper.

State rules
- Each evaluation gives a raw state. Consecutive non-OK results are counted; the hard state
  becomes the raw state once the count reaches the check's `check_cycles` (hysteresis, AC-06).
  One OK result is enough to recover.
- `recent` keeps the last 21 raw states. Flapping starts when at least half of the last
  transitions changed state, and stops below a quarter (FR-33).
- A check is UNREACHABLE when the node's parent is down (its host check is hard CRIT): the
  problem is still recorded, but not notified (FR-32, AC-07).
- Problems: opened when the hard state leaves OK, updated when it changes (a worse state
  clears the acknowledgement), closed when it returns to OK.
"""

import asyncio
import contextlib
import itertools
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops.agents import exec as node_exec
from janus_ops.agents.hub import AgentHub
from janus_ops.monitoring import fritz, probes, snmp_poll
from janus_ops.monitoring.checks import (
    Check,
    FritzParams,
    LogsParams,
    ProcessParams,
    Result,
    ServiceParams,
    SnmpParams,
    clip,
    evaluate,
    evaluate_probe,
    evaluate_snmp,
    grade_logs,
    grade_process,
    grade_service,
    logs_argv,
    process_argv,
)
from janus_ops.proxmox.access import ProxmoxAccess
from janus_ops.vault import Vault

log = logging.getLogger(__name__)

TICK_S = 5
CONCURRENCY = 20
HISTORY = 21
FLAP_START = 0.5
FLAP_STOP = 0.25
FLAP_MIN = 10  # results needed before flapping is judged
LETTER = {"ok": "o", "warn": "w", "crit": "c", "unknown": "u"}
SEVERITY = {"ok": 0, "warn": 1, "unknown": 1, "crit": 2}


@dataclass(frozen=True)
class Prev:
    state: str
    hard_state: str
    consecutive: int
    recent: str
    is_flapping: bool


@dataclass(frozen=True)
class Next:
    state: str
    state_type: str
    hard_state: str
    consecutive: int
    recent: str
    is_flapping: bool
    state_changed: bool  # raw state differs from the previous one
    hard_changed: bool


def flap_ratio(recent: str) -> float:
    if len(recent) < 2:
        return 0.0
    changes = sum(1 for a, b in itertools.pairwise(recent) if a != b)
    return changes / (len(recent) - 1)


def transition(prev: Prev | None, raw: str, cycles: int) -> Next:
    prev = prev or Prev("ok", "ok", 0, "", False)
    recent = (prev.recent + LETTER[raw])[-HISTORY:]
    consecutive = 0 if raw == "ok" else prev.consecutive + 1
    if raw == "ok":
        hard, kind = "ok", "hard"
    elif consecutive >= cycles:
        hard, kind = raw, "hard"
    else:
        hard, kind = prev.hard_state, "soft"
    flapping = prev.is_flapping
    if len(recent) >= FLAP_MIN:
        ratio = flap_ratio(recent)
        if not flapping and ratio >= FLAP_START:
            flapping = True
        elif flapping and ratio < FLAP_STOP:
            flapping = False
    return Next(
        state=raw,
        state_type=kind,
        hard_state=hard,
        consecutive=min(consecutive, 1_000_000),
        recent=recent,
        is_flapping=flapping,
        state_changed=raw != prev.state,
        hard_changed=hard != prev.hard_state,
    )


DUE = """
SELECT c.id, c.node_id, c.kind, c.params, c.comparison, c.warn_threshold, c.crit_threshold,
       c.check_cycles, n.name AS node_name, n.address, n.parent_id,
       a.last_seen_at, a.enrolled_at,
       s.state, s.hard_state, s.consecutive, s.recent, s.is_flapping
FROM service_check c
JOIN node n ON n.id = c.node_id
LEFT JOIN agent a ON a.node_id = c.node_id
LEFT JOIN check_state s ON s.check_id = c.id
WHERE c.enabled
  AND (s.last_checked_at IS NULL
       OR s.last_checked_at + make_interval(secs => c.interval_s) <= now())
ORDER BY s.last_checked_at NULLS FIRST
LIMIT 500
"""

# nodes whose host check is hard CRIT: their children are unreachable
DOWN_NODES = """
SELECT c.node_id FROM service_check c JOIN check_state s ON s.check_id = c.id
WHERE c.kind = 'host' AND c.enabled AND s.hard_state = 'crit'
"""


class Engine:
    def __init__(
        self,
        engine: AsyncEngine,
        hub: AgentHub,
        proxmox: ProxmoxAccess | None = None,
        vault: Vault | None = None,
    ) -> None:
        self.db = engine
        self.hub = hub
        self.proxmox = proxmox  # for the `backup` kind
        self.vault = vault  # for the `snmp` kind: the community or the SNMPv3 keys
        self.wake = asyncio.Event()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("check engine tick failed")
            self.wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), TICK_S)

    async def tick(self) -> int:
        async with self.db.connect() as conn:
            rows = list(await conn.execute(text(DUE)))
            down = {str(r.node_id) for r in await conn.execute(text(DOWN_NODES))}
        if not rows:
            return 0
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(row: Any) -> None:
            async with sem:
                await self._run_one(row, down)

        await asyncio.gather(*(one(r) for r in rows))
        return len(rows)

    async def _run_one(self, row: Any, down: set[str]) -> None:
        check = Check(
            id=str(row.id),
            node_id=str(row.node_id),
            node_name=row.node_name,
            address=row.address,
            kind=row.kind,
            params=row.params,
            comparison=row.comparison,
            warn=row.warn_threshold,
            crit=row.crit_threshold,
        )
        online = self.hub.get(check.node_id) is not None
        if check.kind == "service":
            result = await self._service(check, online)
        elif check.kind in ("process", "logs"):
            result = await self._exec_check(check, online)
        elif check.kind in ("metric", "systemd", "port") and not online:
            # no fresh data while the agent is away: the host check reports it
            result = Result("unknown", "Agent not connected")
        elif check.kind == "snmp":
            result = await self._snmp(check)
        elif check.kind == "fritzbox":
            result = await self._fritzbox(check)
        elif check.kind == "probe":
            result = await evaluate_probe(probes.Ctx(self.db, self.vault, self.proxmox), check)
        else:
            async with self.db.connect() as conn:
                result = await evaluate(conn, check, online, row.last_seen_at, self.proxmox)
        unreachable = row.parent_id is not None and str(row.parent_id) in down
        prev = (
            Prev(row.state, row.hard_state, row.consecutive, row.recent, row.is_flapping)
            if row.state is not None
            else None
        )
        await record(self.db, check.id, row.check_cycles, prev, result, unreachable)

    async def _service(self, check: Check, online: bool) -> Result:
        """`systemctl is-active <unit>` through the agent (agent connected, exec allowed)."""
        if not online:
            return Result("unknown", "Agent not connected")
        unit = ServiceParams.model_validate(check.params).unit
        async with self.db.connect() as conn:
            allowed = await node_exec.policy_allows_exec(conn, check.node_id)
        if not allowed:
            return Result("unknown", "exec is not allowed on this node (agent policy allow_exec)")
        try:
            result = await node_exec.run(
                self.hub, check.node_id, ["systemctl", "is-active", unit], 15
            )
        except node_exec.ExecError as exc:
            return Result("crit", f"{unit}: the command did not run ({exc.code})")
        return grade_service(unit, result.output)

    async def _exec_check(self, check: Check, online: bool) -> Result:
        """`process` and `logs`: a fixed read-only command (pgrep, journalctl, tail) through the
        agent; the server reads the answer. Needs the agent connected and exec allowed."""
        if not online:
            return Result("unknown", "Agent not connected")
        async with self.db.connect() as conn:
            allowed = await node_exec.policy_allows_exec(conn, check.node_id)
        if not allowed:
            return Result("unknown", "exec is not allowed on this node (agent policy allow_exec)")
        if check.kind == "process":
            argv = process_argv(ProcessParams.model_validate(check.params))
        else:
            argv = logs_argv(LogsParams.model_validate(check.params))
        try:
            res = await node_exec.run(self.hub, check.node_id, argv, 30)
        except node_exec.ExecError as exc:
            return Result("unknown", f"the command did not run ({exc.code})")
        if check.kind == "process":
            return grade_process(check, res.exit_code, res.output)
        return grade_logs(check, res.exit_code, res.output, res.truncated)

    async def _fritzbox(self, check: Check) -> Result:
        """One item of the FRITZ!Box reading, polled over TR-064 from the server (no agent)."""
        try:
            async with self.db.connect() as conn:
                target = await fritz.load_target(conn, self.vault, check.node_id)
            data = await fritz.poll(target)
        except fritz.FritzError as exc:
            return fritz.error_result(exc.code)
        return fritz.grade(FritzParams.model_validate(check.params).item, data)

    async def _snmp(self, check: Check) -> Result:
        """One SNMP GET from the server; the credentials are read from the vault here."""
        try:
            async with self.db.connect() as conn:
                target = await snmp_poll.load_target(conn, self.vault, check.node_id)
        except snmp_poll.SnmpError as exc:
            return evaluate_snmp(check, snmp_poll.Reading("", None, error=exc.code))
        p = SnmpParams.model_validate(check.params)
        reading = await snmp_poll.get(target, p.oid, p.timeout_s)
        return evaluate_snmp(check, reading)


async def record(
    db: AsyncEngine,
    check_id: str,
    cycles: int,
    prev: Prev | None,
    result: Result,
    unreachable: bool,
) -> Next:
    """Store one result: state, history and problem, in one transaction."""
    nxt = transition(prev, result.state, cycles)
    output = clip(result.output)
    now = datetime.now(UTC)
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO check_state (check_id, state, state_type, hard_state, consecutive,"
                " since, last_checked_at, last_output, is_flapping, recent, unreachable)"
                " VALUES (:id, :state, :type, :hard, :n, :now, :now, :out, :flap, :recent, :unr)"
                " ON CONFLICT (check_id) DO UPDATE SET state = EXCLUDED.state,"
                " state_type = EXCLUDED.state_type, hard_state = EXCLUDED.hard_state,"
                " consecutive = EXCLUDED.consecutive,"
                " since = CASE WHEN check_state.state = EXCLUDED.state"
                "   THEN check_state.since ELSE EXCLUDED.since END,"
                " last_checked_at = EXCLUDED.last_checked_at, last_output = EXCLUDED.last_output,"
                " is_flapping = EXCLUDED.is_flapping, recent = EXCLUDED.recent,"
                " unreachable = EXCLUDED.unreachable"
            ),
            {
                "id": check_id,
                "state": nxt.state,
                "type": nxt.state_type,
                "hard": nxt.hard_state,
                "n": nxt.consecutive,
                "now": now,
                "out": output,
                "flap": nxt.is_flapping,
                "recent": nxt.recent,
                "unr": unreachable,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO check_result (time, check_id, state, output, perf)"
                " VALUES (:now, :id, :state, :out, CAST(:perf AS jsonb))"
            ),
            {
                "now": now,
                "id": check_id,
                "state": result.state,
                "out": output,
                "perf": json.dumps(result.perf),
            },
        )
        if nxt.hard_state == "ok":
            await conn.execute(
                text(
                    "UPDATE problem SET closed_at = :now WHERE check_id = :id AND closed_at IS NULL"
                ),
                {"now": now, "id": check_id},
            )
        else:
            current = (
                await conn.execute(
                    text(
                        "SELECT id, state FROM problem WHERE check_id = :id AND closed_at IS NULL"
                    ),
                    {"id": check_id},
                )
            ).first()
            if current is None:
                await conn.execute(
                    text("INSERT INTO problem (check_id, state, opened_at) VALUES (:id, :s, :now)"),
                    {"id": check_id, "s": nxt.hard_state, "now": now},
                )
            elif current.state != nxt.hard_state:
                worse = SEVERITY[nxt.hard_state] > SEVERITY[current.state]
                # a worse state clears the acknowledgement: someone must look again
                await conn.execute(
                    text(
                        "UPDATE problem SET state = :s,"
                        " acknowledged_by = CASE WHEN :worse THEN NULL ELSE acknowledged_by END,"
                        " acknowledged_at = CASE WHEN :worse THEN NULL ELSE acknowledged_at END,"
                        " ack_comment = CASE WHEN :worse THEN NULL ELSE ack_comment END"
                        " WHERE id = :pid"
                    ),
                    {"s": nxt.hard_state, "pid": current.id, "worse": worse},
                )
    return nxt
