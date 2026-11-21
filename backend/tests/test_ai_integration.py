"""AI Copilot (F7) over the API: switched off without an endpoint, permissions, and the approval of
action plans (decided once, edits only while proposed, audited). Real database + lldap; the model
itself is not called here (see test_ai_unit.py)."""

import json
import uuid
from collections.abc import Iterator

import pytest
from alembic import command

from janus_ops.db import alembic_config

from ._it import CONFIGURED, DB_URL, run_sql, signed_in

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


def _action(action_id: str, title: str) -> dict[str, object]:
    return {
        "id": action_id,
        "title": title,
        "description": "Check the volume",
        "command": None,
        "risk": "low",
        "status": "proposed",
        "decided_by": None,
        "decided_at": None,
    }


def _seed_done_diagnosis(actions: list[dict[str, object]]) -> str:
    name = f"ai-{uuid.uuid4().hex[:8]}"
    node_id = run_sql(
        "INSERT INTO node (name, address) VALUES (:n, '10.9.9.9') RETURNING id", {"n": name}
    )[0][0]
    check_id = run_sql(
        "INSERT INTO service_check (node_id, name, kind) VALUES (:n, 'disk', 'agent') RETURNING id",
        {"n": node_id},
    )[0][0]
    problem_id = run_sql(
        "INSERT INTO problem (check_id, state) VALUES (:c, 'crit') RETURNING id", {"c": check_id}
    )[0][0]
    return str(
        run_sql(
            "INSERT INTO ai_diagnosis (problem_id, node_id, node_name, check_name, status, model,"
            " requested_by, summary, actions) VALUES (:p, :n, :nn, 'disk', 'done', 'm', 'alice',"
            " 'Disk full', CAST(:a AS jsonb)) RETURNING id",
            {"p": problem_id, "n": node_id, "nn": name, "a": json.dumps(actions)},
        )[0][0]
    )


def test_copilot_is_off_without_an_endpoint() -> None:
    with signed_in("bob") as operator:
        assert operator.get("/api/v1/ai/status").json() == {
            "enabled": False,
            "model": None,
            "review_enabled": False,
            "review_model": None,
        }
        r = operator.post(f"/api/v1/ai/problems/{uuid.uuid4()}/diagnose")
        assert r.status_code == 503 and r.json()["detail"] == "ai_not_configured"


def test_auditor_cannot_use_the_copilot() -> None:
    with signed_in("carol") as auditor:
        assert auditor.get("/api/v1/ai/diagnoses").status_code == 403


def test_operator_approves_once_and_the_decision_is_audited() -> None:
    diagnosis = _seed_done_diagnosis([_action("a1", "Free space")])
    with signed_in("bob") as operator:
        r = operator.patch(
            f"/api/v1/ai/diagnoses/{diagnosis}/actions/a1", json={"status": "approved"}
        )
        assert r.status_code == 200, r.text
        action = r.json()["actions"][0]
        assert action["status"] == "approved" and action["decided_by"] == "bob"
        again = operator.patch(
            f"/api/v1/ai/diagnoses/{diagnosis}/actions/a1", json={"status": "rejected"}
        )
        assert again.status_code == 409 and again.json()["detail"] == "already_decided"
    audited = run_sql(
        "SELECT count(*) FROM audit_event WHERE action = 'ai.action.decided'"
        " AND payload->>'diagnosis' = :d",
        {"d": diagnosis},
    )
    assert audited[0][0] == 1


def test_edits_are_allowed_only_while_proposed() -> None:
    diagnosis = _seed_done_diagnosis([_action("a1", "Free space"), _action("a2", "Restart")])
    with signed_in("bob") as operator:
        empty = operator.patch(f"/api/v1/ai/diagnoses/{diagnosis}/actions/a1", json={})
        assert empty.status_code == 422 and empty.json()["detail"] == "nothing_to_change"
        r = operator.patch(
            f"/api/v1/ai/diagnoses/{diagnosis}/actions/a2",
            json={"status": "rejected", "title": "Restart the agent"},
        )
        assert r.status_code == 200, r.text
        second = next(a for a in r.json()["actions"] if a["id"] == "a2")
        assert second["title"] == "Restart the agent" and second["status"] == "rejected"
        assert second["decided_by"] == "bob"
        missing = operator.patch(
            f"/api/v1/ai/diagnoses/{diagnosis}/actions/zz", json={"status": "approved"}
        )
        assert missing.status_code == 404
        detail = operator.get(f"/api/v1/ai/diagnoses/{diagnosis}")
        assert detail.status_code == 200 and detail.json()["summary"] == "Disk full"


