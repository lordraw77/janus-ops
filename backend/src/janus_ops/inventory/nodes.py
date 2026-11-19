"""/api/v1/nodes: the unified host inventory (FR-01).

A node is an IP or FQDN with an environment, tags, groups, an optional parent (dependency for
alert suppression, FR-32) and capabilities: ssh, agent, proxmox, docker, snmp. Each capability
has a validated configuration and, where needed, a vault credential of a compatible kind.
"""

import ipaddress
import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops import scope as node_scope
from janus_ops.admin.common import record
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.inventory.groups import TAG_NAME

router = APIRouter(prefix="/nodes", tags=["inventory"])

Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("inventory.manage"))]

Environment = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")]  # a row of `environment`
CapabilityKind = Literal["ssh", "agent", "proxmox", "docker", "snmp", "fritzbox", "postgres"]

NODE_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$"
_HOST_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
POSIX_USER = r"^[a-z_][a-z0-9_.-]{0,31}$"


# ---------------------------------------------------------------- capability configuration
class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SshConfig(_Config):
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = Field(default=None, pattern=POSIX_USER)
    jump_host_id: uuid.UUID | None = None


class AgentConfig(_Config):
    pass


class ProxmoxConfig(_Config):
    port: int = Field(default=8006, ge=1, le=65535)
    verify_tls: bool = True
    node_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")


class DockerConfig(_Config):
    via: Literal["agent", "ssh"] = "agent"


class SnmpConfig(_Config):
    # v2c: credential `generic` (the community). v3: credential `snmp_v3` (user and passphrases).
    version: Literal["v2c", "v3"] = "v2c"
    port: int = Field(default=161, ge=1, le=65535)
    # v3 only: noAuthNoPriv and authNoPriv exist for devices without encryption; the default
    # is authPriv. MD5 and DES are not offered.
    security_level: Literal["noAuthNoPriv", "authNoPriv", "authPriv"] = "authPriv"
    auth_protocol: Literal["SHA", "SHA256"] = "SHA256"
    priv_protocol: Literal["AES128", "AES256"] = "AES128"


class FritzConfig(_Config):
    # TR-064 over HTTP (the box's default is 49000); the user and password are in the credential
    port: int = Field(default=49000, ge=1, le=65535)


class PostgresConfig(_Config):
    # the server connects to the node's PostgreSQL; the user and password are in the credential
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(default="postgres", pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.$-]{0,62}$")
    # `require` encrypts without checking the certificate; `prefer` falls back to plain text
    sslmode: Literal["disable", "prefer", "require"] = "prefer"


# kind -> (config model, credential kinds accepted, credential required)
RULES: dict[str, tuple[type[_Config], frozenset[str], bool]] = {
    "ssh": (SshConfig, frozenset({"ssh_key", "ssh_password"}), True),
    "agent": (AgentConfig, frozenset(), False),
    "proxmox": (ProxmoxConfig, frozenset({"proxmox_token", "proxmox_password"}), True),
    "docker": (DockerConfig, frozenset(), False),
    "snmp": (SnmpConfig, frozenset({"snmp_v3", "generic"}), True),
    "fritzbox": (FritzConfig, frozenset({"fritzbox_password"}), True),
    "postgres": (PostgresConfig, frozenset({"postgres_password"}), True),
}


class CapabilityIn(BaseModel):
    kind: CapabilityKind
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    credential_id: uuid.UUID | None = None


class CredentialRef(BaseModel):
    id: str
    name: str
    kind: str


class CapabilityOut(BaseModel):
    kind: str
    enabled: bool
    config: dict[str, Any]
    credential: CredentialRef | None


class NodeRef(BaseModel):
    id: str
    name: str


# where the bootstrap installs the agent binary (agents/bootstrap.py: UNIT and the install steps)
AGENT_INSTALL_PATH = "/usr/local/bin/janus-agent"


class AgentSummary(BaseModel):
    """The agent of a node as the inventory shows it: what it runs, and where it lives."""

    version: str | None
    arch: str | None
    revoked: bool
    install_path: str  # the binary on the node; the row exists only while the agent is installed


