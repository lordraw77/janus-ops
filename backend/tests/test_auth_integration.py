"""Sign-in, roles/permissions, user management and TOTP end to end.

Real PostgreSQL + TimescaleDB and a real lldap seeded from tests/fixtures/ldap.
Run with: make test/integration
"""

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx2 as httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops import audit
from janus_ops.auth import totp
from janus_ops.auth.breakglass import BreakglassError, set_password
from janus_ops.auth.ldap import InvalidCredentials, LdapDirectory
from janus_ops.auth.sessions import COOKIE_NAME
from janus_ops.config import Settings
from janus_ops.db import alembic_config
from janus_ops.main import create_app
from janus_ops.rbac import PERMISSIONS
from janus_ops.setup import SetupError, ensure_admin

from .conftest import make_settings

DB_URL = os.environ.get("JANUS_TEST_DATABASE_URL", "")
LDAP_URL = os.environ.get("JANUS_TEST_LDAP_URL", "")
LLDAP_HTTP = os.environ.get("JANUS_TEST_LLDAP_HTTP", "")
LLDAP_ADMIN_PASSWORD = os.environ.get("JANUS_TEST_LLDAP_ADMIN_PASSWORD", "")
BASE_DN = "dc=janus-ops,dc=local"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (DB_URL and LDAP_URL and LLDAP_HTTP), reason="test database / lldap not configured"
    ),
]

PASSWORDS = {
    "alice": "alice-password-123",
    "bob": "bob-password-1234",
    "carol": "carol-password-12",
    "dave": "dave-password-123",
}
BREAKGLASS_PW = "break-glass-password-0001"
NEW_PW = "fresh-password-0042"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": DB_URL,
        "ldap_url": LDAP_URL,
        "ldap_base_dn": BASE_DN,
        "ldap_bind_dn": f"uid=janus-svc,ou=people,{BASE_DN}",
        "ldap_bind_password": "svc-password-1234",
        "ldap_timeout_s": 2,
        "lldap_http_url": LLDAP_HTTP,
        "ldap_manager_password": "manager-password-1234",
        "secret_key": "ab" * 32,
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


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def make_client(**overrides: Any) -> TestClient:
    return TestClient(create_app(settings(**overrides)), base_url="https://testserver")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with make_client() as c:
        yield c


def login(c: TestClient, user: str, password: str | None = None) -> httpx.Response:
    return c.post(
        "/api/v1/auth/login",
        json={"username": user, "password": password or PASSWORDS[user]},
    )


@contextmanager
def signed_in(user: str, password: str | None = None) -> Iterator[TestClient]:
    with make_client() as c:
        r = login(c, user, password)
        assert r.status_code == 200, r.text
        assert r.json() == {"mfa_required": False}
        yield c


def me(c: TestClient) -> dict[str, Any]:
    r = c.get("/api/v1/auth/me")
    assert r.status_code == 200, r.text
    user: dict[str, Any] = r.json()["user"]
    return user


# ---------------------------------------------------------------- roles from LDAP groups
@pytest.mark.parametrize(
    ("user", "roles", "permissions"),
    [
        ("alice", ["admin"], sorted(PERMISSIONS)),
        ("bob", ["operator"], []),
        ("carol", ["auditor"], ["audit.read", "users.read"]),
    ],
)
def test_login_roles_and_permissions(
    client: TestClient, user: str, roles: list[str], permissions: list[str]
) -> None:
    r = login(client, user)
    assert r.status_code == 200, r.text
    assert r.json() == {"mfa_required": False}
    info = me(client)
    assert info["username"] == user
    assert info["roles"] == roles
    assert info["permissions"] == permissions
    assert info["method"] == "ldap"
    assert info["totp_enabled"] is False


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


@pytest.mark.parametrize("username", ["*", "ali*", "*)(uid=alice", "alice)(|(uid=*"])
def test_filter_escaping_holds_without_api_validation(username: str) -> None:
    directory = LdapDirectory(settings())
    with pytest.raises(InvalidCredentials):
        asyncio.run(directory.authenticate(username, PASSWORDS["alice"]))


def test_repeated_failures_are_rate_limited(client: TestClient) -> None:
    for _ in range(5):
        assert login(client, "carol", "wrong-password").status_code == 401
    r = login(client, "carol")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


# ---------------------------------------------------------------- session lifecycle
def test_logout_revokes_session(client: TestClient) -> None:
    login(client, "bob")
    token = client.cookies[COOKIE_NAME]
    assert client.post("/api/v1/auth/logout").status_code == 204
    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/api/v1/auth/me").status_code == 401