def _approved(action_id: str, command: str) -> dict[str, object]:
    return {**_action(action_id, "Run it"), "status": "approved", "command": command}


def test_running_an_action_needs_the_execute_permission() -> None:
    diagnosis = _seed_done_diagnosis([_approved("a1", "uptime")])
    with signed_in("bob") as operator:
        r = operator.post(f"/api/v1/ai/diagnoses/{diagnosis}/actions/a1/execute")
        assert r.status_code == 403


def test_only_approved_actions_run_and_a_refused_run_leaves_the_action_approved() -> None:
    diagnosis = _seed_done_diagnosis(
        [_action("a1", "Proposed only"), _approved("a2", "uptime"), _approved("a3", "uptime; id")]
    )
    with signed_in("alice") as admin:
        proposed = admin.post(f"/api/v1/ai/diagnoses/{diagnosis}/actions/a1/execute")
        assert proposed.status_code == 409 and proposed.json()["detail"] == "not_approved"

        chained = admin.post(f"/api/v1/ai/diagnoses/{diagnosis}/actions/a3/execute")
        assert chained.status_code == 422
        assert chained.json()["detail"] == "command_not_executable"

        # no agent for this node, so exec is not allowed: refused before anything runs
        refused = admin.post(f"/api/v1/ai/diagnoses/{diagnosis}/actions/a2/execute")
        assert refused.status_code == 409 and refused.json()["detail"] == "exec_not_allowed"
        after = admin.get(f"/api/v1/ai/diagnoses/{diagnosis}").json()
        status = {a["id"]: a["status"] for a in after["actions"]}
        assert status == {"a1": "proposed", "a2": "approved", "a3": "approved"}
    audited = run_sql(
        "SELECT count(*) FROM audit_event WHERE action = 'ai.action.execute_refused'"
        " AND payload->>'diagnosis' = :d",
        {"d": diagnosis},
    )
    assert audited[0][0] == 1


def test_second_opinion_and_iac_are_off_without_an_endpoint() -> None:
    diagnosis = _seed_done_diagnosis([_action("a1", "Free space")])
    with signed_in("bob") as operator:
        r = operator.post(f"/api/v1/ai/diagnoses/{diagnosis}/second-opinion")
        assert r.status_code == 503 and r.json()["detail"] == "ai_review_not_configured"
        r = operator.post(
            "/api/v1/ai/iac",
            json={"node_id": str(uuid.uuid4()), "kind": "shell", "request": "rotate the logs"},
        )
        assert r.status_code == 503 and r.json()["detail"] == "ai_not_configured"
    with signed_in("carol") as auditor:
        assert auditor.get("/api/v1/ai/iac").status_code == 403
        assert auditor.post(f"/api/v1/ai/diagnoses/{diagnosis}/second-opinion").status_code == 403


def test_ai_settings_are_for_administrators_and_never_return_the_key() -> None:
    with signed_in("bob") as operator:
        assert operator.get("/api/v1/ai/config").status_code == 403
        assert operator.put("/api/v1/ai/config/main", json={}).status_code == 403
    with signed_in("alice") as admin:
        r = admin.put(
            "/api/v1/ai/config/review",
            json={"base_url": "http://insecure.lan/v1", "model": "m"},
        )
        assert r.status_code == 422 and r.json()["detail"] == "invalid_ai_url"
        r = admin.put(
            "/api/v1/ai/config/review",
            json={"base_url": "https://llm.lan/v1", "model": "second", "api_key": "sekret-123"},
        )
        assert r.status_code == 204
        body = admin.get("/api/v1/ai/config").json()
        review = next(s for s in body["slots"] if s["slot"] == "review")
        assert review["model"] == "second" and review["key_set"] is True
        assert "sekret-123" not in admin.get("/api/v1/ai/config").text
        assert admin.delete("/api/v1/ai/config/review").status_code == 204
        assert admin.delete("/api/v1/ai/config/review").status_code == 404