class NodeOut(BaseModel):
    id: str
    name: str
    address: str
    environment: str
    parent: NodeRef | None
    description: str | None
    tags: list[str]
    groups: list[str]
    capabilities: list[CapabilityOut]
    agent: AgentSummary | None = None
    created_at: datetime
    updated_at: datetime


def _valid_address(value: str) -> str:
    value = value.strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    host = value.rstrip(".")
    if 0 < len(host) <= 253 and all(_HOST_LABEL.match(p) for p in host.split(".")):
        return host.lower()
    raise ValueError("must be an IPv4/IPv6 address or a host name")


class NodeIn(BaseModel):
    name: str | None = Field(default=None, pattern=NODE_NAME)
    address: str | None = None
    environment: Environment | None = None
    parent_id: uuid.UUID | None = None
    description: str | None = Field(default=None, max_length=1000)
    tags: list[Annotated[str, Field(pattern=TAG_NAME)]] | None = Field(default=None, max_length=32)
    groups: list[str] | None = Field(default=None, max_length=64)
    capabilities: list[CapabilityIn] | None = Field(default=None, max_length=5)

    @field_validator("address")
    @classmethod
    def _address(cls, value: str | None) -> str | None:
        return None if value is None else _valid_address(value)

    @field_validator("capabilities")
    @classmethod
    def _unique_kinds(cls, value: list[CapabilityIn] | None) -> list[CapabilityIn] | None:
        if value is not None and len({c.kind for c in value}) != len(value):
            raise ValueError("each capability kind may appear once")
        return value


# ---------------------------------------------------------------- reading
async def _load(
    conn: AsyncConnection, where: str = "", params: dict[str, Any] | None = None
) -> list[NodeOut]:
    nodes = (
        await conn.execute(
            text(
                # `where` is built from fixed fragments; values are bound parameters
                "SELECT n.id, n.name, n.address, n.environment, n.description, n.created_at,"  # noqa: S608
                " n.updated_at, p.id AS parent_id, p.name AS parent_name"
                f" FROM node n LEFT JOIN node p ON p.id = n.parent_id {where}"
                " ORDER BY lower(n.name) LIMIT 2000"
            ),
            params or {},
        )
    ).all()
    if not nodes:
        return []
    ids = [n.id for n in nodes]
    tags = await conn.execute(
        text(
            "SELECT nt.node_id, t.name FROM node_tag nt JOIN tag t ON t.id = nt.tag_id"
            " WHERE nt.node_id = ANY(:ids) ORDER BY lower(t.name)"
        ),
        {"ids": ids},
    )
    groups = await conn.execute(
        text(
            "SELECT m.node_id, g.name FROM node_group_member m"
            " JOIN node_group g ON g.id = m.group_id WHERE m.node_id = ANY(:ids)"
            " ORDER BY lower(g.name)"
        ),
        {"ids": ids},
    )
    caps = await conn.execute(
        text(
            "SELECT c.node_id, c.kind, c.enabled, c.config, cr.id AS cred_id,"
            " cr.name AS cred_name, cr.kind AS cred_kind FROM node_capability c"
            " LEFT JOIN credential cr ON cr.id = c.credential_id"
            " WHERE c.node_id = ANY(:ids) ORDER BY c.kind"
        ),
        {"ids": ids},
    )
    agents = await conn.execute(
        text(
            "SELECT node_id, version, arch, revoked_at IS NOT NULL AS revoked FROM agent"
            " WHERE node_id = ANY(:ids)"
        ),
        {"ids": ids},
    )
    agent_map: dict[Any, AgentSummary] = {
        r.node_id: AgentSummary(
            version=r.version or None,
            arch=r.arch or None,
            revoked=bool(r.revoked),
            install_path=AGENT_INSTALL_PATH,
        )
        for r in agents
    }
    tag_map: dict[Any, list[str]] = {}
    for r in tags:
        tag_map.setdefault(r.node_id, []).append(r.name)
    group_map: dict[Any, list[str]] = {}
    for r in groups:
        group_map.setdefault(r.node_id, []).append(r.name)
    cap_map: dict[Any, list[CapabilityOut]] = {}
    for r in caps:
        cap_map.setdefault(r.node_id, []).append(
            CapabilityOut(
                kind=r.kind,
                enabled=r.enabled,
                config=r.config,
                credential=CredentialRef(id=str(r.cred_id), name=r.cred_name, kind=r.cred_kind)
                if r.cred_id
                else None,
            )
        )
    return [
        NodeOut(
            id=str(n.id),
            name=n.name,
            address=n.address,
            environment=n.environment,
            parent=NodeRef(id=str(n.parent_id), name=n.parent_name) if n.parent_id else None,
            description=n.description,
            tags=tag_map.get(n.id, []),
            groups=group_map.get(n.id, []),
            capabilities=cap_map.get(n.id, []),
            agent=agent_map.get(n.id),
            created_at=n.created_at,
            updated_at=n.updated_at,
        )
        for n in nodes
    ]


