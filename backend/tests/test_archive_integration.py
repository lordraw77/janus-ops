"""Retention in two steps (archive after X days, delete after Y), and the archive API.
Real database + lldap."""

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
BASE = "/api/v1/archive"


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def _recording(tag: str, days_ago: float, keep: bool = False) -> str:
    return str(
        run_sql(
            "INSERT INTO terminal_session (transport, started_at, ended_at, node_name, target,"
            " remote_user, keep) VALUES ('ssh', now() - make_interval(days => :d) - interval"
            " '1 minute', now() - make_interval(days => :d), :n, '10.0.0.1:22', 'root', :k)"
            " RETURNING id",
            {"d": days_ago, "n": tag, "k": keep},
        )[0][0]
    )


def _chat(tag: str, days_ago: float, keep: bool = False, text: str = "hello") -> str:
    sid = str(
        run_sql(
            "INSERT INTO ai_operate_session (title, created_by, node_ids, node_names, keep,"
            " updated_at) VALUES (:t, 'alice', '{}', ARRAY[:t], :k,"
            " now() - make_interval(days => :d)) RETURNING id",
            {"t": tag, "k": keep, "d": days_ago},
        )[0][0]
    )
    run_sql(
        "INSERT INTO ai_operate_message (session_id, role, content) VALUES"
        " (CAST(:s AS uuid), 'user', :c)",
        {"s": sid, "c": text},
    )
    return sid


def _exists(table: str, row_id: str) -> bool:
    return bool(
        run_sql(f"SELECT 1 FROM {table} WHERE id = CAST(:i AS uuid)", {"i": row_id})  # noqa: S608
    )


def test_defaults_and_permissions() -> None:
    with signed_in("bob") as operator:
        assert operator.get(f"{BASE}/recordings").status_code == 403
        assert operator.put(f"{BASE}/retention", json={}).status_code == 403
    with signed_in("alice") as admin:
        assert admin.get(f"{BASE}/retention").json() == {
            "archive_after_days": 1,
            "delete_after_days": 30,
        }
        for bad in ({"archive_after_days": 5, "delete_after_days": 5},
                    {"archive_after_days": 10, "delete_after_days": 3},
                    {"archive_after_days": -1, "delete_after_days": 30}):  # fmt: skip
            assert admin.put(f"{BASE}/retention", json=bad).status_code == 422
        ok = admin.put(f"{BASE}/retention", json={"archive_after_days": 2, "delete_after_days": 60})
        assert ok.status_code == 204
        assert admin.get(f"{BASE}/retention").json()["delete_after_days"] == 60
        admin.put(f"{BASE}/retention", json={"archive_after_days": 1, "delete_after_days": 30})


