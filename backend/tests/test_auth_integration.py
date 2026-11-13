"""Login end to end: PostgreSQL + TimescaleDB and a real lldap seeded from tests/fixtures/ldap.

Run with: make test/integration
"""

import asyncio
import os
from collections.abc import Iterator
from typing import Annotated, Any

import httpx2 as httpx
import pytest
from alembic import command
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops import audit
from janus_ops.auth.breakglass import BreakglassError, set_password
from janus_ops.auth.deps import require_role
from janus_ops.auth.ldap import InvalidCredentials, LdapDirectory
from janus_ops.auth.sessions import COOKIE_NAME, Session
from janus_ops.config import Settings
from janus_ops.db import alembic_config
from janus_ops.main import create_app

from .conftest import make_settings

DB_URL = os.environ.get("JANUS_TEST_DATABASE_URL", "")
LDAP_URL = os.environ.get("JANUS_TEST_LDAP_URL", "")
LLDAP_HTTP = os.environ.get("JANUS_TEST_LLDAP_HTTP", "")
LLDAP_ADMIN_PASSWORD = os.environ.get("JANUS_TEST_LLDAP_ADMIN_PASSWORD", "")
BASE_DN = "dc=janus-ops,dc=local"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not (DB_URL and LDAP_URL), reason="test database / lldap not configured"),
]

PASSWORDS = {
    "alice": "alice-password-123",
    "bob": "bob-password-1234",
    "carol": "carol-password-12",
    "dave": "dave-password-123",
}
BREAKGLASS_PW = "break-glass-password-0001"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": DB_URL,
        "ldap_url": LDAP_URL,
        "ldap_base_dn": BASE_DN,
        "ldap_bind_dn": f"uid=janus-svc,ou=people,{BASE_DN}",
        "ldap_bind_password": "svc-password-1234",
        "ldap_timeout_s": 2,
    }
    values.update(overrides)
    return make_settings(**values)


def run_sql(stmt: str, params: dict[str, Any] | None = None) -> list[Any]:
    async def _go() -> list[Any]:
        engine = create_async_engine(DB_URL)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(stmt), params or {})
                return list(result.fetchall()) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def audit_actions(actor: str) -> list[tuple[str, dict[str, Any]]]:
    rows = run_sql(
        "SELECT action, payload FROM audit_event WHERE actor_name = :a ORDER BY id", {"a": actor}
    )
    return [(r[0], r[1]) for r in rows]


operator_router = APIRouter()


@operator_router.get("/test/operator-area")
async def operator_area(
    session: Annotated[Session, Depends(require_role("operator"))],
) -> dict[str, str]:
    return {"role": session.role}


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def make_client(**overrides: Any) -> TestClient:
    app = create_app(settings(**overrides), extra_routers=[operator_router])
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with make_client() as c:
        yield c


def login(c: TestClient, user: str, password: str | None = None) -> httpx.Response:
    return c.post(
        "/api/v1/auth/login",
        json={"username": user, "password": password or PASSWORDS[user]},
    )


# ---------------------------------------------------------------- roles from LDAP groups
@pytest.mark.parametrize(
    ("user", "role"), [("alice", "admin"), ("bob", "operator"), ("carol", "auditor")]
)
def test_login_maps_group_to_role(client: TestClient, user: str, role: str) -> None:
    r = login(client, user)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["username"] == user
    assert body["user"]["role"] == role
    assert body["user"]["method"] == "ldap"
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["role"] == role


def test_session_cookie_flags(client: TestClient) -> None:
    r = login(client, "alice")
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith(f"{COOKIE_NAME}=")
    for flag in ("httponly", "secure", "samesite=strict", "path=/", "max-age=28800"):
        assert flag in cookie


def test_token_is_stored_hashed(client: TestClient) -> None:
    login(client, "alice")
    token = client.cookies[COOKIE_NAME]
    rows = run_sql(
        "SELECT count(*) FROM user_session WHERE encode(token_hash, 'escape') LIKE :t",
        {"t": f"%{token}%"},
    )
    assert rows[0][0] == 0


