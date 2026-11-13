"""LDAP directory backend (ADR-008).

ldap3 is synchronous: every call runs in a worker thread. Flow: bind as the read-only
service account, find the user entry, then bind as that DN with the submitted password.
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

ROLE_ORDER = ("admin", "operator", "auditor")  # highest privilege first


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


def role_for_groups(groups: frozenset[str], settings: Settings) -> str | None:
    mapping = {
        "admin": settings.ldap_group_admin.lower(),
        "operator": settings.ldap_group_operator.lower(),
        "auditor": settings.ldap_group_auditor.lower(),
    }
    for role in ROLE_ORDER:
        if mapping[role] in groups:
            return role
    return None


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
        if settings.ldap_url.startswith("ldaps://"):
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
        )

    def _authenticate_sync(self, username: str, password: str) -> DirectoryUser:
        s = self._settings
        try:
            svc = self._connection(s.ldap_bind_dn or "", self._bind_password)
            if not svc.bind():
                raise DirectoryUnavailable("service account bind rejected")
            try:
                svc.search(
                    s.ldap_users_dn,
                    s.ldap_user_filter.replace("{username}", escape_filter_chars(username)),
                    SUBTREE,
                    attributes=["uid", "cn", "displayName", "mail", "memberOf"],
                    size_limit=2,
                )
                entries = [e for e in svc.response or [] if e.get("type") == "searchResEntry"]
            finally:
                svc.unbind()
            if len(entries) != 1:
                # 0 = unknown user, 2+ = ambiguous filter: never guess which one
                raise InvalidCredentials
            entry = entries[0]

            user_conn = self._connection(entry["dn"], password)
            try:
                if not user_conn.bind():
                    raise InvalidCredentials
            finally:
                user_conn.unbind()
        except LDAPException as exc:
            raise DirectoryUnavailable(type(exc).__name__) from exc

        attrs = entry["attributes"]
        groups = frozenset(
            cn for cn in (group_cn(str(dn)) for dn in attrs.get("memberOf", [])) if cn
        )
        return DirectoryUser(
            dn=entry["dn"],
            username=(_first(attrs, "uid") or username).lower(),
            display_name=_first(attrs, "displayName") or _first(attrs, "cn"),
            email=_first(attrs, "mail"),
            groups=groups,
        )

    async def authenticate(self, username: str, password: str) -> DirectoryUser:
        if not password:
            # An empty password would be an "unauthenticated bind" on many servers
            raise InvalidCredentials
        return await asyncio.to_thread(self._authenticate_sync, username, password)
