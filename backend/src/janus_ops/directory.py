"""User and group administration in lldap (users page, initial admin).

- Users and groups: lldap GraphQL API, authenticated as the `janus-manager` account
  (member of lldap_admin). HTTP via the standard library, run in a worker thread.
- Passwords: LDAP Password Modify extended operation (RFC 3062) — lldap stores passwords
  with OPAQUE, so GraphQL cannot set them.
"""

import asyncio
import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ldap3 import NONE, Connection, Server
from ldap3.core.exceptions import LDAPException

from janus_ops.config import Settings

USER_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,63}$"


class DirectoryError(Exception):
    """Generic failure; `code` is returned to the API client."""

    code = "directory_error"
    status = 502


class DirectoryUnavailable(DirectoryError):
    code = "directory_unavailable"
    status = 503


class DirectoryConflict(DirectoryError):
    code = "already_exists"
    status = 409


class DirectoryNotFound(DirectoryError):
    code = "not_found"
    status = 404


@dataclass(frozen=True)
class DirUser:
    id: str
    email: str
    display_name: str
    created_at: datetime | None
    groups: frozenset[str]  # group names, lower-case


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    # lldap returns nanoseconds: trim to microseconds for fromisoformat
    head, _, frac = value.rstrip("Z").partition(".")
    return datetime.fromisoformat(
        f"{head}.{(frac + '000000')[:6]}+00:00" if frac else head + "+00:00"
    )


