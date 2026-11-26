"""/api/v1/credentials: vault entries. Secrets are write-only, with one exception: `reveal` shows a
secret to a user with `credentials.reveal`, writes the audit event first and is never cached."""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from janus_ops.admin.common import record
from janus_ops.auth.deps import require_permission
from janus_ops.auth.sessions import Session
from janus_ops.crypto import DecryptionError
from janus_ops.vault import InvalidSecret, Sealed, Vault, VaultUnavailable, inspect_ssh_key

log = logging.getLogger(__name__)
router = APIRouter(prefix="/credentials", tags=["credentials"])

Reader = Annotated[Session, Depends(require_permission("inventory.read"))]
Manager = Annotated[Session, Depends(require_permission("credentials.manage"))]
Revealer = Annotated[Session, Depends(require_permission("credentials.reveal"))]

Kind = Literal[
    "ssh_key",
    "ssh_password",
    "sudo_password",
    "proxmox_token",
    "proxmox_password",
    "fritzbox_password",
    "postgres_password",
    "git_token",
    "docker_registry",
    "snmp_v3",
    "notification_secret",
    "generic",
]


class Usage(BaseModel):
    node_id: str
    node_name: str
    capability: str


class CredentialOut(BaseModel):
    id: str
    name: str
    kind: str
    username: str | None
    description: str | None
    public_info: dict[str, Any]
    used_by: list[Usage]
    created_at: datetime
    updated_at: datetime


class SecretIn(BaseModel):
    secret: str = Field(min_length=1, max_length=16384, repr=False)
    passphrase: str | None = Field(default=None, max_length=1024, repr=False)


class CredentialCreate(SecretIn):
    name: str = Field(min_length=1, max_length=128)
    kind: Kind
    username: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=1000)


class CredentialUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    username: str | None = Field(default=None, max_length=256)
    description: str | None = Field(default=None, max_length=1000)


# Proxmox user ids carry their realm: root@pam, admin@pve; API tokens add "!<token id>"
_PVE_USER = r"[^\s@:!]+@[A-Za-z0-9][A-Za-z0-9._-]*"
USERNAME_RULES: dict[str, tuple[re.Pattern[str], str]] = {
    # SNMPv3 user names are at most 32 octets (RFC 3414); no spaces
    "snmp_v3": (re.compile(r"^[A-Za-z0-9._@-]{1,32}$"), "SNMPv3 user name, up to 32 characters"),
    "proxmox_password": (re.compile(rf"^{_PVE_USER}$"), "user@realm, e.g. root@pam"),
    # the box's TR-064 user (System → FRITZ!Box-Benutzer): a plain name
    "fritzbox_password": (re.compile(r"^[^\s]{1,64}$"), "the FRITZ!Box user name, no spaces"),
    # a PostgreSQL role name: grant it pg_monitor, nothing more
    "postgres_password": (
        re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.$@-]{0,62}$"),
        "the PostgreSQL role, e.g. janus_monitor",
    ),
    "proxmox_token": (
        re.compile(rf"^{_PVE_USER}![A-Za-z][A-Za-z0-9._-]*$"),
        "user@realm!token, e.g. root@pam!janus",
    ),
}


def _check_username(kind: str, username: str | None) -> None:
    rule = USERNAME_RULES.get(kind)
    if rule is not None and not (username and rule[0].fullmatch(username)):
        raise HTTPException(422, f"invalid_username:{rule[1]}")


def _vault(request: Request) -> Vault:
    vault: Vault = request.app.state.vault
    return vault


