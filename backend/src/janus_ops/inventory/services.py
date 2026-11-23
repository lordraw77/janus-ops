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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    create_model,
    field_validator,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.monitoring import service_catalog

router = APIRouter(prefix="/service-types", tags=["inventory"])
Reader = Annotated[Session, Depends(require_permission("inventory.read"))]

# a kind is `postgres`, `fritzbox` or the slug of a product of monitoring/service_catalog.py;
# ServiceIn checks it against SERVICE_TYPES, the database only checks its shape
ServiceKind = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
# kinds that are also capabilities (the `docker` capability is a socket, the `docker` service
# is the engine): a template's `requires` of these means the capability
CAPABILITY_KINDS = frozenset({"agent", "ssh", "proxmox", "docker", "snmp"})
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
    category: str = "generic"


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
        category="database",
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
        category="network",
        fields=[{"name": "port", "label": "TR-064 port", "type": "number"}],
    ),
}


def _product_type(product: service_catalog.Product) -> ServiceType:
    """A well-known product as a service: its port (and health URL), read without a credential by
    the `service.*` probes (monitoring/probes/service.py)."""
    fields: list[dict[str, Any]] = []
    model_fields: dict[str, Any] = {}
    if product.ports:
        default = product.ports[0][1]
        model_fields["port"] = (int, Field(default=default, ge=1, le=65535))
        fields.append({"name": "port", "label": f"Port ({product.ports[0][2]})", "type": "number"})
    if product.http:
        macro, path, _, scheme = product.http
        port = next(p[1] for p in product.ports if p[0] == macro)
        model_fields["http_port"] = (int, Field(default=port, ge=1, le=65535))
        model_fields["http_path"] = (str, Field(default=path, pattern=r"^/\S{0,255}$"))
        model_fields["scheme"] = (Literal["http", "https"], scheme)
        model_fields["verify_tls"] = (bool, False)
        fields += [
            {"name": "http_port", "label": "Health URL port", "type": "number"},
            {"name": "http_path", "label": "Health URL path", "type": "text"},
            {
                "name": "scheme",
                "label": "Health URL scheme",
                "type": "select",
                "options": [
                    {"value": "http", "label": "http"},
                    {"value": "https", "label": "https"},
                ],
            },
            {
                "name": "verify_tls",
                "label": "Verify the TLS certificate",
                "type": "select",
                "options": [{"value": "false", "label": "no"}, {"value": "true", "label": "yes"}],
            },
        ]
    config = create_model(f"{product.slug}_config", __base__=_Config, **model_fields)
    return ServiceType(
        kind=product.slug,
        label=product.name,
        description=product.description,
        config=config,
        credentials=frozenset(),
        credential_required=False,
        fields=fields,
        category=product.category,
    )


SERVICE_TYPES.update({p.slug: _product_type(p) for p in service_catalog.PRODUCTS})
SERVICE_KINDS = frozenset(SERVICE_TYPES)
# what a template's `requires` can name that is a service rather than a capability
REQUIRABLE_SERVICES = SERVICE_KINDS - CAPABILITY_KINDS


class ServiceIn(BaseModel):
    kind: ServiceKind
    # unique on the node; the kind when left empty (a second service of a kind needs its own name)
    name: str | None = Field(default=None, pattern=NAME)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    credential_id: uuid.UUID | None = None

    @field_validator("kind")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in SERVICE_TYPES:
            raise ValueError("unknown service kind")
        return value

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
    table = "node_service" if requirement in REQUIRABLE_SERVICES else "node_capability"
    found = await conn.scalar(
        text(f"SELECT 1 FROM {table} WHERE node_id = :n AND kind = :k AND enabled"),  # noqa: S608
        {"n": node_id, "k": requirement},
    )
    return bool(found)


def missing_code(requirement: str) -> str:
    """The API error for a node that lacks what a check or template needs."""
    return f"no_{requirement}_{'service' if requirement in REQUIRABLE_SERVICES else 'capability'}"


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
            "category": t.category,
            "defaults": t.config().model_dump(mode="json"),
        }
        for t in SERVICE_TYPES.values()
    ]