def test_mcp_settings_are_for_administrators_and_validate_environments() -> None:
    with signed_in("bob") as operator:
        assert operator.get("/api/v1/ai/config/mcp").status_code == 403
        assert operator.put("/api/v1/ai/config/mcp", json={}).status_code == 403
    with signed_in("alice") as admin:
        body = admin.get("/api/v1/ai/config/mcp").json()
        assert body["allow_write"] is True and body["enabled_source"] == "environment"
        bad = admin.put("/api/v1/ai/config/mcp", json={"plan_only_environments": ["nope"]})
        assert bad.status_code == 422
        env = body["environments"][0]
        ok = admin.put(
            "/api/v1/ai/config/mcp",
            json={"enabled": False, "allow_write": False, "plan_only_environments": [env]},
        )
        assert ok.status_code == 204
        saved = admin.get("/api/v1/ai/config/mcp").json()
        assert saved["enabled"] is False and saved["enabled_source"] == "database"
        assert saved["allow_write"] is False and saved["plan_only_environments"] == [env]
        admin.put("/api/v1/ai/config/mcp", json={})  # back to the defaults


def _node(name: str, address: str, ssh: bool) -> str:
    node_id = str(
        run_sql(
            "INSERT INTO node (name, address) VALUES (:n, :a) RETURNING id",
            {"n": name, "a": address},
        )[0][0]
    )
    if ssh:
        run_sql(
            "INSERT INTO node_capability (node_id, kind, enabled, config)"
            " VALUES (:n, 'ssh', true, '{}')",
            {"n": node_id},
        )
    return node_id


def test_operate_sessions_need_the_permission_and_pick_nodes_in_scope() -> None:
    name = f"op-{uuid.uuid4().hex[:8]}"
    node_id = _node(name, "10.9.9.8", ssh=True)
    plain = _node(name + "x", "10.9.9.7", ssh=False)
    base = "/api/v1/ai/operate"
    with signed_in("bob") as operator:
        assert operator.get(f"{base}/nodes").status_code == 403
        assert operator.post(f"{base}/sessions", json={"node_ids": [node_id]}).status_code == 403
    with signed_in("alice") as admin:
        nodes = {n["name"]: n for n in admin.get(f"{base}/nodes").json()}
        assert nodes[name]["linux"] == "no_credential" and name + "x" not in nodes
        # a node without SSH or Proxmox cannot be picked; neither can an unknown one
        for bad in (plain, str(uuid.uuid4())):
            assert admin.post(f"{base}/sessions", json={"node_ids": [bad]}).status_code == 404
        assert admin.post(f"{base}/sessions", json={"node_ids": []}).status_code == 422
        created = admin.post(f"{base}/sessions", json={"node_ids": [node_id]})
        assert created.status_code == 201
        sid = created.json()["id"]
        assert sid in [s["id"] for s in admin.get(f"{base}/sessions").json()]
        detail = admin.get(f"{base}/sessions/{sid}").json()
        assert detail["node_names"] == [name] and detail["messages"] == []
        sent = admin.post(f"{base}/sessions/{sid}/messages", json={"content": "x"})
        assert sent.status_code == 503 and sent.json()["detail"] == "ai_not_configured"
        # provider, model, title, archiving
        assert detail["provider"] == "main" and detail["model"] is None and not detail["archived"]
        assert admin.patch(f"{base}/sessions/{sid}", json={"provider": "other"}).status_code == 422
        assert (
            admin.patch(f"{base}/sessions/{sid}", json={"model": "bad model!"}).status_code == 422
        )
        patched = admin.patch(
            f"{base}/sessions/{sid}",
            json={"title": "NTP", "model": "m-2", "provider": "review", "archived": True},
        )
        assert patched.status_code == 204
        detail = admin.get(f"{base}/sessions/{sid}").json()
        assert (detail["title"], detail["model"], detail["provider"]) == ("NTP", "m-2", "review")
        assert detail["archived"] is True
        assert sid not in [s["id"] for s in admin.get(f"{base}/sessions").json()]
        archived = admin.get(f"{base}/sessions", params={"archived": "true"}).json()
        assert sid in [s["id"] for s in archived]
        # the second provider is not set up here: sending says so (before the AI-off check)
        providers = {p["slot"]: p for p in admin.get(f"{base}/providers").json()}
        assert providers["main"]["available"] is False and providers["review"]["available"] is False
        assert admin.get(f"{base}/providers/review/models").status_code == 422
        assert admin.get(f"{base}/providers/nope/models").status_code == 404
        assert admin.patch(f"{base}/sessions/{sid}", json={"clear_model": True}).status_code == 204
        assert admin.get(f"{base}/sessions/{sid}").json()["model"] is None
        assert admin.delete(f"{base}/sessions/{sid}").status_code == 204
        assert admin.get(f"{base}/sessions/{sid}").status_code == 404
