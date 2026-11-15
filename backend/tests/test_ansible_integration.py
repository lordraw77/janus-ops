"""Playbook catalog end to end (F6): a survey next to a playbook, answers frozen with the run,
the refusals of the API and the SFTP source rules. Real database + lldap."""

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

SURVEY = """{"questions": [
  {"name": "app_version", "label": "Version", "type": "text", "required": true},
  {"name": "replicas", "label": "Replicas", "type": "integer", "default": 2},
  {"name": "channel", "label": "Channel", "type": "choice", "choices": ["stable", "beta"]}
]}"""


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def _error(r: object) -> str:
    return r.json()["detail"]  # type: ignore[attr-defined,no-any-return]


def test_survey_answers_are_checked_and_frozen_with_the_run() -> None:
    with signed_in("alice") as admin:
        r = admin.post("/api/v1/ansible/sources", json={"name": "survey-src", "kind": "editor"})
        assert r.status_code == 201, r.text
        source_id = r.json()["id"]
        r = admin.post(
            f"/api/v1/ansible/sources/{source_id}/files",
            json={"path": "site.yml", "content": "- hosts: all\n  tasks: []\n"},
        )
        assert r.status_code == 201, r.text
        playbook_id = r.json()["id"]
        r = admin.post(
            f"/api/v1/ansible/sources/{source_id}/files",
            json={"path": "site.survey.json", "content": SURVEY},
        )
        assert r.status_code == 201, r.text

        survey = admin.get(f"/api/v1/ansible/files/{playbook_id}/survey").json()["questions"]
        assert [q["name"] for q in survey] == ["app_version", "replicas", "channel"]

        node = str(uuid.uuid4())  # not in the inventory: the attempt is recorded with a reason
        url = f"/api/v1/ansible/files/{playbook_id}/runs"
        r = admin.post(url, json={"node_ids": [node], "check": True, "survey": {"replicas": "3"}})
        assert r.status_code == 422 and _error(r) == "survey_required"
        r = admin.post(url, json={"node_ids": [node], "survey": {"nope": "x"}})
        assert r.status_code == 422 and _error(r) == "survey_unknown_question"
        r = admin.post(
            url, json={"node_ids": [node], "survey": {"app_version": "1", "channel": "nightly"}}
        )
        assert r.status_code == 422 and _error(r) == "survey_value_invalid"

        answers = {"app_version": "1.4.2", "replicas": "5", "channel": "beta"}
        r = admin.post(url, json={"node_ids": [node], "check": True, "survey": answers})
        assert r.status_code == 202, r.text
        run_id = r.json()["id"]
        stored = run_sql(
            "SELECT survey_values FROM job_run WHERE id = CAST(:id AS uuid)", {"id": run_id}
        )
        assert stored[0].survey_values == {"app_version": "1.4.2", "replicas": 5, "channel": "beta"}


def test_only_admins_manage_sources_and_sftp_is_checked_before_saving() -> None:
    with signed_in("bob") as operator:
        r = operator.post("/api/v1/ansible/sources", json={"name": "nope", "kind": "editor"})
        assert r.status_code == 403
    with signed_in("alice") as admin:
        base = {"name": "sftp-src", "kind": "sftp"}
        r = admin.post("/api/v1/ansible/sources", json={**base, "url": "sftp://h/srv/x"})
        assert r.status_code == 422 and _error(r) == "credential_required"
        r = admin.post(
            "/api/v1/ansible/sources",
            json={**base, "url": "sftp://h/../etc", "credential_id": str(uuid.uuid4())},
        )
        assert r.status_code == 422 and _error(r) == "invalid_sftp_url"
        r = admin.post(
            "/api/v1/ansible/sources",
            json={**base, "url": "sftp://h/srv/x", "credential_id": str(uuid.uuid4())},
        )
        assert r.status_code == 422 and _error(r) == "credential_kind_unsupported"