@router.get("", response_model=list[NodeOut])
async def list_nodes(
    request: Request,
    session: Reader,
    q: Annotated[str | None, Query(max_length=100)] = None,
    environment: Environment | None = None,
    tag: Annotated[str | None, Query(max_length=64)] = None,
    group: Annotated[str | None, Query(max_length=128)] = None,
    capability: CapabilityKind | None = None,
) -> list[NodeOut]:
    scope_sql, params = session.scope.sql("n")
    clauses = [scope_sql]
    if q:
        clauses.append("(n.name ILIKE :q OR n.address ILIKE :q)")
        params["q"] = "%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    if environment:
        clauses.append("n.environment = :env")
        params["env"] = environment
    if tag:
        clauses.append(
            "EXISTS (SELECT 1 FROM node_tag nt JOIN tag t ON t.id = nt.tag_id"
            " WHERE nt.node_id = n.id AND lower(t.name) = lower(:tag))"
        )
        params["tag"] = tag
    if group:
        clauses.append(
            "EXISTS (SELECT 1 FROM node_group_member m JOIN node_group g ON g.id = m.group_id"
            " WHERE m.node_id = n.id AND lower(g.name) = lower(:grp))"
        )
        params["grp"] = group
    if capability:
        clauses.append(
            "EXISTS (SELECT 1 FROM node_capability c WHERE c.node_id = n.id"
            " AND c.kind = :cap AND c.enabled)"
        )
        params["cap"] = capability
    where = "WHERE " + " AND ".join(clauses)
    async with request.app.state.engine.connect() as conn:
        return await _load(conn, where, params)


@router.get("/{node_id}", response_model=NodeOut, responses={404: {}})
async def get_node(node_id: uuid.UUID, request: Request, session: Reader) -> NodeOut:
    await node_scope.require_node(request, session, node_id)
    async with request.app.state.engine.connect() as conn:
        found = await _load(conn, "WHERE n.id = :id", {"id": node_id})
    if not found:
        raise HTTPException(404, "not_found")
    return found[0]


# ---------------------------------------------------------------- writing
async def _check_environment(conn: AsyncConnection, name: str) -> None:
    if not await conn.scalar(text("SELECT 1 FROM environment WHERE name = :n"), {"n": name}):
        raise HTTPException(422, f"unknown_environment:{name}")


async def _check_parent(conn: AsyncConnection, node_id: uuid.UUID, parent_id: uuid.UUID) -> None:
    if parent_id == node_id:
        raise HTTPException(422, "parent_cycle")
    exists = await conn.scalar(text("SELECT 1 FROM node WHERE id = :p"), {"p": parent_id})
    if not exists:
        raise HTTPException(422, "unknown_parent")
    cycle = await conn.scalar(
        text(
            "WITH RECURSIVE up(id) AS (SELECT CAST(:p AS uuid) UNION"
            " SELECT n.parent_id FROM node n JOIN up ON n.id = up.id WHERE n.parent_id IS NOT NULL)"
            " SELECT 1 FROM up WHERE id = :me"
        ),
        {"p": parent_id, "me": node_id},
    )
    if cycle:
        raise HTTPException(422, "parent_cycle")