def test_expired_session_rejected(client: TestClient) -> None:
    login(client, "bob")
    run_sql(
        "UPDATE user_session SET created_at = now() - interval '2 hours',"
        " expires_at = now() - interval '1 hour' WHERE revoked_at IS NULL"
        " AND user_id = (SELECT id FROM app_user WHERE username = 'bob')"
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


def test_forged_token_rejected(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, "forged-token-value")
    assert client.get("/api/v1/auth/me").status_code == 401


# ---------------------------------------------------------------- audit access is configurable
@pytest.mark.parametrize(("user", "status"), [("alice", 200), ("carol", 200), ("bob", 403)])
def test_default_audit_access(user: str, status: int) -> None:
    with signed_in(user) as c:
        assert c.get("/api/v1/audit").status_code == status


def test_granting_audit_read_applies_to_live_sessions() -> None:
    with signed_in("alice") as admin, signed_in("bob") as bob:
        assert bob.get("/api/v1/audit").status_code == 403
        r = admin.patch("/api/v1/roles/operator", json={"permissions": ["audit.read"]})
        assert r.status_code == 204, r.text
        try:
            assert bob.get("/api/v1/audit").status_code == 200  # same session, no re-login
            assert "audit.read" in me(bob)["permissions"]
        finally:
            admin.patch("/api/v1/roles/operator", json={"permissions": []})
        assert bob.get("/api/v1/audit").status_code == 403
    assert ("roles.updated", {"permissions": ["audit.read"]}) in audit_actions("alice")


def test_audit_listing_filters_and_pages() -> None:
    with signed_in("carol") as c:
        page = c.get("/api/v1/audit", params={"limit": 2, "action": "auth.login"}).json()
        assert len(page["events"]) == 2
        assert all(e["action"].startswith("auth.login") for e in page["events"])
        assert page["next_before_id"] == page["events"][-1]["id"]
        older = c.get(
            "/api/v1/audit",
            params={"limit": 2, "action": "auth.login", "before_id": page["next_before_id"]},
        ).json()
        assert all(e["id"] < page["next_before_id"] for e in older["events"])
        mine = c.get("/api/v1/audit", params={"actor": "CAROL"}).json()["events"]
        assert mine and all(e["actor_name"] == "carol" for e in mine)
        status = c.get("/api/v1/audit/verify").json()
        assert status["intact"] is True and status["events"] > 0


# ---------------------------------------------------------------- roles management
def test_role_rules() -> None:
    with signed_in("alice") as admin:
        roles = admin.get("/api/v1/roles").json()
        assert roles["managed"] is True
        assert set(roles["permissions"]) == set(PERMISSIONS)
        by_name = {r["name"]: r for r in roles["roles"]}
        assert by_name["admin"]["permissions"] == sorted(PERMISSIONS)
        assert by_name["auditor"]["ldap_group"] == "janus-auditors"

        assert admin.patch("/api/v1/roles/admin", json={"permissions": []}).status_code == 400
        assert admin.delete("/api/v1/roles/operator").status_code == 400
        r = admin.post("/api/v1/roles", json={"name": "x", "permissions": []})
        assert r.status_code == 422  # name too short
        r = admin.post("/api/v1/roles", json={"name": "bad-perm", "permissions": ["root.all"]})
        assert r.status_code == 422
        r = admin.post("/api/v1/roles", json={"name": "dup", "ldap_group": "janus-admins"})
        assert r.status_code == 409


def test_custom_role_end_to_end() -> None:
    with signed_in("alice") as admin:
        r = admin.post(
            "/api/v1/roles",
            json={"name": "netops", "description": "Network team", "permissions": ["audit.read"]},
        )
        assert r.status_code == 201, r.text
        r = admin.post(
            "/api/v1/users",
            json={
                "id": "nina",
                "display_name": "Nina Net",
                "email": "nina@example.org",
                "password": NEW_PW,
                "roles": ["netops"],
            },
        )
        assert r.status_code == 201, r.text
        with signed_in("nina", NEW_PW) as nina:
            assert me(nina)["roles"] == ["netops"]
            assert nina.get("/api/v1/audit").status_code == 200
            assert nina.get("/api/v1/users").status_code == 403
        assert admin.delete("/api/v1/roles/netops").status_code == 204
        with make_client() as c:
            assert login(c, "nina", NEW_PW).json() == {"detail": "no_role"}
        admin.delete("/api/v1/users/nina")


def test_union_of_permissions_for_several_roles() -> None:
    with signed_in("alice") as admin:
        r = admin.post(
            "/api/v1/users",
            json={
                "id": "olga",
                "display_name": "Olga Two-Hats",
                "email": "olga@example.org",
                "password": NEW_PW,
                "roles": ["operator", "auditor"],
            },
        )
        assert r.status_code == 201, r.text
        with signed_in("olga", NEW_PW) as olga:
            info = me(olga)
            assert info["roles"] == ["auditor", "operator"]
            assert info["permissions"] == ["audit.read", "users.read"]
        admin.delete("/api/v1/users/olga")


# ---------------------------------------------------------------- users management
def test_user_lifecycle() -> None:
    with signed_in("alice") as admin:
        body = {
            "id": "erin",
            "display_name": "Erin Ops",
            "email": "erin@example.org",
            "password": NEW_PW,
            "roles": ["operator"],
        }
        assert admin.post("/api/v1/users", json=body).status_code == 201
        assert admin.post("/api/v1/users", json=body).status_code == 409

        users = {u["id"]: u for u in admin.get("/api/v1/users").json()["users"]}
        assert users["erin"]["roles"] == ["operator"]
        assert users["erin"]["display_name"] == "Erin Ops"
        assert "janus-svc" not in users and "janus-manager" not in users and "admin" not in users

        with signed_in("erin", NEW_PW) as erin:
            # password reset: old password stops working, open sessions end
            r = admin.post("/api/v1/users/erin/password", json={"password": NEW_PW + "-2"})
            assert r.status_code == 204
            assert erin.get("/api/v1/auth/me").status_code == 401
        with make_client() as c:
            assert login(c, "erin", NEW_PW).status_code == 401

        with signed_in("erin", NEW_PW + "-2") as erin:
            # disable: live session ends, login refused; enable again
            assert admin.patch("/api/v1/users/erin", json={"disabled": True}).status_code == 204
            assert erin.get("/api/v1/auth/me").status_code == 401
        with make_client() as c:
            assert login(c, "erin", NEW_PW + "-2").json() == {"detail": "account_disabled"}
        assert admin.patch("/api/v1/users/erin", json={"disabled": False}).status_code == 204

        # profile and roles
        r = admin.patch(
            "/api/v1/users/erin", json={"display_name": "Erin Audit", "roles": ["auditor"]}
        )
        assert r.status_code == 204, r.text
        with signed_in("erin", NEW_PW + "-2") as c:
            assert me(c)["roles"] == ["auditor"]
            assert me(c)["display_name"] == "Erin Audit"

        assert admin.delete("/api/v1/users/erin").status_code == 204
        with make_client() as c:
            assert login(c, "erin", NEW_PW + "-2").status_code == 401
    actions = [a for a, _ in audit_actions("alice")]
    for action in ("users.created", "users.password_reset", "users.updated", "users.deleted"):
        assert action in actions


def test_create_user_validation() -> None:
    with signed_in("alice") as admin:
        base = {"display_name": "X", "email": "x@example.org", "password": NEW_PW}
        assert admin.post("/api/v1/users", json={**base, "id": "janus-svc"}).status_code == 409
        assert admin.post("/api/v1/users", json={**base, "id": "Bad Name"}).status_code == 422
        r = admin.post("/api/v1/users", json={**base, "id": "shorty", "password": "short"})
        assert r.status_code == 422
        r = admin.post("/api/v1/users", json={**base, "id": "ghost", "roles": ["nope"]})
        assert r.status_code == 422


def test_read_only_viewer_cannot_change_users() -> None:
    with signed_in("carol") as auditor:
        assert auditor.get("/api/v1/users").status_code == 200
        assert auditor.get("/api/v1/roles").status_code == 200
        r = auditor.post(
            "/api/v1/users",
            json={"id": "eve", "display_name": "E", "email": "e@example.org", "password": NEW_PW},
        )
        assert r.status_code == 403
        assert auditor.patch("/api/v1/roles/auditor", json={"description": "x"}).status_code == 403


def test_user_managers_cannot_escalate_to_admin() -> None:
    with signed_in("alice") as admin:
        admin.post("/api/v1/roles", json={"name": "usermgr", "permissions": ["users.manage"]})
        admin.post(
            "/api/v1/users",
            json={
                "id": "frank",
                "display_name": "Frank",
                "email": "frank@example.org",
                "password": NEW_PW,
                "roles": ["usermgr"],
            },
        )
        try:
            with signed_in("frank", NEW_PW) as frank:
                r = frank.patch("/api/v1/users/frank", json={"roles": ["usermgr", "admin"]})
                assert r.json() == {"detail": "admin_only"}
                r = frank.post("/api/v1/users/alice/password", json={"password": NEW_PW})
                assert r.json() == {"detail": "admin_only"}
                r = frank.post(
                    "/api/v1/users",
                    json={
                        "id": "gina",
                        "display_name": "G",
                        "email": "g@example.org",
                        "password": NEW_PW,
                        "roles": ["admin"],
                    },
                )
                assert r.json() == {"detail": "admin_only"}
                r = frank.post(
                    "/api/v1/users",
                    json={
                        "id": "gina",
                        "display_name": "G",
                        "email": "g@example.org",
                        "password": NEW_PW,
                        "roles": ["operator"],
                    },
                )
                assert r.status_code == 201
        finally:
            admin.delete("/api/v1/users/gina")
            admin.delete("/api/v1/users/frank")
            admin.delete("/api/v1/roles/usermgr")


def test_admins_cannot_lock_themselves_out() -> None:
    with signed_in("alice") as admin:
        assert admin.delete("/api/v1/users/alice").json() == {"detail": "cannot_delete_self"}
        r = admin.patch("/api/v1/users/alice", json={"disabled": True})
        assert r.json() == {"detail": "cannot_disable_self"}
        r = admin.patch("/api/v1/users/alice", json={"roles": ["operator"]})
        assert r.json() == {"detail": "cannot_remove_own_admin"}
        assert me(admin)["roles"] == ["admin"]


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


def test_role_follows_group_membership_at_next_login(client: TestClient) -> None:
    groups = _lldap("{ groups { id displayName } }")["groups"]
    operators = next(g["id"] for g in groups if g["displayName"] == "janus-operators")
    add = 'mutation($g: Int!) { addUserToGroup(userId: "dave", groupId: $g) { ok } }'
    remove = 'mutation($g: Int!) { removeUserFromGroup(userId: "dave", groupId: $g) { ok } }'
    assert login(client, "dave").status_code == 403
    _lldap(add, {"g": operators})
    try:
        assert login(client, "dave").status_code == 200
        assert me(client)["roles"] == ["operator"]
    finally:
        _lldap(remove, {"g": operators})
    assert login(client, "dave").status_code == 403


# ---------------------------------------------------------------- TOTP (optional)
def _enrol(c: TestClient) -> tuple[str, list[str]]:
    status = c.get("/api/v1/auth/totp").json()
    assert status == {"enabled": False, "recovery_codes_left": 0, "available": True}
    setup = c.post("/api/v1/auth/totp/setup").json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/Janus-Ops")
    assert "<svg" in setup["qr_svg"]
    secret = setup["secret"]
    assert c.post("/api/v1/auth/totp/enable", json={"code": "000000"}).status_code == 400
    code = totp.code_at(secret, totp.current_step() - 1)
    r = c.post("/api/v1/auth/totp/enable", json={"code": code})
    assert r.status_code == 200, r.text
    codes: list[str] = r.json()["recovery_codes"]
    assert len(codes) == 10
    assert c.post("/api/v1/auth/totp/setup").json() == {"detail": "totp_already_enabled"}
    return secret, codes


def test_totp_is_optional_and_full_flow() -> None:
    with signed_in("bob") as c:
        secret, codes = _enrol(c)
        assert me(c)["totp_enabled"] is True
    assert ("auth.totp_enabled", {}) in audit_actions("bob")

    with make_client() as c:
        # password alone now yields a pending session that cannot be used
        r = login(c, "bob")
        assert r.json() == {"mfa_required": True}
        assert "max-age=300" in r.headers["set-cookie"].lower()
        pending = c.cookies[COOKIE_NAME]
        assert c.get("/api/v1/auth/me").status_code == 401
        assert c.get("/api/v1/audit").status_code == 401
        assert c.post("/api/v1/auth/login/totp", json={"code": "000000"}).json() == {
            "detail": "invalid_code"
        }
        code = totp.code_at(secret, totp.current_step())
        r = c.post("/api/v1/auth/login/totp", json={"code": code})
        assert r.json() == {"mfa_required": False}
        assert c.cookies[COOKIE_NAME] != pending  # new token after the second factor
        assert me(c)["totp_enabled"] is True
        # the same code cannot be replayed
        c.cookies.clear()
        login(c, "bob")
        r = c.post("/api/v1/auth/login/totp", json={"code": code})
        assert r.json() == {"detail": "invalid_code"}

    with make_client() as c:  # recovery code, single use
        login(c, "bob")
        assert c.post("/api/v1/auth/login/totp", json={"code": codes[0]}).status_code == 200
        assert c.get("/api/v1/auth/totp").json()["recovery_codes_left"] == 9
        c.cookies.clear()
        login(c, "bob")
        assert c.post("/api/v1/auth/login/totp", json={"code": codes[0]}).status_code == 401
        assert c.post("/api/v1/auth/login/totp", json={"code": codes[1]}).status_code == 200
        # self-service disable needs a valid factor
        assert c.post("/api/v1/auth/totp/disable", json={"code": "000000"}).status_code == 400
        assert c.post("/api/v1/auth/totp/disable", json={"code": codes[2]}).status_code == 204

    with make_client() as c:
        assert login(c, "bob").json() == {"mfa_required": False}


def test_totp_step_locks_after_five_wrong_codes() -> None:
    with signed_in("carol") as c:
        _enrol(c)
    try:
        with make_client() as c:
            login(c, "carol")
            for _ in range(4):
                assert c.post("/api/v1/auth/login/totp", json={"code": "000000"}).json() == {
                    "detail": "invalid_code"
                }
            r = c.post("/api/v1/auth/login/totp", json={"code": "000000"})
            assert r.json() == {"detail": "mfa_locked"}
            r = c.post("/api/v1/auth/login/totp", json={"code": "111111"})
            assert r.status_code == 401  # pending session is gone
    finally:
        with signed_in("alice") as admin:  # lost phone: admin reset
            assert admin.delete("/api/v1/users/carol/totp").status_code == 204
    with make_client() as c:
        assert login(c, "carol").json() == {"mfa_required": False}
    assert ("users.totp_reset", {}) in audit_actions("alice")


def test_totp_unavailable_without_secret_key() -> None:
    with make_client(secret_key=None) as c:
        login(c, "alice")
        assert c.get("/api/v1/auth/totp").json()["available"] is False
        assert c.post("/api/v1/auth/totp/setup").json() == {"detail": "totp_unavailable"}


# ---------------------------------------------------------------- initial admin from .env
def test_initial_admin_created_once() -> None:
    s = settings(admin_username="root-admin", admin_password="initial-admin-pass-01")
    assert asyncio.run(ensure_admin(s)) == "created"
    with signed_in("root-admin", "initial-admin-pass-01") as c:
        assert me(c)["roles"] == ["admin"]
        # password changed in the UI is not overwritten by a later setup run
        c.post("/api/v1/users/root-admin/password", json={"password": "changed-in-ui-0001"})
    assert asyncio.run(ensure_admin(s)) == "exists"
    with make_client() as c:
        assert login(c, "root-admin", "changed-in-ui-0001").status_code == 200
    assert asyncio.run(ensure_admin(settings())) == "skipped"
    with pytest.raises(SetupError):
        asyncio.run(ensure_admin(settings(admin_username="x2", admin_password="short")))


# ---------------------------------------------------------------- break-glass (AC-02c)
def test_breakglass_works_while_directory_is_down() -> None:
    assert asyncio.run(set_password(settings(), BREAKGLASS_PW)) in ("created", "rotated")
    with make_client(ldap_url="ldap://127.0.0.1:1", lldap_http_url=None) as c:
        assert login(c, "bob").json() == {"detail": "directory_unavailable"}
        assert login(c, "breakglass", "wrong-password-0000000").status_code == 401
        assert login(c, "breakglass", BREAKGLASS_PW).json() == {"mfa_required": False}
        info = me(c)
        assert info["roles"] == ["admin"] and info["method"] == "breakglass"
        assert c.get("/api/v1/audit").status_code == 200


def test_breakglass_rotation_revokes_sessions() -> None:
    asyncio.run(set_password(settings(), BREAKGLASS_PW))
    with make_client() as c:
        assert login(c, "breakglass", BREAKGLASS_PW).status_code == 200
        assert asyncio.run(set_password(settings(), BREAKGLASS_PW + "-new")) == "rotated"
        assert c.get("/api/v1/auth/me").status_code == 401
        assert login(c, "breakglass", BREAKGLASS_PW + "-new").status_code == 200


def test_breakglass_name_clash_with_directory_user(client: TestClient) -> None:
    login(client, "alice")
    with pytest.raises(BreakglassError, match="already belongs"):
        asyncio.run(set_password(settings(breakglass_username="alice"), BREAKGLASS_PW))


# ---------------------------------------------------------------- audit integrity
def test_audit_chain_intact_after_everything() -> None:
    async def _verify() -> int | None:
        engine = create_async_engine(DB_URL)
        try:
            async with engine.connect() as conn:
                return await audit.verify_chain(conn)
        finally:
            await engine.dispose()

    assert run_sql("SELECT count(*) FROM audit_event")[0][0] > 20
    assert asyncio.run(_verify()) is None
