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
        assert operator.get("/api/v1/ai/status").json() == {"enabled": False, "model": None}
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
