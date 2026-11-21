"""Workflows (FR-80) over the API and the database: permissions, runs with notify and exec steps,
approvals (approve, reject), the no-repeat rule after a wait, and the alert and interval triggers.
The agent is not running, so an exec step is refused as exec_not_allowed, which is the point of
the test: the approval gate comes first."""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops.agents.hub import AgentHub
from janus_ops.db import alembic_config
from janus_ops.monitoring import notify
from janus_ops.vault import Vault
from janus_ops.workflows import engine as wf_engine

from ._it import CONFIGURED, DB_URL, make_client, run_sql, signed_in

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


PLACEHOLDER_CHANNEL = str(uuid.UUID(int=0))


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _plain_node(client: Any) -> str:
    r = client.post(
        "/api/v1/nodes",
        json={"name": _name("wf-node"), "address": "127.0.0.30", "environment": "lab"},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


@pytest.fixture(scope="module")
def channel() -> Iterator[str]:
    """A notification channel for the notify steps. Its sender is replaced by a recorder below,
    so no message leaves the test."""
    cid = str(uuid.uuid4())
    run_sql(
        "INSERT INTO notification_channel (id, name, kind, config, enabled)"
        " VALUES (CAST(:id AS uuid), :name, 'webhook', CAST(:c AS jsonb), true)",
        {"id": cid, "name": _name("wf-channel"), "c": '{"url": "https://example.invalid/hook"}'},
    )
    yield cid


@pytest.fixture(autouse=True)
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    sent: list[Any] = []

    async def fake_deliver(vault: Any, channel_row: Any, message: Any, channel_id: str) -> None:
        sent.append((channel_id, message.title, message.output))

    monkeypatch.setattr(notify, "deliver", fake_deliver)
    return sent


def _notify(step_id: str, channel_id: str, message: str = "hello") -> dict[str, Any]:
    return {
        "id": step_id,
        "action": "notify",
        "params": {"channel_id": channel_id, "message": message},
    }


def _exec(step_id: str, node: str, command_line: str, read_only: bool = False) -> dict[str, Any]:
    params: dict[str, Any] = {"node_id": node, "command": command_line}
    if read_only:
        params["read_only"] = True
    return {"id": step_id, "action": "exec", "params": params}


def _workflow(
    client: Any, steps: list[dict[str, Any]], trigger: dict[str, Any] | None = None
) -> dict[str, Any]:
    body = {
        "name": _name("wf"),
        "definition": {"trigger": trigger or {"type": "manual"}, "steps": steps},
    }
    r = client.post("/api/v1/workflows", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _run(client: Any, workflow_id: str) -> str:
    r = client.post(f"/api/v1/workflows/{workflow_id}/run")
    assert r.status_code == 202, r.text
    return str(r.json()["run_id"])


def _wait_until(client: Any, run_id: str, statuses: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 15
    while True:
        run = client.get(f"/api/v1/workflows/runs/{run_id}").json()
        if run["status"] in statuses or time.monotonic() > deadline:
            return run
        time.sleep(0.2)


def _notify_count(run_id: str) -> int:
    return int(
        run_sql(
            "SELECT count(*) FROM audit_event WHERE action = 'workflow.notify'"
            " AND payload->>'run' = :r",
            {"r": run_id},
        )[0][0]
    )


def test_an_auditor_cannot_manage_workflows_and_an_operator_cannot_create_them() -> None:
    with signed_in("carol") as auditor:
        assert auditor.get("/api/v1/workflows").status_code == 200
        body = {
            "name": _name("x"),
            "definition": {
                "trigger": {"type": "manual"},
                "steps": [_notify("a", PLACEHOLDER_CHANNEL)],
            },
        }
        assert auditor.post("/api/v1/workflows", json=body).status_code == 403
    with signed_in("bob") as operator:
        assert operator.post("/api/v1/workflows", json=body).status_code == 403


@pytest.mark.parametrize(
    "definition",
    [
        {"trigger": {"type": "manual"}, "steps": []},
        {
            "trigger": {"type": "manual"},
            "steps": [_notify("a", PLACEHOLDER_CHANNEL), _notify("a", PLACEHOLDER_CHANNEL)],
        },
        {
            "trigger": {"type": "interval", "interval_s": 10},
            "steps": [_notify("a", PLACEHOLDER_CHANNEL)],
        },
        {
            "trigger": {"type": "manual"},
            "steps": [{"id": "a", "action": "exec", "params": {"command": "uptime"}}],
        },
    ],
)
def test_invalid_definitions_are_refused(definition: dict[str, Any]) -> None:
    with signed_in("alice") as admin:
        r = admin.post("/api/v1/workflows", json={"name": _name("bad"), "definition": definition})
    assert r.status_code == 422


def test_a_notify_only_workflow_runs_to_done(channel: str) -> None:
    with signed_in("alice") as admin:
        wf = _workflow(
            admin, [_notify("hello", channel, "first"), _notify("bye", channel, "second")]
        )
        run_id = _run(admin, wf["id"])
        run = _wait_until(admin, run_id, {"done", "failed"})
    assert run["status"] == "done"
    assert [s["status"] for s in run["steps"]] == ["done", "done"]
    assert run["steps"][0]["output"] == "first"
    assert _notify_count(run_id) == 2


def test_a_destructive_exec_waits_for_approval_and_then_runs() -> None:
    with signed_in("alice") as admin:
        node = _plain_node(admin)
        wf = _workflow(admin, [_exec("clean", node, "rm -rf /tmp/janus-test")])
        run_id = _run(admin, wf["id"])
        waiting = _wait_until(admin, run_id, {"waiting_approval", "done", "failed"})
    assert waiting["status"] == "waiting_approval"
    assert waiting["steps"][0]["status"] == "waiting_approval"
    assert waiting["approvals"][0]["status"] == "pending"
    with signed_in("bob") as operator:
        r = operator.post(
            f"/api/v1/workflows/runs/{run_id}/steps/clean/approve", json={"reason": "ok"}
        )
        assert r.status_code == 200 and r.json()["status"] == "approved"
        run = _wait_until(operator, run_id, {"done", "failed"})
    # the agent is not running here: the approved command is refused, and the run says why
    assert run["status"] == "failed"
    assert run["error"] == "clean: exec_not_allowed"
    assert run["approvals"][0]["decided_by"] == "bob"


def test_a_rejected_step_fails_the_run_without_running_anything() -> None:
    with signed_in("alice") as admin:
        node = _plain_node(admin)
        wf = _workflow(admin, [_exec("drop", node, "rm -rf /tmp/janus-test")])
        run_id = _run(admin, wf["id"])
        _wait_until(admin, run_id, {"waiting_approval"})
        r = admin.post(f"/api/v1/workflows/runs/{run_id}/steps/drop/reject", json={"reason": "no"})
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        run = _wait_until(admin, run_id, {"done", "failed"})
    assert run["status"] == "failed" and run["error"] == "drop: rejected"
    assert run["steps"][0]["status"] == "failed"


def test_a_read_only_exec_needs_no_approval() -> None:
    with signed_in("alice") as admin:
        node = _plain_node(admin)
        wf = _workflow(admin, [_exec("look", node, "uptime", read_only=True)])
        run_id = _run(admin, wf["id"])
        run = _wait_until(admin, run_id, {"done", "failed"})
    assert run["status"] == "failed" and run["error"] == "look: exec_not_allowed"
    assert run["approvals"] == []


def test_steps_that_are_done_are_not_repeated_after_an_approval(channel: str) -> None:
    with signed_in("alice") as admin:
        node = _plain_node(admin)
        wf = _workflow(
            admin,
            [
                _notify("before", channel, "one"),
                _exec("gate", node, "reboot"),
                _notify("after", channel, "two"),
            ],
        )
        run_id = _run(admin, wf["id"])
        _wait_until(admin, run_id, {"waiting_approval"})
        assert _notify_count(run_id) == 1  # the step before the gate ran once
        admin.post(f"/api/v1/workflows/runs/{run_id}/steps/gate/approve", json={})
        run = _wait_until(admin, run_id, {"done", "failed"})
    # the gate's command is refused (no agent): the run ends failed, the last notify never runs
    assert run["status"] == "failed"
    assert _notify_count(run_id) == 1


def test_only_an_approver_can_decide() -> None:
    with signed_in("alice") as admin:
        node = _plain_node(admin)
        wf = _workflow(admin, [_exec("gate", node, "reboot")])
        run_id = _run(admin, wf["id"])
        _wait_until(admin, run_id, {"waiting_approval"})
    with signed_in("carol") as auditor:
        r = auditor.post(f"/api/v1/workflows/runs/{run_id}/steps/gate/approve", json={})
    assert r.status_code == 403


def test_a_deciding_twice_is_refused() -> None:
    with signed_in("alice") as admin:
        node = _plain_node(admin)
        wf = _workflow(admin, [_exec("gate", node, "reboot")])
        run_id = _run(admin, wf["id"])
        _wait_until(admin, run_id, {"waiting_approval"})
        assert (
            admin.post(f"/api/v1/workflows/runs/{run_id}/steps/gate/approve", json={}).status_code
            == 200
        )
        again = admin.post(f"/api/v1/workflows/runs/{run_id}/steps/gate/approve", json={})
    assert again.status_code == 409 and again.json()["detail"] == "no_pending_approval"


def test_a_cancelled_run_stops_and_cannot_be_cancelled_twice() -> None:
    with signed_in("alice") as admin:
        node = _plain_node(admin)
        wf = _workflow(admin, [_exec("gate", node, "reboot")])
        run_id = _run(admin, wf["id"])
        _wait_until(admin, run_id, {"waiting_approval"})
        assert admin.post(f"/api/v1/workflows/runs/{run_id}/cancel").json()["status"] == "cancelled"
        again = admin.post(f"/api/v1/workflows/runs/{run_id}/cancel")
    assert again.status_code == 409


def test_the_alert_and_interval_triggers_start_runs_on_a_scheduler_pass(channel: str) -> None:
    node = str(uuid.uuid4())
    check = str(uuid.uuid4())
    run_sql(
        "INSERT INTO node (id, name, address) VALUES (CAST(:n AS uuid), :name, '127.0.0.40')",
        {"n": node, "name": _name("sched-node")},
    )
    run_sql(
        "INSERT INTO service_check (id, node_id, name, kind) VALUES (CAST(:c AS uuid),"
        " CAST(:n AS uuid), 'sched-check', 'tcp')",
        {"c": check, "n": node},
    )
    opened = datetime.now(UTC)
    cursor = opened - timedelta(minutes=5)
    run_sql(
        "INSERT INTO problem (check_id, state, opened_at) VALUES (CAST(:c AS uuid), 'crit', :t)",
        {"c": check, "t": opened},
    )
    with signed_in("alice") as admin:
        alert_wf = _workflow(
            admin, [_notify("a", channel, "alert")], {"type": "alert", "check_id": check}
        )
        interval_wf = _workflow(
            admin, [_notify("i", channel, "tick")], {"type": "interval", "interval_s": 60}
        )

    async def one_pass() -> int:
        db = create_async_engine(DB_URL)
        try:
            started, _ = await wf_engine.tick(db, AgentHub(), set(), cursor)
            return started
        finally:
            await db.dispose()

    started = asyncio.run(one_pass())
    assert started >= 2
    triggers = {
        r[0]
        for r in run_sql(
            "SELECT trigger FROM workflow_run"
            " WHERE workflow_id IN (CAST(:a AS uuid), CAST(:b AS uuid))",
            {"a": alert_wf["id"], "b": interval_wf["id"]},
        )
    }
    assert triggers == {"alert", "interval"}


def test_a_notify_step_is_delivered_to_its_channel(channel: str, recorded: list[Any]) -> None:
    with signed_in("alice") as admin:
        wf = _workflow(admin, [_notify("hello", channel, "first")])
        run = _wait_until(admin, _run(admin, wf["id"]), {"done", "failed"})
    assert run["status"] == "done"
    sent = [item for item in recorded if item[2] == "first"]
    assert sent and sent[-1][0] == channel
    assert sent[-1][1] == f"[WORKFLOW] {wf['name']} / hello"


def test_a_missing_channel_fails_the_step(channel: str) -> None:
    gone = str(uuid.uuid4())
    with signed_in("alice") as admin:
        wf = _workflow(admin, [_notify("lost", gone)])
        run = _wait_until(admin, _run(admin, wf["id"]), {"done", "failed"})
    assert run["status"] == "failed"
    assert run["error"] == "lost: channel_not_found"


def test_a_disabled_channel_fails_the_step(channel: str) -> None:
    off = str(uuid.uuid4())
    run_sql(
        "INSERT INTO notification_channel (id, name, kind, config, enabled)"
        " VALUES (CAST(:id AS uuid), :name, 'webhook', CAST(:c AS jsonb), false)",
        {"id": off, "name": _name("off-channel"), "c": '{"url": "https://example.invalid/hook"}'},
    )
    with signed_in("alice") as admin:
        wf = _workflow(admin, [_notify("muted", off)])
        run = _wait_until(admin, _run(admin, wf["id"]), {"done", "failed"})
    assert run["status"] == "failed"
    assert run["error"] == "muted: channel_disabled"


def test_a_notify_step_without_a_channel_fails_with_a_reason(channel: str) -> None:
    # a definition saved before notify steps named a channel must not crash the run
    with signed_in("alice") as admin:
        wf = _workflow(admin, [_notify("old", channel)])
        run_sql(
            "UPDATE workflow SET definition = jsonb_set(definition, '{steps,0,params}',"
            ' \'{"message": "old"}\'::jsonb) WHERE id = CAST(:id AS uuid)',
            {"id": wf["id"]},
        )
        run = _wait_until(admin, _run(admin, wf["id"]), {"done", "failed"})
    assert run["status"] == "failed"
    assert run["error"] == "old: channel_not_set"


def test_a_tolerated_failure_lets_later_steps_branch_on_it(channel: str) -> None:
    with signed_in("alice") as admin:
        node = _plain_node(admin)
        look = {**_exec("look", node, "uptime", read_only=True), "on_failure": "continue"}
        happy = {**_notify("happy", channel), "when": {"step": "look", "outcome": "ok"}}
        sad = {
            **_notify("sad", channel, "it failed"),
            "when": {"step": "look", "outcome": "failed"},
        }
        wf = _workflow(admin, [look, happy, sad])
        run_id = _run(admin, wf["id"])
        run = _wait_until(admin, run_id, {"done", "failed"})
    assert run["status"] == "done", run
    assert {s["step_id"]: s["status"] for s in run["steps"]} == {
        "look": "failed",
        "happy": "skipped",
        "sad": "done",
    }
    assert _notify_count(run_id) == 1


def test_a_failure_that_is_not_tolerated_still_fails_the_run(channel: str) -> None:
    with signed_in("alice") as admin:
        node = _plain_node(admin)
        wf = _workflow(
            admin,
            [
                _exec("look", node, "uptime", read_only=True),
                {**_notify("sad", channel), "when": {"step": "look", "outcome": "failed"}},
            ],
        )
        run = _wait_until(admin, _run(admin, wf["id"]), {"done", "failed"})
    assert run["status"] == "failed" and run["error"] == "look: exec_not_allowed"


def test_a_condition_on_a_later_step_is_refused(channel: str) -> None:
    with signed_in("alice") as admin:
        r = admin.post(
            "/api/v1/workflows",
            json={
                "name": _name("wf"),
                "definition": {
                    "trigger": {"type": "manual"},
                    "steps": [
                        {**_notify("a", channel), "when": {"step": "b"}},
                        _notify("b", channel),
                    ],
                },
            },
        )
    assert r.status_code == 422


def _signed(secret: str, body: bytes) -> dict[str, str]:
    return {
        "X-Janus-Signature": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    }


def test_a_webhook_workflow_starts_on_a_signed_post_and_only_then(channel: str) -> None:
    body = json.dumps({"ref": "release", "n": 1}).encode()
    with signed_in("alice") as admin:
        wf = _workflow(admin, [_notify("hi", channel)], trigger={"type": "webhook"})
        secret = wf["webhook_secret"]
        assert len(secret) >= 40 and wf["has_webhook"] is True
        fetched = admin.get(f"/api/v1/workflows/{wf['id']}")
        assert "webhook_secret" not in fetched.text
        url = f"/api/v1/workflows/{wf['id']}/webhook"
        with make_client() as anonymous:
            assert anonymous.post(url, content=body).status_code == 403  # no signature
            bad = anonymous.post(url, content=body, headers=_signed("wrong", body))
            assert bad.status_code == 403
            ok = anonymous.post(url, content=body, headers=_signed(secret, body))
            assert ok.status_code == 202, ok.text
            run_id = ok.json()["run_id"]
            # a second one right away is held back
            again = anonymous.post(url, content=body, headers=_signed(secret, body))
            assert again.status_code == 429
            # the run lives in this app's background task: wait for it in here
            run = _wait_until(admin, run_id, {"done", "failed"})
        assert run["status"] == "done" and run["trigger"] == "webhook"
        assert run["context"]["webhook"] == {"ref": "release", "n": 1}
        # rotating makes the old secret useless
        new = admin.post(f"/api/v1/workflows/{wf['id']}/rotate-secret").json()["webhook_secret"]
        assert new != secret
        with make_client() as anonymous:
            stale = anonymous.post(url, content=body, headers=_signed(secret, body))
            assert stale.status_code == 403
        # a disabled workflow does not exist for the outside
        admin.patch(f"/api/v1/workflows/{wf['id']}", json={"enabled": False})
        with make_client() as anonymous:
            gone = anonymous.post(url, content=body, headers=_signed(new, body))
            assert gone.status_code == 404
    rejected = run_sql(
        "SELECT count(*) FROM audit_event WHERE action = 'workflow.webhook.rejected'"
        " AND target_id = :n",
        {"n": wf["name"]},
    )
    assert int(rejected[0][0]) >= 2


def test_rotating_the_secret_of_a_manual_workflow_is_refused(channel: str) -> None:
    with signed_in("alice") as admin:
        wf = _workflow(admin, [_notify("hi", channel)])
        assert "webhook_secret" not in wf
        r = admin.post(f"/api/v1/workflows/{wf['id']}/rotate-secret")
    assert r.status_code == 409


def test_changing_the_trigger_adds_and_removes_the_secret(channel: str) -> None:
    with signed_in("alice") as admin:
        wf = _workflow(admin, [_notify("hi", channel)])
        steps = wf["definition"]["steps"]
        added = admin.patch(
            f"/api/v1/workflows/{wf['id']}",
            json={"definition": {"trigger": {"type": "webhook"}, "steps": steps}},
        ).json()
        assert added["has_webhook"] is True and len(added["webhook_secret"]) >= 40
        removed = admin.patch(
            f"/api/v1/workflows/{wf['id']}",
            json={"definition": {"trigger": {"type": "manual"}, "steps": steps}},
        ).json()
        assert removed["has_webhook"] is False and "webhook_secret" not in removed


def test_a_restart_resumes_a_run_between_steps_and_fails_one_with_a_command_in_flight(
    channel: str,
) -> None:
    with signed_in("alice") as admin:
        node = _plain_node(admin)
        resumable = _workflow(admin, [_notify("a", channel, "one"), _notify("b", channel, "two")])
        in_flight = _workflow(admin, [_exec("x", node, "systemctl restart foo")])

    def interrupted(workflow_id: str, step_id: str, action: str, status: str) -> str:
        rid = str(uuid.uuid4())
        run_sql(
            "INSERT INTO workflow_run (id, workflow_id, trigger, status, requested_by)"
            " VALUES (CAST(:r AS uuid), CAST(:w AS uuid), 'manual', 'running', 'alice')",
            {"r": rid, "w": workflow_id},
        )
        run_sql(
            "INSERT INTO workflow_step_run (run_id, step_id, position, action, status)"
            " VALUES (CAST(:r AS uuid), :s, 0, :a, :st)",
            {"r": rid, "s": step_id, "a": action, "st": status},
        )
        return rid

    resumed_id = interrupted(resumable["id"], "a", "notify", "done")
    failed_id = interrupted(in_flight["id"], "x", "exec", "running")

    async def restart() -> int:
        db = create_async_engine(DB_URL)
        jobs: set[asyncio.Task[None]] = set()
        try:
            failed = await wf_engine.recover(db, AgentHub(), jobs, Vault(None))
            await asyncio.gather(*jobs)
            return failed
        finally:
            await db.dispose()

    assert asyncio.run(restart()) >= 1
    states = {
        str(r[0]): (r[1], r[2])
        for r in run_sql(
            "SELECT id, status, error FROM workflow_run WHERE id IN"
            " (CAST(:a AS uuid), CAST(:b AS uuid))",
            {"a": resumed_id, "b": failed_id},
        )
    }
    assert states[resumed_id][0] == "done"
    assert states[failed_id] == ("failed", "server_restarted")
    assert _notify_count(resumed_id) == 1  # step "a" was already done: only "b" ran
