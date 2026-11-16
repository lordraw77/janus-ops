"""GitOps (F8) over the API and the database: permissions, the one-time webhook secret, signed
webhooks, refusals when the node has no agent, and the recording of a deploy. Real database and
lldap; the agent is not running, so the commands of a deploy are replaced by scripted results."""

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops.agents import exec as node_exec
from janus_ops.db import alembic_config
from janus_ops.gitops import deploy

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


def _node() -> str:
    name = f"gitops-{uuid.uuid4().hex[:8]}"
    return str(
        run_sql(
            "INSERT INTO node (name, address) VALUES (:n, '10.8.8.8') RETURNING id", {"n": name}
        )[0][0]
    )


def _body(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": f"app-{uuid.uuid4().hex[:6]}",
        "node_id": _node(),
        "repo_url": "https://git.example.com/org/app.git",
        "branch": "main",
        "compose_file": "docker-compose.yml",
    }
    values.update(overrides)
    return values


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push(branch: str = "main", **extra: Any) -> bytes:
    return json.dumps({"ref": f"refs/heads/{branch}", **extra}).encode()


def _audit_count(action: str, target: str) -> int:
    return int(
        run_sql(
            "SELECT count(*) FROM audit_event WHERE action = :a AND target_id = :t",
            {"a": action, "t": target},
        )[0][0]
    )


def test_auditor_reads_but_cannot_create_or_deploy() -> None:
    with signed_in("alice") as admin:
        stack = admin.post("/api/v1/gitops/stacks", json=_body()).json()
    with signed_in("carol") as auditor:
        assert auditor.get("/api/v1/gitops/stacks").status_code == 200
        assert auditor.post("/api/v1/gitops/stacks", json=_body()).status_code == 403
        assert auditor.post(f"/api/v1/gitops/stacks/{stack['id']}/deploy").status_code == 403


def test_operator_deploys_but_does_not_manage_stacks() -> None:
    with signed_in("bob") as operator:
        assert operator.post("/api/v1/gitops/stacks", json=_body()).status_code == 403


def test_secret_is_returned_once_and_never_by_the_api() -> None:
    with signed_in("alice") as admin:
        r = admin.post("/api/v1/gitops/stacks", json=_body())
        assert r.status_code == 201, r.text
        created = r.json()
        assert len(created["webhook_secret"]) >= 40
        fetched = admin.get(f"/api/v1/gitops/stacks/{created['id']}").json()
        assert "webhook_secret" not in fetched
        listed = admin.get("/api/v1/gitops/stacks").json()
        assert all("webhook_secret" not in s for s in listed)
    assert _audit_count("gitops.stack.created", created["name"]) == 1


def test_invalid_input_is_refused_with_a_stable_code() -> None:
    with signed_in("alice") as admin:
        r = admin.post(
            "/api/v1/gitops/stacks",
            json=_body(repo_url="https://user:pw@git.example.com/org/app.git"),
        )
        assert r.status_code == 422 and r.json()["detail"] == "invalid_repo_url"
        r = admin.post("/api/v1/gitops/stacks", json=_body(compose_file="../x.yml"))
        assert r.status_code == 422 and r.json()["detail"] == "invalid_compose_file"


def test_stack_name_is_unique() -> None:
    body = _body()
    with signed_in("alice") as admin:
        assert admin.post("/api/v1/gitops/stacks", json=body).status_code == 201
        r = admin.post("/api/v1/gitops/stacks", json={**body, "node_id": _node()})
        assert r.status_code == 409 and r.json()["detail"] == "name_taken"


def test_manual_deploy_is_refused_when_the_node_has_no_agent() -> None:
    with signed_in("alice") as admin:
        stack = admin.post("/api/v1/gitops/stacks", json=_body()).json()
    with signed_in("bob") as operator:
        r = operator.post(f"/api/v1/gitops/stacks/{stack['id']}/deploy")
        assert r.status_code == 409 and r.json()["detail"] == "agent_offline"
        runs = operator.get(f"/api/v1/gitops/stacks/{stack['id']}/runs").json()
        assert len(runs) == 1
        assert runs[0]["status"] == "refused" and runs[0]["reason"] == "agent_offline"
        assert runs[0]["trigger"] == "manual" and runs[0]["requested_by"] == "bob"
        assert operator.get(f"/api/v1/gitops/stacks/{stack['id']}").json()["last_status"] == (
            "refused"
        )