def test_user_profile_synced_from_directory(client: TestClient) -> None:
    login(client, "carol")
    rows = run_sql(
        "SELECT ldap_dn, display_name, email, last_login_at IS NOT NULL FROM app_user"
        " WHERE username = 'carol'"
    )
    assert rows == [(f"uid=carol,ou=people,{BASE_DN}", "Carol Auditor", "carol@example.org", True)]


def test_user_without_janus_group_is_refused(client: TestClient) -> None:
    r = login(client, "dave")
    assert r.status_code == 403
    assert r.json() == {"detail": "no_role"}
    assert COOKIE_NAME not in client.cookies
    assert ("auth.login_failed", {"reason": "no_role"}) in audit_actions("dave")


# ---------------------------------------------------------------- failures
def test_wrong_password_and_unknown_user_look_identical(client: TestClient) -> None:
    wrong = login(client, "bob", "not-the-password")
    unknown = login(client, "nobody-here", "whatever-password")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "invalid_credentials"}
    assert ("auth.login_failed", {"reason": "invalid_credentials"}) in audit_actions("bob")


@pytest.mark.parametrize("username", ["*", "ali*", "*)(uid=alice", "alice)(|(uid=*"])
def test_filter_escaping_holds_without_api_validation(username: str) -> None:
    # Defense in depth: call the backend directly, bypassing the API username pattern
    directory = LdapDirectory(settings())
    with pytest.raises(InvalidCredentials):
        asyncio.run(directory.authenticate(username, PASSWORDS["alice"]))


def test_repeated_failures_are_rate_limited(client: TestClient) -> None:
    for _ in range(5):
        assert login(client, "carol", "wrong-password").status_code == 401
    r = login(client, "carol")  # even the right password is refused while locked
    assert r.status_code == 429
    assert r.json() == {"detail": "too_many_attempts"}
    assert int(r.headers["retry-after"]) > 0


# ---------------------------------------------------------------- session lifecycle
def test_logout_revokes_session(client: TestClient) -> None:
    login(client, "bob")
    token = client.cookies[COOKIE_NAME]
    r = client.post("/api/v1/auth/logout")
    assert r.status_code == 204
    assert (
        'janus_session=""' in r.headers["set-cookie"]
        or "max-age=0" in r.headers["set-cookie"].lower()
    )
    client.cookies.set(COOKIE_NAME, token)  # replay the old token
    assert client.get("/api/v1/auth/me").status_code == 401
    assert [a for a, _ in audit_actions("bob")][-2:] == ["auth.login", "auth.logout"]


def test_expired_session_rejected(client: TestClient) -> None:
    login(client, "bob")
    run_sql(
        "UPDATE user_session SET created_at = now() - interval '2 hours',"
        " expires_at = now() - interval '1 hour'"
        " WHERE revoked_at IS NULL AND user_id = (SELECT id FROM app_user WHERE username = 'bob')"
    )
    assert client.get("/api/v1/auth/me").status_code == 401


def test_idle_session_revoked() -> None:
    with make_client(session_idle_s=60) as c:
        login(c, "bob")
        run_sql(
            "UPDATE user_session SET last_seen_at = now() - interval '2 hours'"
            " WHERE revoked_at IS NULL"
            " AND user_id = (SELECT id FROM app_user WHERE username = 'bob')"
        )
        assert c.get("/api/v1/auth/me").status_code == 401
    rows = run_sql(
        "SELECT count(*) FROM user_session WHERE revoked_at IS NULL AND expires_at > now()"
        " AND user_id = (SELECT id FROM app_user WHERE username = 'bob')"
    )
    assert rows[0][0] == 0


def test_forged_token_rejected(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, "forged-token-value")
    assert client.get("/api/v1/auth/me").status_code == 401


# ---------------------------------------------------------------- RBAC
@pytest.mark.parametrize(("user", "status"), [("alice", 200), ("bob", 200), ("carol", 403)])
def test_require_role(client: TestClient, user: str, status: int) -> None:
    login(client, user)
    assert client.get("/api/v1/test/operator-area").status_code == status