async def _validated_capabilities(
    conn: AsyncConnection, node_id: uuid.UUID, caps: list[CapabilityIn]
) -> list[tuple[str, bool, dict[str, Any], uuid.UUID | None]]:
    out: list[tuple[str, bool, dict[str, Any], uuid.UUID | None]] = []
    kinds = {c.kind for c in caps}
    for cap in caps:
        model, cred_kinds, required = RULES[cap.kind]
        try:
            config = model.model_validate(cap.config).model_dump(mode="json")
        except ValidationError as exc:
            field = ".".join(str(p) for p in exc.errors()[0]["loc"]) or "config"
            raise HTTPException(422, f"invalid_config:{cap.kind}.{field}") from None
        if cap.credential_id is None:
            if required and cap.enabled:
                raise HTTPException(422, f"credential_required:{cap.kind}")
        else:
            if not cred_kinds:
                raise HTTPException(422, f"credential_not_used:{cap.kind}")
            kind = await conn.scalar(
                text("SELECT kind FROM credential WHERE id = :id"), {"id": cap.credential_id}
            )
            if kind is None:
                raise HTTPException(422, "unknown_credential")
            if kind not in cred_kinds:
                raise HTTPException(422, f"credential_kind_mismatch:{cap.kind}")
            if cap.kind == "snmp":
                # v2c reads a community, v3 reads a user: the two are not interchangeable
                wanted = "generic" if config["version"] == "v2c" else "snmp_v3"
                if kind != wanted:
                    raise HTTPException(422, "credential_kind_mismatch:snmp")
        if cap.kind == "docker" and config["via"] == "ssh" and "ssh" not in kinds:
            raise HTTPException(422, "docker_via_ssh_needs_ssh")
        if cap.kind == "ssh" and config.get("jump_host_id"):
            jump = uuid.UUID(config["jump_host_id"])
            if jump == node_id:
                raise HTTPException(422, "invalid_config:ssh.jump_host_id")
            ok = await conn.scalar(
                text(
                    "SELECT 1 FROM node_capability WHERE node_id = :j AND kind = 'ssh' AND enabled"
                ),
                {"j": jump},
            )
            if not ok:
                raise HTTPException(422, "jump_host_without_ssh")
        out.append((cap.kind, cap.enabled, config, cap.credential_id))
    return out


async def _write_relations(conn: AsyncConnection, node_id: uuid.UUID, body: NodeIn) -> None:
    if body.tags is not None:
        await conn.execute(text("DELETE FROM node_tag WHERE node_id = :id"), {"id": node_id})
        for name in dict.fromkeys(body.tags):  # de-duplicate, keep order
            await conn.execute(
                text("INSERT INTO tag (name) VALUES (:n) ON CONFLICT ((lower(name))) DO NOTHING"),
                {"n": name},
            )
            await conn.execute(
                text(
                    "INSERT INTO node_tag (node_id, tag_id) SELECT :id, id FROM tag"
                    " WHERE lower(name) = lower(:n) ON CONFLICT DO NOTHING"
                ),
                {"id": node_id, "n": name},
            )
    if body.groups is not None:
        wanted = {g.lower() for g in body.groups}
        rows = (
            await conn.execute(
                text("SELECT id, lower(name) AS n FROM node_group WHERE lower(name) = ANY(:g)"),
                {"g": sorted(wanted)},
            )
        ).all()
        missing = wanted - {r.n for r in rows}
        if missing:
            raise HTTPException(422, f"unknown_group:{sorted(missing)[0]}")
        await conn.execute(
            text("DELETE FROM node_group_member WHERE node_id = :id"), {"id": node_id}
        )
        for r in rows:
            await conn.execute(
                text("INSERT INTO node_group_member (group_id, node_id) VALUES (:g, :id)"),
                {"g": r.id, "id": node_id},
            )
    if body.capabilities is not None:
        caps = await _validated_capabilities(conn, node_id, body.capabilities)
        await conn.execute(text("DELETE FROM node_capability WHERE node_id = :id"), {"id": node_id})
        for kind, enabled, config, cred in caps:
            await conn.execute(
                text(
                    "INSERT INTO node_capability (node_id, kind, enabled, config, credential_id)"
                    " VALUES (:id, :k, :e, CAST(:c AS jsonb), :cred)"
                ),
                {"id": node_id, "k": kind, "e": enabled, "c": json.dumps(config), "cred": cred},
            )