def test_webhook_with_a_bad_signature_is_refused_and_audited() -> None:
    with signed_in("alice") as admin:
        stack = admin.post("/api/v1/gitops/stacks", json=_body()).json()
    with make_client() as anonymous:
        r = anonymous.post(
            f"/api/v1/gitops/stacks/{stack['id']}/webhook",
            content=_push(),
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )
        assert r.status_code == 403 and r.json()["detail"] == "bad_signature"
        assert (
            anonymous.post(
                f"/api/v1/gitops/stacks/{stack['id']}/webhook", content=_push()
            ).status_code
            == 403
        )
    assert _audit_count("gitops.webhook.rejected", stack["name"]) == 2


def test_signed_pushes_to_the_branch_reach_the_deploy_and_other_events_do_not() -> None:
    with signed_in("alice") as admin:
        created = admin.post("/api/v1/gitops/stacks", json=_body()).json()
    secret = created["webhook_secret"]
    url = f"/api/v1/gitops/stacks/{created['id']}/webhook"
    with make_client() as anonymous:
        ping = b'{"zen": "keep it logically awesome"}'
        r = anonymous.post(url, content=ping, headers={"X-Hub-Signature-256": _sign(secret, ping)})
        assert r.status_code == 202 and r.json() == {"status": "ignored", "reason": "not_a_push"}

        other = _push("dev")
        r = anonymous.post(
            url, content=other, headers={"X-Hub-Signature-256": _sign(secret, other)}
        )
        assert r.status_code == 202 and r.json()["reason"] == "other_branch"

        push = _push()
        r = anonymous.post(url, content=push, headers={"X-Hub-Signature-256": _sign(secret, push)})
        # signed and for the branch: it reaches the deploy, which is refused (no agent here)
        assert r.status_code == 409 and r.json()["detail"] == "agent_offline"
    count = run_sql("SELECT count(*) FROM gitops_run WHERE stack_id = :s", {"s": created["id"]})
    assert count[0][0] == 1


def test_rotating_the_secret_invalidates_the_old_one() -> None:
    with signed_in("alice") as admin:
        created = admin.post("/api/v1/gitops/stacks", json=_body()).json()
        rotated = admin.post(f"/api/v1/gitops/stacks/{created['id']}/rotate-secret").json()[
            "webhook_secret"
        ]
    assert rotated != created["webhook_secret"]
    url = f"/api/v1/gitops/stacks/{created['id']}/webhook"
    push = _push()
    with make_client() as anonymous:
        old = anonymous.post(
            url,
            content=push,
            headers={"X-Hub-Signature-256": _sign(created["webhook_secret"], push)},
        )
        assert old.status_code == 403
        new = anonymous.post(
            url, content=push, headers={"X-Hub-Signature-256": _sign(rotated, push)}
        )
        assert new.status_code == 409


def test_disabled_stack_answers_not_found_to_webhooks() -> None:
    with signed_in("alice") as admin:
        created = admin.post("/api/v1/gitops/stacks", json=_body()).json()
        assert (
            admin.patch(
                f"/api/v1/gitops/stacks/{created['id']}", json={"enabled": False}
            ).status_code
            == 200
        )
        with signed_in("bob") as operator:
            assert operator.post(f"/api/v1/gitops/stacks/{created['id']}/deploy").status_code == 409
    push = _push()
    with make_client() as anonymous:
        r = anonymous.post(
            f"/api/v1/gitops/stacks/{created['id']}/webhook",
            content=push,
            headers={"X-Hub-Signature-256": _sign(created["webhook_secret"], push)},
        )
        assert r.status_code == 404


def test_at_most_one_running_deploy_per_stack() -> None:
    with signed_in("alice") as admin:
        stack = admin.post("/api/v1/gitops/stacks", json=_body()).json()
    run_sql(
        "INSERT INTO gitops_run (stack_id, trigger, status, requested_by)"
        " VALUES (:s, 'manual', 'running', 'bob')",
        {"s": stack["id"]},
    )
    with pytest.raises(IntegrityError):
        run_sql(
            "INSERT INTO gitops_run (stack_id, trigger, status, requested_by)"
            " VALUES (:s, 'manual', 'running', 'bob')",
            {"s": stack["id"]},
        )