def test_two_steps_then_search_keep_and_delete() -> None:
    tag = f"ret-{uuid.uuid4().hex[:8]}"
    fresh = _recording(tag + "-fresh", 0.05)
    old = _recording(tag + "-old", 5)
    ancient = _recording(tag + "-ancient", 40)
    kept = _recording(tag + "-kept", 40, keep=True)
    chat_old = _chat(tag + "-chat-old", 5, text=f"change the ntp server {tag}")
    chat_ancient = _chat(tag + "-chat-ancient", 40)
    chat_kept = _chat(tag + "-chat-kept", 40, keep=True)
    with signed_in("alice") as admin:
        ran = admin.post(f"{BASE}/run")
        assert ran.status_code == 200
        assert ran.json()["archived_recordings"] >= 2 and ran.json()["deleted_chats"] >= 1
        # step 1: archived after X days; step 2: deleted after Y days, unless kept
        assert not run_sql("SELECT 1 FROM terminal_session WHERE id = CAST(:i AS uuid)"
                           " AND archived_at IS NOT NULL", {"i": fresh})  # fmt: skip
        assert _exists("terminal_session", old) and not _exists("terminal_session", ancient)
        assert _exists("terminal_session", kept)
        assert _exists("ai_operate_session", chat_old)
        assert not _exists("ai_operate_session", chat_ancient)
        assert _exists("ai_operate_session", chat_kept)
        # the everyday list no longer shows what is archived
        names = {s["node_name"] for s in admin.get("/api/v1/terminal/sessions").json()}
        assert tag + "-fresh" in names and tag + "-old" not in names
        # the archive: search by metadata, and by the text of a chat message
        found = admin.get(f"{BASE}/recordings", params={"q": tag}).json()
        assert {i["node_name"] for i in found["items"]} == {tag + "-old", tag + "-kept"}
        item = next(i for i in found["items"] if i["node_name"] == tag + "-old")
        assert item["keep"] is False and item["expires_at"] is not None
        kept_item = next(i for i in found["items"] if i["node_name"] == tag + "-kept")
        assert kept_item["keep"] is True and kept_item["expires_at"] is None
        # sorting on a column (whitelisted), by host and by group
        asc = admin.get(f"{BASE}/recordings", params={"q": tag, "sort": "node", "dir": "asc"})
        assert [i["node_name"] for i in asc.json()["items"]] == [tag + "-kept", tag + "-old"]
        desc = admin.get(f"{BASE}/recordings", params={"q": tag, "sort": "node", "dir": "desc"})
        assert [i["node_name"] for i in desc.json()["items"]] == [tag + "-old", tag + "-kept"]
        junk = admin.get(f"{BASE}/recordings", params={"sort": "id; drop table x", "dir": "asc"})
        assert junk.status_code == 200  # an unknown column falls back to the default order
        assert admin.get(f"{BASE}/recordings", params={"dir": "sideways"}).status_code == 422
        by_host = admin.get(f"{BASE}/recordings", params={"host": tag + "-old"}).json()
        assert [i["node_name"] for i in by_host["items"]] == [tag + "-old"]
        opts = admin.get(f"{BASE}/filters", params={"kind": "recordings"}).json()
        assert tag + "-old" in opts["hosts"] and isinstance(opts["groups"], list)
        assert admin.get(f"{BASE}/filters", params={"kind": "nope"}).status_code == 422
        by_group = admin.get(f"{BASE}/recordings", params={"group": str(uuid.uuid4())}).json()
        assert by_group["total"] == 0
        chats_host = admin.get(f"{BASE}/chats", params={"host": tag + "-chat-old"}).json()
        assert [c["id"] for c in chats_host["items"]] == [chat_old]
        chats_sorted = admin.get(f"{BASE}/chats", params={"sort": "messages", "dir": "asc"})
        assert chats_sorted.status_code == 200
        only_kept = admin.get(f"{BASE}/recordings", params={"q": tag, "keep": "true"}).json()
        assert only_kept["total"] == 1
        chats = admin.get(f"{BASE}/chats", params={"q": f"ntp server {tag}"}).json()
        assert [c["id"] for c in chats["items"]] == [chat_old]
        detail = admin.get(f"{BASE}/chats/{chat_old}").json()
        assert detail["messages"][0]["content"].startswith("change the ntp")
        assert admin.get(f"{BASE}/chats/{uuid.uuid4()}").status_code == 404
        # keep / un-keep, and a kept item is not deleted by hand either
        mark = admin.post(f"{BASE}/keep", json={"kind": "recordings", "ids": [old], "keep": True})
        assert mark.json() == {"updated": 1, "not_found": 0}
        blocked = admin.post(f"{BASE}/delete", json={"kind": "recordings", "ids": [old, kept]})
        assert blocked.json() == {"deleted": 0, "protected": 2, "not_found": 0}
        admin.post(f"{BASE}/keep", json={"kind": "recordings", "ids": [old, kept], "keep": False})
        # several at once; ids that are unknown or not archived are counted, not an error
        gone = admin.post(
            f"{BASE}/delete",
            json={"kind": "recordings", "ids": [old, kept, fresh, str(uuid.uuid4())]},
        )
        assert gone.json() == {"deleted": 2, "protected": 0, "not_found": 2}
        assert not _exists("terminal_session", old) and _exists("terminal_session", fresh)
        chat_gone = admin.post(
            f"{BASE}/delete", json={"kind": "chats", "ids": [chat_old, chat_kept]}
        )
        assert chat_gone.json()["deleted"] == 1 and chat_gone.json()["protected"] == 1
        admin.post(f"{BASE}/keep", json={"kind": "chats", "ids": [chat_kept], "keep": False})
        assert (
            admin.post(f"{BASE}/delete", json={"kind": "chats", "ids": [chat_kept]}).json()[
                "deleted"
            ]
            == 1
        )
        too_many = [str(uuid.uuid4()) for _ in range(201)]
        assert (
            admin.post(f"{BASE}/delete", json={"kind": "chats", "ids": too_many}).status_code == 422
        )
    run_sql("DELETE FROM terminal_session WHERE id = CAST(:i AS uuid)", {"i": fresh})


def test_audit_log_filters_by_host_and_group() -> None:
    with signed_in("alice") as admin:
        everything = admin.get("/api/v1/audit", params={"limit": 5}).json()["events"]
        assert everything  # the archive tests above wrote some
        for params in ({"host": "no-such-host"}, {"group": str(uuid.uuid4())}):
            assert admin.get("/api/v1/audit", params=params).json()["events"] == []
        assert admin.get("/api/v1/audit", params={"group": "not-a-uuid"}).status_code == 422
