"""LDAP directory backend (ADR-008).

ldap3 is synchronous: every call runs in a worker thread. Flow: bind as the read-only
service account, find the user entry, then bind as that DN with the submitted password.

Works with lldap and OpenLDAP (`uid`) and with an external Active Directory (FR-07):
- the login attribute is configurable (`sAMAccountName`), and the user filter may match more
  than one attribute, e.g. `(|(sAMAccountName={username})(userPrincipalName={username}))`;
- StartTLS on ldap:// (certificate always verified), or ldaps://;
- referrals are never followed: a referral could send the service account's or the user's
  password to a server nobody configured;
- with `ldap_nested_groups`, groups of groups are resolved with the AD matching rule
  LDAP_MATCHING_RULE_IN_CHAIN, besides the direct `memberOf`.
"""

import asyncio
import ssl
from dataclasses import dataclass
from typing import Any, Protocol

from ldap3 import NONE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import parse_dn

from janus_ops.config import Settings


class InvalidCredentials(Exception):
    """Unknown user or wrong password (deliberately indistinguishable)."""


class DirectoryUnavailable(Exception):
    """The directory could not be reached or rejected the service account."""


@dataclass(frozen=True)
class DirectoryUser:
    dn: str
    username: str
    display_name: str | None
    email: str | None
    groups: frozenset[str]  # group CNs, lower-case


class DirectoryBackend(Protocol):
    async def authenticate(self, username: str, password: str) -> DirectoryUser: ...


def group_cn(dn: str) -> str | None:
    """'cn=janus-admins,ou=groups,dc=x' -> 'janus-admins'."""
    try:
        attr, value, _sep = parse_dn(dn)[0]
    except (LDAPException, IndexError, ValueError):
        return None
    return value.lower() if attr.lower() == "cn" else None


IN_CHAIN = "1.2.840.113556.1.4.1941"  # LDAP_MATCHING_RULE_IN_CHAIN (Active Directory)
PAGE = 500


def _first(attrs: dict[str, Any], name: str) -> str | None:
    value = attrs.get(name)
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value else None


class LdapDirectory:
    def __init__(self, settings: Settings) -> None:
        if settings.ldap_url is None or settings.ldap_bind_password is None:
            raise ValueError("LDAP is not configured")
        tls = None
        if settings.ldap_url.startswith("ldaps://") or settings.ldap_start_tls:
            tls = Tls(
                validate=ssl.CERT_REQUIRED,
                ca_certs_file=str(settings.ldap_ca_file) if settings.ldap_ca_file else None,
            )
        # get_info=NONE: no schema download, so attributes lldap does not declare still work
        self._server = Server(
            settings.ldap_url,
            connect_timeout=settings.ldap_timeout_s,
            get_info=NONE,
            tls=tls,
        )
        self._settings = settings
        self._bind_password = settings.ldap_bind_password.get_secret_value()

    def _connection(self, user: str, password: str) -> Connection:
        return Connection(
            self._server,
            user=user,
            password=password,
            receive_timeout=self._settings.ldap_timeout_s,
            read_only=True,
            auto_referrals=False,
        )

    def _bind(self, conn: Connection) -> bool:
        """Bind, upgrading to TLS first when StartTLS is configured: no password in clear text."""
        if self._settings.ldap_start_tls:
            conn.open()
            if not conn.start_tls():
                raise DirectoryUnavailable("StartTLS failed")
        return bool(conn.bind())

    def _nested_groups(self, svc: Connection, user_dn: str) -> set[str]:
        """Every group the user belongs to, directly or through other groups (Active Directory)."""
        s = self._settings
        found: set[str] = set()
        cookie: Any = None
        while True:
            svc.search(
                s.ldap_group_base_dn or s.ldap_base_dn,
                f"(&(objectClass=group)(member:{IN_CHAIN}:={escape_filter_chars(user_dn)}))",
                SUBTREE,
                attributes=["cn"],
                paged_size=PAGE,
                paged_cookie=cookie,
            )
            for e in svc.response or []:
                if e.get("type") == "searchResEntry":
                    name = _first(e.get("attributes", {}), "cn")
                    if name:
                        found.add(name.lower())
            cookie = (
                svc.result.get("controls", {})
                .get("1.2.840.113556.1.4.319", {})
                .get("value", {})
                .get("cookie")
            )
            if not cookie:
                return found

    def _authenticate_sync(self, username: str, password: str) -> DirectoryUser:
        s = self._settings
        try:
            svc = self._connection(s.ldap_bind_dn or "", self._bind_password)
            if not self._bind(svc):
                raise DirectoryUnavailable("service account bind rejected")
            nested: set[str] = set()
            try:
                svc.search(
                    s.ldap_users_dn,
                    s.ldap_user_filter.replace("{username}", escape_filter_chars(username)),
                    SUBTREE,
                    attributes=[s.ldap_username_attr, "cn", "displayName", "mail", "memberOf"],
                    size_limit=2,
                )
                entries = [e for e in svc.response or [] if e.get("type") == "searchResEntry"]
                if len(entries) == 1 and s.ldap_nested_groups:
                    nested = self._nested_groups(svc, entries[0]["dn"])
            finally:
                svc.unbind()
            if len(entries) != 1:
                # 0 = unknown user, 2+ = ambiguous filter: never guess which one
                raise InvalidCredentials
            entry = entries[0]

            user_conn = self._connection(entry["dn"], password)
            try:
                if not self._bind(user_conn):
                    raise InvalidCredentials
            finally:
                user_conn.unbind()
        except LDAPException as exc:
            raise DirectoryUnavailable(type(exc).__name__) from exc

        attrs = entry["attributes"]
        direct = (group_cn(str(dn)) for dn in attrs.get("memberOf", []))
        groups = frozenset({cn for cn in direct if cn} | nested)
        return DirectoryUser(
            dn=entry["dn"],
            username=(_first(attrs, s.ldap_username_attr) or username).lower(),
            display_name=_first(attrs, "displayName") or _first(attrs, "cn"),
            email=_first(attrs, "mail"),
            groups=groups,
        )

    async def authenticate(self, username: str, password: str) -> DirectoryUser:
        if not password:
            # An empty password would be an "unauthenticated bind" on many servers
            raise InvalidCredentials
        return await asyncio.to_thread(self._authenticate_sync, username, password)
