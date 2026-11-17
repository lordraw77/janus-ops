"""External Active Directory / LDAP (FR-07): login attribute, user filter, referrals, StartTLS and
nested groups. ldap3's in-memory mock server stands in for the directory; no network."""

import asyncio
from typing import Any

import pytest
from ldap3 import MOCK_SYNC, Connection, Server

from janus_ops.auth.ldap import (
    IN_CHAIN,
    DirectoryUnavailable,
    InvalidCredentials,
    LdapDirectory,
)
from janus_ops.config import Settings

BASE = "DC=corp,DC=local"
SVC = f"CN=svc-janus,CN=Users,{BASE}"
ANNA = f"CN=Anna Rossi,CN=Users,{BASE}"
OPS = f"CN=Ops Team,OU=Groups,{BASE}"
ADMINS = f"CN=Server Admins,OU=Groups,{BASE}"


def _settings(**kw: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "ldap_url": "ldaps://dc.corp.local",
        "ldap_base_dn": BASE,
        "ldap_user_base_dn": f"CN=Users,{BASE}",
        "ldap_bind_dn": SVC,
        "ldap_bind_password": "svc-pw",
        "ldap_username_attr": "sAMAccountName",
        "ldap_user_filter": (
            "(&(objectClass=user)(|(sAMAccountName={username})(userPrincipalName={username})))"
        ),
    }
    values.update(kw)
    return Settings(**values)


def _directory(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> tuple[LdapDirectory, list[Any]]:
    """A directory on an in-memory server. `seen` collects the connections it opened."""
    server = Server("mock")
    seen: list[Any] = []

    def connection(self: LdapDirectory, user: str, password: str) -> Connection:
        conn = Connection(
            server, user=user, password=password, client_strategy=MOCK_SYNC, read_only=True
        )
        seen.append(conn)
        return conn

    monkeypatch.setattr(LdapDirectory, "_connection", connection)
    directory = LdapDirectory(_settings(**kw))
    seed = Connection(server, user=SVC, password="svc-pw", client_strategy=MOCK_SYNC)
    seed.strategy.add_entry(SVC, {"userPassword": "svc-pw", "objectClass": "user"})
    seed.strategy.add_entry(
        ANNA,
        {
            "userPassword": "anna-pw",
            "objectClass": "user",
            "sAMAccountName": "Anna",
            "userPrincipalName": "anna@corp.local",
            "cn": "Anna Rossi",
            "displayName": "Anna Rossi",
            "mail": "anna@corp.local",
            "memberOf": [OPS],
        },
    )
    seed.strategy.add_entry(
        f"CN=Other,CN=Users,{BASE}",
        {"userPassword": "x", "objectClass": "user", "sAMAccountName": "other"},
    )
    return directory, seen


def _login(directory: LdapDirectory, user: str, password: str) -> Any:
    return asyncio.run(directory.authenticate(user, password))


def test_login_with_the_sam_account_name_or_the_upn(monkeypatch: pytest.MonkeyPatch) -> None:
    directory, _ = _directory(monkeypatch)
    for name in ("Anna", "anna@corp.local"):
        user = _login(directory, name, "anna-pw")
        assert user.username == "anna"  # the attribute, lower-cased
        assert user.display_name == "Anna Rossi" and user.email == "anna@corp.local"
        assert user.groups == frozenset({"ops team"})
        assert user.dn == ANNA


def test_wrong_password_and_unknown_user_look_the_same(monkeypatch: pytest.MonkeyPatch) -> None:
    directory, _ = _directory(monkeypatch)
    for name, password in (("Anna", "nope"), ("ghost", "anna-pw"), ("Anna", "")):
        with pytest.raises(InvalidCredentials):
            _login(directory, name, password)


def test_a_filter_that_matches_two_entries_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    directory, _ = _directory(
        monkeypatch, ldap_user_filter="(|(objectClass=user)(sAMAccountName={username}))"
    )
    with pytest.raises(InvalidCredentials):
        _login(directory, "Anna", "anna-pw")


def test_login_input_cannot_change_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    directory, _ = _directory(monkeypatch)
    with pytest.raises(InvalidCredentials):
        _login(directory, "*)(sAMAccountName=*", "anna-pw")


def test_nested_groups_use_the_in_chain_rule_and_merge_with_memberof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, _ = _directory(monkeypatch, ldap_nested_groups=True)
    asked: list[str] = []

    def nested(self: LdapDirectory, svc: Connection, user_dn: str) -> set[str]:
        asked.append(user_dn)
        return {"server admins", "ops team"}

    monkeypatch.setattr(LdapDirectory, "_nested_groups", nested)
    user = _login(directory, "Anna", "anna-pw")
    assert asked == [ANNA]
    assert user.groups == frozenset({"ops team", "server admins"})


def test_the_nested_group_query_is_an_escaped_in_chain_filter() -> None:
    directory = LdapDirectory(_settings(ldap_nested_groups=True, ldap_group_base_dn=f"OU=G,{BASE}"))
    queries: list[tuple[str, str]] = []

    class Svc:
        def __init__(self) -> None:
            self.response: list[dict[str, Any]] = []
            self.result: dict[str, Any] = {}

        def search(self, base: str, flt: str, *_a: Any, **_k: Any) -> None:
            queries.append((base, flt))
            self.response = [{"type": "searchResEntry", "attributes": {"cn": ["Server Admins"]}}]

    found = directory._nested_groups(Svc(), "CN=Weird (x)*,CN=Users,DC=corp,DC=local")  # type: ignore[arg-type]
    assert found == {"server admins"}
    base, flt = queries[0]
    assert base == f"OU=G,{BASE}"
    assert f"(member:{IN_CHAIN}:=" in flt
    assert "\\28x\\29\\2a" in flt  # parentheses and * in the DN are escaped


def test_referrals_are_never_followed() -> None:
    directory = LdapDirectory(_settings())
    conn = directory._connection("u", "p")
    assert conn.auto_referrals is False


def test_start_tls_is_only_for_plain_ldap_urls() -> None:
    with pytest.raises(ValueError, match="already TLS"):
        _settings(ldap_start_tls=True)
    s = _settings(ldap_url="ldap://dc.corp.local", ldap_start_tls=True)
    directory = LdapDirectory(s)
    assert directory._server.tls is not None  # certificate verification on


def test_start_tls_failure_is_a_directory_error() -> None:
    directory = LdapDirectory(_settings(ldap_url="ldap://dc.corp.local", ldap_start_tls=True))

    class Conn:
        def open(self) -> None:
            pass

        def start_tls(self) -> bool:
            return False

    with pytest.raises(DirectoryUnavailable):
        directory._bind(Conn())  # type: ignore[arg-type]