def _summary(body: NodeIn) -> dict[str, Any]:
    data = body.model_dump(mode="json", exclude_unset=True)
    if "capabilities" in data:
        data["capabilities"] = [c["kind"] for c in data["capabilities"]]
    return data


@router.post("", status_code=201, responses={409: {}, 422: {}})
async def create_node(body: NodeIn, request: Request, session: Manager) -> dict[str, str]:
    if body.name is None or body.address is None:
        raise HTTPException(422, "name_and_address_required")
    node_id = uuid.uuid4()
    try:
        async with request.app.state.engine.begin() as conn:
            await _check_environment(conn, body.environment or "prod")
            if body.parent_id:
                await _check_parent(conn, node_id, body.parent_id)
            await conn.execute(
                text(
                    "INSERT INTO node (id, name, address, environment, parent_id, description,"
                    " created_by) VALUES (:id, :n, :a, :e, :p, :d, CAST(:by AS uuid))"
                ),
                {
                    "id": node_id,
                    "n": body.name,
                    "a": body.address,
                    "e": body.environment or "prod",
                    "p": body.parent_id,
                    "d": body.description,
                    "by": session.user_id,
                },
            )
            await _write_relations(conn, node_id, body)
            if not await node_scope.allows(conn, session, node_id):
                raise HTTPException(403, "out_of_scope")  # rolled back: not a node of theirs
    except IntegrityError:
        raise HTTPException(409, "name_exists") from None
    await record(request, session, "inventory.node_created", "node", body.name, _summary(body))
    return {"id": str(node_id)}


@router.patch("/{node_id}", status_code=204, responses={404: {}, 409: {}, 422: {}})
async def update_node(
    node_id: uuid.UUID, body: NodeIn, request: Request, session: Manager
) -> Response:
    fields = body.model_dump(exclude_unset=True)
    await node_scope.require_node(request, session, node_id)
    try:
        async with request.app.state.engine.begin() as conn:
            name = await conn.scalar(text("SELECT name FROM node WHERE id = :id"), {"id": node_id})
            if name is None:
                raise HTTPException(404, "not_found")
            if fields.get("environment"):
                await _check_environment(conn, fields["environment"])
            if fields.get("parent_id"):
                await _check_parent(conn, node_id, body.parent_id)  # type: ignore[arg-type]
            for column in ("name", "address", "environment", "parent_id", "description"):
                if column in fields:
                    if column in ("name", "address", "environment") and fields[column] is None:
                        raise HTTPException(422, f"{column}_required")
                    await conn.execute(
                        text(f"UPDATE node SET {column} = :v WHERE id = :id"),  # noqa: S608
                        {"v": fields[column], "id": node_id},
                    )
            await conn.execute(
                text("UPDATE node SET updated_at = now() WHERE id = :id"), {"id": node_id}
            )
            await _write_relations(conn, node_id, body)
            if not await node_scope.allows(conn, session, node_id):
                raise HTTPException(403, "out_of_scope")  # it would leave their scope
    except IntegrityError:
        raise HTTPException(409, "name_exists") from None
    if body.capabilities is not None and not any(
        c.kind == "agent" and c.enabled for c in body.capabilities
    ):
        # agent capability removed or disabled: its connection goes too
        await request.app.state.agents.disconnect(str(node_id), 4403, "disabled")
    await record(
        request, session, "inventory.node_updated", "node", body.name or name, _summary(body)
    )
    return Response(status_code=204)


@router.delete("/{node_id}", status_code=204, responses={404: {}})
async def delete_node(node_id: uuid.UUID, request: Request, session: Manager) -> Response:
    await node_scope.require_node(request, session, node_id)
    async with request.app.state.engine.begin() as conn:
        name = await conn.scalar(
            text("DELETE FROM node WHERE id = :id RETURNING name"), {"id": node_id}
        )
    if name is None:
        raise HTTPException(404, "not_found")
    # Children keep existing: their parent_id becomes NULL (ON DELETE SET NULL)
    await request.app.state.agents.disconnect(str(node_id), 4403, "disabled")
    await record(request, session, "inventory.node_deleted", "node", name)
    return Response(status_code=204)
