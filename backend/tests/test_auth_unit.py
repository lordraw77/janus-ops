"""Auth building blocks that need no database or directory."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from janus_ops.auth import totp
from janus_ops.auth.ldap import group_cn
from janus_ops.auth.passwords import hash_password, verify_password
from janus_ops.auth.ratelimit import LoginRateLimiter
from janus_ops.crypto import DecryptionError, SecretBox
from janus_ops.rbac import PERMISSIONS

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


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/users"),
        ("post", "/api/v1/users"),
        ("patch", "/api/v1/users/alice"),
        ("delete", "/api/v1/users/alice"),
        ("post", "/api/v1/users/alice/password"),
        ("delete", "/api/v1/users/alice/totp"),
        ("get", "/api/v1/roles"),
        ("post", "/api/v1/roles"),
        ("patch", "/api/v1/roles/operator"),
        ("delete", "/api/v1/roles/x"),
        ("get", "/api/v1/audit"),
        ("get", "/api/v1/audit/verify"),
        ("get", "/api/v1/auth/totp"),
        ("post", "/api/v1/auth/totp/setup"),
        ("post", "/api/v1/auth/login/totp"),
    ],
)
def test_admin_and_totp_endpoints_require_session(
    client: TestClient, method: str, path: str
) -> None:
    kwargs = {"json": {"code": "123456"}} if method in ("post", "patch") else {}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code in (401, 422), (path, r.status_code)
    if r.status_code == 422:  # body validated first: retry with a valid-looking body
        r = getattr(client, method)(path, json={"code": "123456", "password": "x" * 12})
    assert r.status_code in (401, 422)


# ---------------------------------------------------------------- TOTP (RFC 6238)
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of b"12345678901234567890"


@pytest.mark.parametrize(
    ("unix_time", "code"),
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
    ],
)
def test_totp_rfc6238_vectors(unix_time: int, code: str) -> None:
    assert totp.code_at(RFC_SECRET, totp.current_step(unix_time)) == code


def test_totp_accepts_one_step_of_drift_only() -> None:
    now = 1_700_000_000.0
    step = totp.current_step(now)
    for delta in (-1, 0, 1):
        assert totp.verify(
            RFC_SECRET, totp.code_at(RFC_SECRET, step + delta), last_step=None, now=now
        )
    for delta in (-2, 2):
        code = totp.code_at(RFC_SECRET, step + delta)
        assert totp.verify(RFC_SECRET, code, last_step=None, now=now) is None


def test_totp_rejects_replay() -> None:
    now = 1_700_000_000.0
    step = totp.current_step(now)
    code = totp.code_at(RFC_SECRET, step)
    assert totp.verify(RFC_SECRET, code, last_step=None, now=now) == step
    assert totp.verify(RFC_SECRET, code, last_step=step, now=now) is None


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 34 5"])
def test_totp_rejects_malformed_codes(code: str) -> None:
    assert totp.verify(RFC_SECRET, code, last_step=None) is None


def test_totp_secret_and_uri() -> None:
    secret = totp.new_secret()
    assert len(secret) == 32 and secret.isalnum()
    uri = totp.provisioning_uri(secret, "alice")
    assert uri.startswith("otpauth://totp/Janus-Ops%3Aalice?")
    assert f"secret={secret}" in uri and "issuer=Janus-Ops" in uri


def test_totp_qr_is_local_svg() -> None:
    svg = totp.qr_svg(totp.provisioning_uri(totp.new_secret(), "alice"))
    assert svg.lstrip().startswith("<?xml") or "<svg" in svg[:200]
    assert "http://" not in svg.replace("http://www.w3.org", "")  # no external resources


def test_recovery_codes_unique_and_hashed_case_insensitively() -> None:
    codes = totp.new_recovery_codes()
    assert len(codes) == len(set(codes)) == totp.RECOVERY_CODES
    assert totp.hash_recovery_code(codes[0]) == totp.hash_recovery_code(f" {codes[0].upper()} ")


# ---------------------------------------------------------------- secrets at rest
KEY = "11" * 32


def test_secretbox_round_trip_bound_to_owner() -> None:
    box = SecretBox(KEY)
    blob = box.encrypt(b"seed", b"user-1")
    assert b"seed" not in blob
    assert box.decrypt(blob, b"user-1") == b"seed"
    with pytest.raises(DecryptionError):
        box.decrypt(blob, b"user-2")  # copied to another user's row
    with pytest.raises(DecryptionError):
        SecretBox("22" * 32).decrypt(blob, b"user-1")  # wrong key
    with pytest.raises(DecryptionError):
        box.decrypt(blob[:-1] + bytes([blob[-1] ^ 1]), b"user-1")  # tampered


@pytest.mark.parametrize("key", ["", "zz" * 32, "11" * 16, "11" * 33])
def test_secret_key_setting_validated(key: str) -> None:
    with pytest.raises(ValidationError, match="64 hexadecimal"):
        make_settings(secret_key=key)


def test_manager_settings_require_ldap() -> None:
    with pytest.raises(ValidationError, match="JANUS_LDAP_MANAGER_PASSWORD"):
        make_settings(**LDAP_OK, lldap_http_url="http://ldap:17170")
    s = make_settings(**LDAP_OK, lldap_http_url="http://ldap:17170", ldap_manager_password="m")
    assert s.directory_managed
    assert s.user_dn("zoe") == "uid=zoe,ou=people,dc=x"


def test_permission_catalogue_is_well_formed() -> None:
    assert {"audit.read", "users.read", "users.manage", "roles.manage"} <= set(PERMISSIONS)
    assert all(k.count(".") == 1 and k == k.lower() for k in PERMISSIONS)
