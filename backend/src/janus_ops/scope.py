"""Which nodes a user may see and act on (per-role node scope).

A role is unrestricted ('all') or restricted to the nodes matching at least one of its rules; a
rule matches when every non-empty field of it matches (environment, tag, group, enabled
capability, explicit node). A user with several roles gets the union of their scopes, and one
unrestricted role makes the user unrestricted. The built-in admin is always unrestricted.

Everything that reads or acts on a node goes through here: list queries add `scope.sql()`, and
single-node endpoints call `require_node`, which answers 404 (never 403) so the existence of a
node outside the scope is not revealed.
"""

# ruff: noqa: S608 - SQL built from fixed fragments and a code-chosen alias; values are bound

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, WebSocket
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.rbac import ADMIN_ROLE

if TYPE_CHECKING:
    from janus_ops.auth.sessions import Session


@dataclass(frozen=True)
class Rule:
    environments: tuple[str, ...] = ()
    tags: tuple[uuid.UUID, ...] = ()
    groups: tuple[uuid.UUID, ...] = ()
    capabilities: tuple[str, ...] = ()
    nodes: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class Scope:
    rules: tuple[Rule, ...] | None = None  # None: every node; (): no node

    @property
    def unrestricted(self) -> bool:
        return self.rules is None

    def sql(self, alias: str = "n", prefix: str = "sc") -> tuple[str, dict[str, Any]]:
        """A boolean SQL expression on the node table aliased `alias`, with its parameters."""
        if self.rules is None:
            return "TRUE", {}
        if not self.rules:
            return "FALSE", {}
        params: dict[str, Any] = {}
        ors = []
        for i, rule in enumerate(self.rules):
            ands = []
            p = f"{prefix}{i}"
            if rule.environments:
                ands.append(f"{alias}.environment = ANY(CAST(:{p}e AS text[]))")
                params[f"{p}e"] = list(rule.environments)
            if rule.tags:
                ands.append(
                    f"EXISTS (SELECT 1 FROM node_tag x WHERE x.node_id = {alias}.id"
                    f" AND x.tag_id = ANY(CAST(:{p}t AS uuid[])))"
                )
                params[f"{p}t"] = list(rule.tags)
            if rule.groups:
                ands.append(
                    f"EXISTS (SELECT 1 FROM node_group_member x WHERE x.node_id = {alias}.id"
                    f" AND x.group_id = ANY(CAST(:{p}g AS uuid[])))"
                )
                params[f"{p}g"] = list(rule.groups)
            if rule.capabilities:
                ands.append(
                    f"(EXISTS (SELECT 1 FROM node_capability x WHERE x.node_id = {alias}.id"
                    f" AND x.enabled AND x.kind = ANY(CAST(:{p}c AS text[])))"
                    f" OR EXISTS (SELECT 1 FROM node_service x WHERE x.node_id = {alias}.id"
                    f" AND x.enabled AND x.kind = ANY(CAST(:{p}c AS text[]))))"
                )
                params[f"{p}c"] = list(rule.capabilities)
            if rule.nodes:
                ands.append(f"{alias}.id = ANY(CAST(:{p}n AS uuid[]))")
                params[f"{p}n"] = list(rule.nodes)
            ors.append("(" + (" AND ".join(ands) if ands else "TRUE") + ")")
        return "(" + " OR ".join(ors) + ")", params


UNRESTRICTED = Scope()
MARK = "<SCOPE>"


def fill(sql: str, scope_sql: str) -> str:
    """Put `Scope.sql()` where the query has the marker `<SCOPE>`.

    The scope SQL is built here from fixed fragments (values are bound parameters), never from
    user input; the marker keeps callers from assembling queries with f-strings.
    """
    return sql.replace(MARK, scope_sql)


async def scope_for(conn: AsyncConnection, roles: tuple[str, ...]) -> Scope:
    if ADMIN_ROLE in roles:
        return UNRESTRICTED
    if not roles:
        return Scope(())
    rows = (
        await conn.execute(
            text("SELECT name, node_scope FROM role WHERE name = ANY(:roles)"),
            {"roles": list(roles)},
        )
    ).all()
    if any(r.node_scope == "all" for r in rows):
        return UNRESTRICTED
    restricted = [r.name for r in rows]
    if not restricted:
        return Scope(())
    rules = await conn.execute(
        text(
            "SELECT environments, tag_ids, group_ids, capabilities, node_ids"
            " FROM role_scope_rule WHERE role = ANY(:roles) ORDER BY role, position, id"
        ),
        {"roles": restricted},
    )
    return Scope(
        tuple(
            Rule(
                tuple(r.environments),
                tuple(r.tag_ids),
                tuple(r.group_ids),
                tuple(r.capabilities),
                tuple(r.node_ids),
            )
            for r in rules
        )
    )


async def allows(conn: AsyncConnection, session: "Session", node_id: uuid.UUID | str) -> bool:
    if session.scope.unrestricted:
        return True
    clause, params = session.scope.sql("n")
    found = await conn.scalar(
        text(f"SELECT 1 FROM node n WHERE n.id = :id AND {clause}"),
        {"id": node_id, **params},
    )
    return bool(found)


async def require_node(
    request: Request | WebSocket, session: "Session", node_id: uuid.UUID | str
) -> None:
    """404 unless the node is in the session's scope (the node may also not exist)."""
    if session.scope.unrestricted:
        return
    async with request.app.state.engine.connect() as conn:
        if not await allows(conn, session, node_id):
            raise HTTPException(404, "not_found")


async def visible_ids(conn: AsyncConnection, session: "Session") -> set[str] | None:
    """Ids of the nodes in scope, or None when every node is."""
    if session.scope.unrestricted:
        return None
    clause, params = session.scope.sql("n")
    rows = await conn.execute(text(f"SELECT n.id FROM node n WHERE {clause}"), params)
    return {str(r.id) for r in rows}