def _scripted(results: list[SimpleNamespace]) -> Any:
    calls: list[list[str]] = []

    async def fake_run(hub: Any, node_id: str, argv: list[str], timeout_s: int) -> Any:
        calls.append(argv)
        return results.pop(0)

    fake_run.calls = calls  # type: ignore[attr-defined]
    return fake_run


def _result(exit_code: int, output: str = "", reason: str = "exit:0") -> SimpleNamespace:
    return SimpleNamespace(exit_code=exit_code, reason=reason, output=output, truncated=False)


def test_a_deploy_records_the_commit_and_stops_at_the_first_failing_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with signed_in("alice") as admin:
        created = admin.post("/api/v1/gitops/stacks", json=_body()).json()
    stack = deploy.Stack(
        id=created["id"],
        name=created["name"],
        node_id=created["node_id"],
        repo_url=created["repo_url"],
        branch=created["branch"],
        compose_file=created["compose_file"],
    )
    run_id = str(
        run_sql(
            "INSERT INTO gitops_run (stack_id, trigger, status, requested_by)"
            " VALUES (:s, 'manual', 'running', 'bob') RETURNING id",
            {"s": stack.id},
        )[0][0]
    )
    # probe: the checkout is absent (1); mkdir, clone, commit, then compose fails
    scripted = _scripted(
        [
            _result(1, reason="exit:1"),
            _result(0),
            _result(0),
            _result(0, "3f9c2e1d\n"),
            _result(2, "service web: pull denied\n", reason="exit:2"),
        ]
    )
    monkeypatch.setattr(node_exec, "run", scripted)
    engine = create_async_engine(DB_URL)
    try:
        asyncio.run(deploy.execute(engine, object(), run_id, stack))  # type: ignore[arg-type]
    finally:
        asyncio.run(engine.dispose())

    row = run_sql(
        "SELECT status, reason, exit_code, commit_sha, output, finished_at IS NOT NULL"
        " FROM gitops_run WHERE id = :id",
        {"id": run_id},
    )[0]
    assert row[0] == "failed"
    assert row[1] == "compose up: exit:2"
    assert row[2] == 2
    assert row[3] == "3f9c2e1d"
    assert "$ compose up\nservice web: pull denied" in row[4]
    assert row[5] is True
    assert scripted.calls[-1][0] == "docker"  # nothing ran after the failing command
    last = run_sql("SELECT last_status FROM gitops_stack WHERE id = :s", {"s": stack.id})
    assert last[0][0] == "failed"
    assert _audit_count("gitops.deploy.finished", stack.name) == 1


def test_a_successful_deploy_updates_an_existing_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    with signed_in("alice") as admin:
        created = admin.post("/api/v1/gitops/stacks", json=_body()).json()
    stack = deploy.Stack(
        id=created["id"],
        name=created["name"],
        node_id=created["node_id"],
        repo_url=created["repo_url"],
        branch=created["branch"],
        compose_file=created["compose_file"],
    )
    run_id = str(
        run_sql(
            "INSERT INTO gitops_run (stack_id, trigger, status, requested_by)"
            " VALUES (:s, 'webhook', 'running', 'webhook') RETURNING id",
            {"s": stack.id},
        )[0][0]
    )
    scripted = _scripted(
        [
            _result(0),  # probe: the checkout exists
            _result(0),  # mkdir
            _result(0),  # remote set-url
            _result(0),  # fetch
            _result(0),  # reset
            _result(0, "aa11bb22\n"),  # commit
            _result(0, "Creating web ... done\n"),  # compose up
        ]
    )
    monkeypatch.setattr(node_exec, "run", scripted)
    engine = create_async_engine(DB_URL)
    try:
        asyncio.run(deploy.execute(engine, object(), run_id, stack))  # type: ignore[arg-type]
    finally:
        asyncio.run(engine.dispose())
    status = run_sql("SELECT status, commit_sha FROM gitops_run WHERE id = :id", {"id": run_id})
    assert tuple(status[0]) == ("ok", "aa11bb22")
    assert any("fetch" in a for a in scripted.calls)  # wrapped in `env`, so not at argv[0]
    assert not any("clone" in a for a in scripted.calls)