def test_require_role_without_session(client: TestClient) -> None:
    assert client.get("/api/v1/test/operator-area").status_code == 401


# ---------------------------------------------------------------- AC-02b: group change
def _lldap(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    token = httpx.post(
        f"{LLDAP_HTTP}/auth/simple/login",
        json={"username": "admin", "password": LLDAP_ADMIN_PASSWORD},
        timeout=5,
    ).json()["token"]
    r = httpx.post(
        f"{LLDAP_HTTP}/api/graphql",
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    r.raise_for_status()
    body: dict[str, Any] = r.json()
    assert "errors" not in body, body
    return body["data"]


@pytest.mark.skipif(not LLDAP_HTTP, reason="lldap HTTP API not configured")
def test_role_follows_group_membership_at_next_login(client: TestClient) -> None:
    groups = _lldap("{ groups { id displayName } }")["groups"]
    operators = next(g["id"] for g in groups if g["displayName"] == "janus-operators")
    add = 'mutation($g: Int!) { addUserToGroup(userId: "dave", groupId: $g) { ok } }'
    remove = 'mutation($g: Int!) { removeUserFromGroup(userId: "dave", groupId: $g) { ok } }'

    assert login(client, "dave").status_code == 403
    _lldap(add, {"g": operators})
    try:
        r = login(client, "dave")
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "operator"
    finally:
        _lldap(remove, {"g": operators})
    assert login(client, "dave").status_code == 403


# ---------------------------------------------------------------- break-glass (AC-02c)
def test_breakglass_works_while_directory_is_down() -> None:
    assert asyncio.run(set_password(settings(), BREAKGLASS_PW)) in ("created", "rotated")
    # Directory unreachable: port 1 on loopback refuses immediately
    with make_client(ldap_url="ldap://127.0.0.1:1") as c:
        r = login(c, "bob")
        assert r.status_code == 503
        assert r.json() == {"detail": "directory_unavailable"}

        assert login(c, "breakglass", "wrong-password-0000000").status_code == 401
        r = login(c, "breakglass", BREAKGLASS_PW)
        assert r.status_code == 200, r.text
        assert r.json()["user"] == {
            "username": "breakglass",
            "display_name": None,
            "role": "admin",
            "method": "breakglass",
        }
        assert c.get("/api/v1/auth/me").json()["user"]["method"] == "breakglass"
    actions = audit_actions("breakglass")
    assert ("auth.login", {"method": "breakglass", "role": "admin"}) in [
        (a, {k: v for k, v in p.items() if k != "session_id"}) for a, p in actions
    ]


def test_breakglass_rotation_revokes_sessions() -> None:
    asyncio.run(set_password(settings(), BREAKGLASS_PW))
    with make_client() as c:
        assert login(c, "breakglass", BREAKGLASS_PW).status_code == 200
        assert asyncio.run(set_password(settings(), BREAKGLASS_PW + "-new")) == "rotated"
        assert c.get("/api/v1/auth/me").status_code == 401
        assert login(c, "breakglass", BREAKGLASS_PW).status_code == 401
        assert login(c, "breakglass", BREAKGLASS_PW + "-new").status_code == 200


def test_breakglass_name_clash_with_directory_user(client: TestClient) -> None:
    login(client, "alice")  # creates the LDAP-backed 'alice' profile
    with pytest.raises(BreakglassError, match="already belongs"):
        asyncio.run(set_password(settings(breakglass_username="alice"), BREAKGLASS_PW))


# ---------------------------------------------------------------- audit integrity
def test_audit_chain_intact_after_all_logins(client: TestClient) -> None:
    login(client, "alice")
    login(client, "bob", "wrong-password")

    async def _verify() -> int | None:
        engine = create_async_engine(DB_URL)
        try:
            async with engine.connect() as conn:
                return await audit.verify_chain(conn)
        finally:
            await engine.dispose()

    assert run_sql("SELECT count(*) FROM audit_event")[0][0] > 0
    assert asyncio.run(_verify()) is None
