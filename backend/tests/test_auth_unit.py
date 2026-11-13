"""Auth building blocks that need no database or directory."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from janus_ops.auth.ldap import group_cn, role_for_groups
from janus_ops.auth.passwords import hash_password, verify_password
from janus_ops.auth.ratelimit import LoginRateLimiter

from .conftest import make_settings

GOOD_PW = "correct horse battery staple"


# ---------------------------------------------------------------- passwords
def test_password_round_trip() -> None:
    encoded = hash_password(GOOD_PW)
    assert encoded.startswith("scrypt$")
    assert GOOD_PW not in encoded
    assert verify_password(GOOD_PW, encoded)
    assert not verify_password(GOOD_PW + "x", encoded)


def test_password_hashes_are_salted() -> None:
    assert hash_password(GOOD_PW) != hash_password(GOOD_PW)


def test_short_password_rejected() -> None:
    with pytest.raises(ValueError, match="at least 16"):
        hash_password("short")


@pytest.mark.parametrize("encoded", ["", "plain", "bcrypt$x$y$z$a$b", "scrypt$1$2$3$!!$!!"])
def test_malformed_hash_never_verifies(encoded: str) -> None:
    assert verify_password(GOOD_PW, encoded) is False


# ---------------------------------------------------------------- rate limiter
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_rate_limiter_blocks_after_max_failures_then_recovers() -> None:
    clock = FakeClock()
    rl = LoginRateLimiter(max_failures=3, window_s=60, clock=clock)
    for _ in range(3):
        assert rl.retry_after("bob") == 0
        rl.record_failure("bob")
    assert 1 <= rl.retry_after("bob") <= 61
    assert rl.retry_after("alice") == 0  # keys are independent
    clock.now += 61
    assert rl.retry_after("bob") == 0


def test_rate_limiter_reset_on_success() -> None:
    rl = LoginRateLimiter(max_failures=2, window_s=60, clock=FakeClock())
    rl.record_failure("bob")
    rl.record_failure("bob")
    assert rl.retry_after("bob") > 0
    rl.reset("bob")
    assert rl.retry_after("bob") == 0


def test_rate_limiter_memory_is_bounded() -> None:
    rl = LoginRateLimiter(max_failures=5, window_s=60, clock=FakeClock(), max_keys=2000)
    for i in range(5000):
        rl.record_failure(f"spray-{i}")
    assert len(rl._failures) <= 2000


# ---------------------------------------------------------------- group -> role mapping
@pytest.mark.parametrize(
    ("dn", "cn"),
    [
        ("cn=janus-admins,ou=groups,dc=x", "janus-admins"),
        ("CN=Janus-Operators,OU=Groups,DC=corp,DC=local", "janus-operators"),
        ("uid=alice,ou=people,dc=x", None),
        ("not a dn", None),
    ],
)
def test_group_cn(dn: str, cn: str | None) -> None:
    assert group_cn(dn) == cn


@pytest.mark.parametrize(
    ("groups", "role"),
    [
        ({"janus-auditors", "janus-admins"}, "admin"),
        ({"janus-auditors", "janus-operators"}, "operator"),
        ({"janus-auditors"}, "auditor"),
        ({"developers"}, None),
        (set(), None),
    ],
)
def test_highest_role_wins(groups: set[str], role: str | None) -> None:
    assert role_for_groups(frozenset(groups), make_settings()) == role


def test_group_names_are_configurable() -> None:
    settings = make_settings(ldap_group_admin="Domain-Admins")
    assert role_for_groups(frozenset({"domain-admins"}), settings) == "admin"


# ---------------------------------------------------------------- LDAP settings
LDAP_OK = {
    "ldap_url": "ldap://ldap:3890",
    "ldap_base_dn": "dc=x",
    "ldap_bind_dn": "uid=svc,ou=people,dc=x",
    "ldap_bind_password": "svc-secret",
}


def test_ldap_settings_complete() -> None:
    s = make_settings(**LDAP_OK)
    assert s.ldap_users_dn == "ou=people,dc=x"
    assert "svc-secret" not in repr(s)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"ldap_bind_password": None}, "JANUS_LDAP_BIND_PASSWORD"),
        ({"ldap_base_dn": ""}, "JANUS_LDAP_BASE_DN"),
        ({"ldap_url": "http://ldap"}, "ldap:// or ldaps://"),
        ({"ldap_user_filter": "(uid=*)"}, "{username}"),
    ],
)
def test_ldap_settings_rejected(override: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(**{**LDAP_OK, **override})


# ---------------------------------------------------------------- HTTP surface (no DB needed)
@pytest.mark.parametrize(
    "username", ["*", "admin)(uid=*", "a b", "", "x" * 65, "alice\x00", "cn=admin"]
)
def test_login_rejects_malformed_username(client: TestClient, username: str) -> None:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": "x"})
    assert r.status_code == 422


def test_login_rejects_empty_password(client: TestClient) -> None:
    r = client.post("/api/v1/auth/login", json={"username": "alice", "password": ""})
    assert r.status_code == 422


@pytest.mark.parametrize("origin", ["https://evil.example", "null"])
def test_cross_origin_write_blocked(client: TestClient, origin: str) -> None:
    r = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "x"},
        headers={"Origin": origin},
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "cross_origin"}


def test_cross_origin_referer_blocked(client: TestClient) -> None:
    r = client.post("/api/v1/auth/logout", headers={"Referer": "https://evil.example/page"})
    assert r.status_code == 403


def test_same_origin_write_reaches_handler(client: TestClient) -> None:
    # Same host as the request: passes the guard and fails validation instead
    r = client.post(
        "/api/v1/auth/login", json={"username": "*"}, headers={"Origin": "http://testserver"}
    )
    assert r.status_code == 422


def test_cross_origin_read_allowed(client: TestClient) -> None:
    r = client.get("/api/v1/health", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200


def test_me_and_logout_require_session(client: TestClient) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/logout").status_code == 401
