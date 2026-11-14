"""Server-side sessions. The browser holds a random token; the database stores only its hash.

A session's roles are fixed at sign-in (LDAP groups are read then); its permissions are
recomputed on every request, so permission changes made by an admin apply immediately.
Stage 'mfa_pending' = password accepted, TOTP still required: only the TOTP step accepts it.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.rbac import permissions_for
from janus_ops.scope import UNRESTRICTED, Scope, scope_for

COOKIE_NAME = "janus_session"
MFA_PENDING_TTL_S = 300
_TOUCH_EVERY = timedelta(seconds=60)  # limit last_seen_at writes

Stage = Literal["mfa_pending", "full"]


@dataclass(frozen=True)
class Session:
    id: str
    user_id: str
    username: str
    display_name: str | None
    roles: tuple[str, ...]
    permissions: frozenset[str]
    method: str
    stage: Stage
    totp_enabled: bool
    mfa_failures: int
    expires_at: datetime
    scope: Scope = UNRESTRICTED  # nodes this user may see (per-role)


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def create(
    conn: AsyncConnection,
    *,
    user_id: str,
    roles: list[str],
    ttl_s: int,
    stage: Stage = "full",
) -> tuple[str, str, datetime]:
    """Return (token, session_id, expires_at)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_s)
    session_id = await conn.scalar(
        text(
            "INSERT INTO user_session (user_id, token_hash, roles, stage, created_at,"
            " expires_at, last_seen_at) VALUES (CAST(:user_id AS uuid), :token_hash, :roles,"
            " :stage, :now, :expires_at, :now) RETURNING id"
        ),
        {
            "user_id": user_id,
            "token_hash": _hash(token),
            "roles": roles,
            "stage": stage,
            "now": now,
            "expires_at": expires_at,
        },
    )
    return token, str(session_id), expires_at


async def lookup(
    conn: AsyncConnection, token: str, *, idle_s: int, stage: Stage = "full"
) -> Session | None:
    """Valid session of the given stage, or None. Idle sessions are revoked on the spot."""
    row = (
        await conn.execute(
            text(
                "SELECT s.id, s.user_id, s.roles, s.stage, s.mfa_failures, s.expires_at,"
                " s.last_seen_at, u.username, u.display_name, u.is_breakglass, u.totp_enabled"
                " FROM user_session s JOIN app_user u ON u.id = s.user_id"
                " WHERE s.token_hash = :h AND s.stage = :stage AND s.revoked_at IS NULL"
                " AND s.expires_at > now() AND u.is_active"
            ),
            {"h": _hash(token), "stage": stage},
        )
    ).first()
    if row is None:
        return None

    now = datetime.now(UTC)
    if row.last_seen_at is not None and now - row.last_seen_at > timedelta(seconds=idle_s):
        await revoke(conn, str(row.id))
        return None
    if row.last_seen_at is None or now - row.last_seen_at > _TOUCH_EVERY:
        await conn.execute(
            text("UPDATE user_session SET last_seen_at = :now WHERE id = :id"),
            {"now": now, "id": row.id},
        )
    roles = tuple(row.roles)
    return Session(
        id=str(row.id),
        user_id=str(row.user_id),
        username=row.username,
        display_name=row.display_name,
        roles=roles,
        permissions=await permissions_for(conn, roles),
        scope=await scope_for(conn, roles),
        method="breakglass" if row.is_breakglass else "ldap",
        stage=row.stage,
        totp_enabled=row.totp_enabled,
        mfa_failures=row.mfa_failures,
        expires_at=row.expires_at,
    )


async def record_mfa_failure(conn: AsyncConnection, session_id: str) -> int:
    failures: int = await conn.scalar(
        text(
            "UPDATE user_session SET mfa_failures = mfa_failures + 1"
            " WHERE id = CAST(:id AS uuid) RETURNING mfa_failures"
        ),
        {"id": session_id},
    )
    return failures


async def revoke(conn: AsyncConnection, session_id: str) -> None:
    await conn.execute(
        text(
            "UPDATE user_session SET revoked_at = now()"
            " WHERE id = CAST(:id AS uuid) AND revoked_at IS NULL"
        ),
        {"id": session_id},
    )


async def revoke_user(conn: AsyncConnection, user_id: str) -> None:
    await conn.execute(
        text(
            "UPDATE user_session SET revoked_at = now()"
            " WHERE user_id = CAST(:id AS uuid) AND revoked_at IS NULL"
        ),
        {"id": user_id},
    )