class LldapAdmin:
    def __init__(self, settings: Settings) -> None:
        if not settings.lldap_http_url or settings.ldap_manager_password is None:
            raise ValueError("directory management is not configured")
        self._settings = settings
        self._base = settings.lldap_http_url.rstrip("/")
        self._password = settings.ldap_manager_password.get_secret_value()
        self._token: str | None = None
        self._lock = threading.Lock()
        # Accounts Janus-Ops must never show or touch
        self.reserved = frozenset(
            {"admin", settings.ldap_manager_user, _uid(settings.ldap_bind_dn)} - {None}
        )

    # ------------------------------------------------------------------ HTTP / GraphQL
    def _post(self, path: str, body: dict[str, Any], token: str | None) -> dict[str, Any]:
        req = urllib.request.Request(  # noqa: S310 - URL scheme validated in Settings
            self._base + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}
            | ({"Authorization": f"Bearer {token}"} if token else {}),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._settings.ldap_timeout_s) as resp:  # noqa: S310
                data: dict[str, Any] = json.loads(resp.read())
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise PermissionError from exc
            raise DirectoryError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DirectoryUnavailable(type(exc).__name__) from exc

    def _login(self) -> str:
        try:
            data = self._post(
                "/auth/simple/login",
                {"username": self._settings.ldap_manager_user, "password": self._password},
                None,
            )
        except PermissionError as exc:
            raise DirectoryUnavailable("manager account rejected") from exc
        return str(data["token"])

    def _gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._token is None:
                self._token = self._login()
            token = self._token
        body = {"query": query, "variables": variables or {}}
        try:
            data = self._post("/api/graphql", body, token)
        except PermissionError:
            with self._lock:  # token expired (1 day): log in again once
                self._token = self._login()
                token = self._token
            data = self._post("/api/graphql", body, token)
        if data.get("errors"):
            message = str(data["errors"][0].get("message", ""))
            if "UNIQUE constraint" in message or "already exists" in message:
                raise DirectoryConflict(message)
            if "not found" in message.lower() or "does not exist" in message.lower():
                raise DirectoryNotFound(message)
            raise DirectoryError(message)
        result: dict[str, Any] = data["data"]
        return result

    # ------------------------------------------------------------------ users
    def _list_users(self) -> list[DirUser]:
        data = self._gql("{ users { id email displayName creationDate groups { displayName } } }")
        return [
            DirUser(
                id=u["id"],
                email=u["email"] or "",
                display_name=u["displayName"] or "",
                created_at=_parse_ts(u.get("creationDate")),
                groups=frozenset(g["displayName"].lower() for g in u["groups"]),
            )
            for u in data["users"]
            if u["id"] not in self.reserved
        ]

    def _group_ids(self) -> dict[str, int]:
        data = self._gql("{ groups { id displayName } }")
        return {g["displayName"].lower(): int(g["id"]) for g in data["groups"]}

    def _ensure_group(self, name: str) -> int:
        ids = self._group_ids()
        if name.lower() in ids:
            return ids[name.lower()]
        data = self._gql("mutation($n: String!) { createGroup(name: $n) { id } }", {"n": name})
        return int(data["createGroup"]["id"])

    def _create_user(self, user_id: str, email: str, display_name: str) -> None:
        self._gql(
            "mutation($u: CreateUserInput!) { createUser(user: $u) { id } }",
            {"u": {"id": user_id, "email": email, "displayName": display_name}},
        )

    def _update_user(self, user_id: str, email: str | None, display_name: str | None) -> None:
        patch: dict[str, Any] = {"id": user_id}
        if email is not None:
            patch["email"] = email
        if display_name is not None:
            patch["displayName"] = display_name
        self._gql("mutation($u: UpdateUserInput!) { updateUser(user: $u) { ok } }", {"u": patch})

    def _delete_user(self, user_id: str) -> None:
        self._gql("mutation($id: String!) { deleteUser(userId: $id) { ok } }", {"id": user_id})

    def _set_groups(self, user_id: str, wanted: set[str], managed: set[str]) -> None:
        """Make the user's membership among `managed` groups equal to `wanted`."""
        current = next((u.groups for u in self._list_users() if u.id == user_id), None)
        if current is None:
            raise DirectoryNotFound(user_id)
        wanted_l = {g.lower() for g in wanted}
        managed_l = {g.lower() for g in managed}
        for name in wanted_l - current:
            gid = self._ensure_group(name)
            self._gql(
                "mutation($u: String!, $g: Int!)"
                " { addUserToGroup(userId: $u, groupId: $g) { ok } }",
                {"u": user_id, "g": gid},
            )
        ids = self._group_ids()
        for name in (current & managed_l) - wanted_l:
            if name in ids:
                self._gql(
                    "mutation($u: String!, $g: Int!)"
                    " { removeUserFromGroup(userId: $u, groupId: $g) { ok } }",
                    {"u": user_id, "g": ids[name]},
                )

    def _set_password(self, user_id: str, password: str) -> None:
        s = self._settings
        server = Server(s.ldap_url or "", connect_timeout=s.ldap_timeout_s, get_info=NONE)
        conn = Connection(
            server,
            user=s.user_dn(s.ldap_manager_user),
            password=self._password,
            receive_timeout=s.ldap_timeout_s,
        )
        try:
            if not conn.bind():
                raise DirectoryUnavailable("manager bind rejected")
            try:
                ok = conn.extend.standard.modify_password(s.user_dn(user_id), new_password=password)
            finally:
                conn.unbind()
        except LDAPException as exc:
            raise DirectoryUnavailable(type(exc).__name__) from exc
        if not ok:
            raise DirectoryError(str(conn.result.get("description", "password change refused")))

    # ------------------------------------------------------------------ async API
    async def list_users(self) -> list[DirUser]:
        return await asyncio.to_thread(self._list_users)

    async def ensure_group(self, name: str) -> None:
        await asyncio.to_thread(self._ensure_group, name)

    async def create_user(
        self, user_id: str, email: str, display_name: str, password: str, groups: set[str]
    ) -> None:
        """Create, set the password and the groups; undo the creation if a later step fails."""

        def _go() -> None:
            self._create_user(user_id, email, display_name)
            try:
                self._set_password(user_id, password)
                self._set_groups(user_id, groups, groups)
            except DirectoryError:
                self._delete_user(user_id)
                raise

        await asyncio.to_thread(_go)

    async def update_user(self, user_id: str, email: str | None, display_name: str | None) -> None:
        await asyncio.to_thread(self._update_user, user_id, email, display_name)

    async def set_groups(self, user_id: str, wanted: set[str], managed: set[str]) -> None:
        await asyncio.to_thread(self._set_groups, user_id, wanted, managed)

    async def set_password(self, user_id: str, password: str) -> None:
        await asyncio.to_thread(self._set_password, user_id, password)

    async def delete_user(self, user_id: str) -> None:
        await asyncio.to_thread(self._delete_user, user_id)


def _uid(dn: str | None) -> str | None:
    if not dn or not dn.lower().startswith("uid="):
        return None
    return dn.split(",", 1)[0][4:]