def _prepare(kind: str, body: SecretIn) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (secret payload, public info). Raises HTTP 422 with a safe message."""
    payload: dict[str, Any] = {"secret": body.secret}
    public: dict[str, Any] = {}
    if kind == "ssh_key":
        try:
            public = inspect_ssh_key(body.secret, body.passphrase)
        except InvalidSecret as exc:
            raise HTTPException(422, f"invalid_secret:{exc}") from None
        if body.passphrase:
            payload["passphrase"] = body.passphrase
    elif kind == "snmp_v3":
        # secret: the authentication passphrase; passphrase: the privacy passphrase (RFC 3414
        # requires at least 8 characters for both)
        if not body.passphrase:
            raise HTTPException(422, "invalid_secret:SNMPv3 needs a privacy passphrase")
        if min(len(body.secret), len(body.passphrase)) < 8:
            raise HTTPException(422, "invalid_secret:SNMPv3 passphrases need 8 characters or more")
        payload["passphrase"] = body.passphrase
    elif body.passphrase:
        raise HTTPException(
            422, "invalid_secret:a passphrase is only used with SSH keys and SNMPv3"
        )
    return payload, public


def _seal(request: Request, credential_id: str, payload: dict[str, Any]) -> Sealed:
    try:
        return _vault(request).seal(credential_id, payload)
    except VaultUnavailable:
        raise HTTPException(503, "vault_unavailable") from None


@router.get("", response_model=list[CredentialOut])
async def list_credentials(request: Request, _: Reader) -> list[CredentialOut]:
    async with request.app.state.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, name, kind, username, description, public_info, created_at,"
                    " updated_at FROM credential ORDER BY lower(name)"
                )
            )
        ).all()
        uses = (
            await conn.execute(
                text(
                    "SELECT c.credential_id, n.id AS node_id, n.name, c.kind"
                    " FROM node_capability c JOIN node n ON n.id = c.node_id"
                    " WHERE c.credential_id IS NOT NULL"
                    " UNION ALL"
                    " SELECT s.credential_id, n.id, n.name, s.kind"
                    " FROM node_service s JOIN node n ON n.id = s.node_id"
                    " WHERE s.credential_id IS NOT NULL ORDER BY 3"
                )
            )
        ).all()
    by_cred: dict[str, list[Usage]] = {}
    for u in uses:
        by_cred.setdefault(str(u.credential_id), []).append(
            Usage(node_id=str(u.node_id), node_name=u.name, capability=u.kind)
        )
    return [
        CredentialOut(
            id=str(r.id),
            name=r.name,
            kind=r.kind,
            username=r.username,
            description=r.description,
            public_info=r.public_info,
            used_by=by_cred.get(str(r.id), []),
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


@router.post("", status_code=201, responses={409: {}, 422: {}, 503: {}})
async def create_credential(
    body: CredentialCreate, request: Request, session: Manager
) -> dict[str, str]:
    credential_id = str(uuid.uuid4())
    _check_username(body.kind, body.username)
    payload, public = _prepare(body.kind, body)
    sealed = _seal(request, credential_id, payload)
    try:
        async with request.app.state.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO credential (id, name, kind, username, description, ciphertext,"
                    " nonce, dek_wrapped, kek_version, public_info, created_by)"
                    " VALUES (CAST(:id AS uuid), :name, :kind, :username, :description, :ct,"
                    " :nonce, :dek, :kv, CAST(:public AS jsonb), CAST(:by AS uuid))"
                ),
                {
                    "id": credential_id,
                    "name": body.name,
                    "kind": body.kind,
                    "username": body.username,
                    "description": body.description,
                    "ct": sealed.ciphertext,
                    "nonce": sealed.nonce,
                    "dek": sealed.dek_wrapped,
                    "kv": sealed.kek_version,
                    "public": json.dumps(public),
                    "by": session.user_id,
                },
            )
    except IntegrityError:
        raise HTTPException(409, "name_exists") from None
    await record(
        request,
        session,
        "credentials.created",
        "credential",
        credential_id,
        {"name": body.name, "kind": body.kind, **public},
    )
    return {"id": credential_id}


async def _kind(request: Request, credential_id: uuid.UUID) -> str:
    async with request.app.state.engine.connect() as conn:
        kind = await conn.scalar(
            text("SELECT kind FROM credential WHERE id = :id"), {"id": credential_id}
        )
    if kind is None:
        raise HTTPException(404, "not_found")
    return str(kind)


@router.post("/{credential_id}/reveal", responses={404: {}, 409: {}, 503: {}})
async def reveal_secret(
    credential_id: uuid.UUID, request: Request, response: Response, session: Revealer
) -> dict[str, Any]:
    """The secret in clear. The audit event is written BEFORE the answer leaves: if it cannot be
    written, nothing is shown."""
    async with request.app.state.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT name, kind, username, ciphertext, nonce, dek_wrapped"
                    " FROM credential WHERE id = :id"
                ),
                {"id": credential_id},
            )
        ).first()
    if row is None:
        raise HTTPException(404, "not_found")
    try:
        secret = _vault(request).reveal(
            str(credential_id), row.ciphertext, row.nonce, row.dek_wrapped
        )
    except VaultUnavailable:
        raise HTTPException(503, "vault_unavailable") from None
    except DecryptionError:
        raise HTTPException(409, "credential_unreadable") from None
    await record(
        request,
        session,
        "credentials.revealed",
        "credential",
        str(credential_id),
        {"name": row.name, "kind": row.kind},
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "username": row.username,
        "secret": str(secret.get("secret", "")),
        "passphrase": str(secret["passphrase"]) if secret.get("passphrase") else None,
    }


@router.put("/{credential_id}/secret", status_code=204, responses={404: {}, 422: {}, 503: {}})
async def replace_secret(
    credential_id: uuid.UUID, body: SecretIn, request: Request, session: Manager
) -> Response:
    payload, public = _prepare(await _kind(request, credential_id), body)
    sealed = _seal(request, str(credential_id), payload)
    async with request.app.state.engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE credential SET ciphertext = :ct, nonce = :nonce, dek_wrapped = :dek,"
                " kek_version = :kv, public_info = CAST(:public AS jsonb), updated_at = now()"
                " WHERE id = :id"
            ),
            {
                "ct": sealed.ciphertext,
                "nonce": sealed.nonce,
                "dek": sealed.dek_wrapped,
                "kv": sealed.kek_version,
                "public": json.dumps(public),
                "id": credential_id,
            },
        )
    await record(request, session, "credentials.rotated", "credential", str(credential_id), public)
    return Response(status_code=204)


@router.patch("/{credential_id}", status_code=204, responses={404: {}, 409: {}})
async def update_credential(
    credential_id: uuid.UUID, body: CredentialUpdate, request: Request, session: Manager
) -> Response:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return Response(status_code=204)
    kind = await _kind(request, credential_id)
    if "username" in changes:
        _check_username(kind, changes["username"])
    sets = ", ".join(f"{k} = :{k}" for k in changes)  # keys come from the pydantic model only
    try:
        async with request.app.state.engine.begin() as conn:
            await conn.execute(
                text(f"UPDATE credential SET {sets}, updated_at = now() WHERE id = :id"),  # noqa: S608
                {**changes, "id": credential_id},
            )
    except IntegrityError:
        raise HTTPException(409, "name_exists") from None
    await record(request, session, "credentials.updated", "credential", str(credential_id), changes)
    return Response(status_code=204)


@router.delete("/{credential_id}", status_code=204, responses={404: {}, 409: {}})
async def delete_credential(
    credential_id: uuid.UUID, request: Request, session: Manager
) -> Response:
    await _kind(request, credential_id)
    try:
        async with request.app.state.engine.begin() as conn:
            await conn.execute(text("DELETE FROM credential WHERE id = :id"), {"id": credential_id})
    except IntegrityError:
        raise HTTPException(409, "credential_in_use") from None
    try:  # the content of a secret kept in KV goes with it; a failure leaves an orphan for prune
        await asyncio.to_thread(request.app.state.vault.discard, str(credential_id))
    except Exception:  # noqa: BLE001 - the credential is already gone; do not fail the request
        log.warning("secret content could not be removed from the secret manager")
    await record(request, session, "credentials.deleted", "credential", str(credential_id))
    return Response(status_code=204)
