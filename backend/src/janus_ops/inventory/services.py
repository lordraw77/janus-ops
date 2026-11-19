"""Node services: what a machine runs or is, read by monitoring over the network.

A capability is how Janus-Ops reaches a node (ssh, agent, proxmox, docker, snmp, see nodes.py). A
service is a thing on the node that a connector reads: a PostgreSQL server, a FRITZ!Box. A node has
any number of them, each with a name (unique on the node), a config and a credential, so two
PostgreSQL servers on different ports are two services.

`SERVICE_TYPES` is the catalog: for each kind its config model, the credential kinds it accepts and
the fields the web UI draws (`GET /service-types`). Adding a kind is an entry here, its connector in
monitoring/, and the CHECK of `node_service.kind`.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session

router = APIRouter(prefix="/service-types", tags=["inventory"])
Reader = Annotated[Session, Depends(require_permission("inventory.read"))]

ServiceKind = Literal["postgres", "fritzbox"]
NAME = r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$"
MAX_SERVICES = 20


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PostgresConfig(_Config):
    # the server connects to the node's PostgreSQL; the user and password are in the credential
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(default="postgres", pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.$-]{0,62}$")
    # `require` encrypts without checking the certificate; `prefer` falls back to plain text
    sslmode: Literal["disable", "prefer", "require"] = "prefer"


class FritzConfig(_Config):
    # TR-064 over HTTP (the box's default is 49000); the user and password are in the credential
    port: int = Field(default=49000, ge=1, le=65535)


@dataclass(frozen=True)
class ServiceType:
    kind: str
    label: str
    description: str
    config: type[_Config]
    credentials: frozenset[str]
    credential_required: bool
    # what the UI draws for the config: {name, label, type: number|text|select, options?, hint?}
    fields: list[dict[str, Any]] = field(default_factory=list)


SERVICE_TYPES: dict[str, ServiceType] = {
    "postgres": ServiceType(
        kind="postgres",
        label="PostgreSQL",
        description=(
            "The server connects and reads statistics only. Give the role pg_monitor and "
            "nothing else: GRANT pg_monitor TO janus_monitor;"
        ),
        config=PostgresConfig,
        credentials=frozenset({"postgres_password"}),
        credential_required=True,
        fields=[
            {"name": "port", "label": "Port", "type": "number"},
            {"name": "database", "label": "Database", "type": "text"},
            {
                "name": "sslmode",
                "label": "Encryption",
                "type": "select",
                "options": [
                    {"value": "prefer", "label": "prefer (encrypt when the server can)"},
                    {"value": "require", "label": "require (never plain text)"},
                    {"value": "disable", "label": "disable"},
                ],
            },
        ],
    ),
    "fritzbox": ServiceType(
        kind="fritzbox",
        label="FRITZ!Box (TR-064)",
        description="Polled over TR-064 with a user of the box that has the TR-064 right.",
        config=FritzConfig,
        credentials=frozenset({"fritzbox_password"}),
        credential_required=True,
        fields=[{"name": "port", "label": "TR-064 port", "type": "number"}],
    ),
}
SERVICE_KINDS = frozenset(SERVICE_TYPES)


class ServiceIn(BaseModel):
    kind: ServiceKind
    # unique on the node; the kind when left empty (a second service of a kind needs its own name)
    name: str | None = Field(default=None, pattern=NAME)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    credential_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class ServiceOut(BaseModel):
    id: str
    kind: str
    name: str
    enabled: bool
    config: dict[str, Any]
    credential: dict[str, str] | None


def unique_names(services: list[ServiceIn]) -> list[ServiceIn]:
    names = [(s.name or s.kind).lower() for s in services]
    if len(set(names)) != len(names):
        raise ValueError("each service name may appear once on a node")
    return services


async def validate(
    conn: AsyncConnection, services: list[ServiceIn]
) -> list[tuple[str, str, bool, dict[str, Any], uuid.UUID | None]]:
    """(kind, name, enabled, config, credential) for each service, or an HTTP 422 saying why not."""
    out: list[tuple[str, str, bool, dict[str, Any], uuid.UUID | None]] = []
    for svc in services:
        stype = SERVICE_TYPES[svc.kind]
        try:
            config = stype.config.model_validate(svc.config).model_dump(mode="json")
        except ValidationError as exc:
            field_name = ".".join(str(p) for p in exc.errors()[0]["loc"]) or "config"
            raise HTTPException(422, f"invalid_config:{svc.kind}.{field_name}") from None
        if svc.credential_id is None:
            if stype.credential_required and svc.enabled:
                raise HTTPException(422, f"credential_required:{svc.kind}")
        else:
            kind = await conn.scalar(
                text("SELECT kind FROM credential WHERE id = :id"), {"id": svc.credential_id}
            )
            if kind is None:
                raise HTTPException(422, "unknown_credential")
            if kind not in stype.credentials:
                raise HTTPException(422, f"credential_kind_mismatch:{svc.kind}")
        out.append((svc.kind, svc.name or svc.kind, svc.enabled, config, svc.credential_id))
    return out


async def write(conn: AsyncConnection, node_id: uuid.UUID, services: list[ServiceIn]) -> None:
    """Replaces the node's services with this list (validated first)."""
    rows = await validate(conn, services)
    await conn.execute(text("DELETE FROM node_service WHERE node_id = :id"), {"id": node_id})
    for kind, name, enabled, config, cred in rows:
        await conn.execute(
            text(
                "INSERT INTO node_service (node_id, kind, name, enabled, config, credential_id)"
                " VALUES (:id, :k, :n, :e, CAST(:c AS jsonb), :cred)"
            ),
            {
                "id": node_id,
                "k": kind,
                "n": name,
                "e": enabled,
                "c": json.dumps(config),
                "cred": cred,
            },
        )


async def has_requirement(
    conn: AsyncConnection, node_id: uuid.UUID | str, requirement: str
) -> bool:
    """The node has an enabled capability, or an enabled service, of this kind."""
    table = "node_service" if requirement in SERVICE_KINDS else "node_capability"
    found = await conn.scalar(
        text(f"SELECT 1 FROM {table} WHERE node_id = :n AND kind = :k AND enabled"),  # noqa: S608
        {"n": node_id, "k": requirement},
    )
    return bool(found)


def missing_code(requirement: str) -> str:
    """The API error for a node that lacks what a check or template needs."""
    return f"no_{requirement}_{'service' if requirement in SERVICE_KINDS else 'capability'}"


@router.get("")
async def list_service_types(_: Reader) -> list[dict[str, Any]]:
    """The kinds of service a node can have, with what the UI needs to draw their form."""
    return [
        {
            "kind": t.kind,
            "label": t.label,
            "description": t.description,
            "credentials": sorted(t.credentials),
            "credential_required": t.credential_required,
            "fields": t.fields,
            "defaults": t.config().model_dump(mode="json"),
        }
        for t in SERVICE_TYPES.values()
    ]
