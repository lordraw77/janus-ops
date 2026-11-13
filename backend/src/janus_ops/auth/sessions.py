"""Server-side sessions. The browser holds a random token; the database stores only its hash."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

COOKIE_NAME = "janus_session"
_TOUCH_EVERY = timedelta(seconds=60)  # limit last_seen_at writes


@dataclass(frozen=True)
class Session:
    id: str
    user_id: str
    username: str
    display_name: str | None
    role: str
    method: str
    expires_at: datetime


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def create(
    conn: AsyncConnection, *, user_id: str, role: str, ttl_s: int
) -> tuple[str, str, datetime]:
    """Return (token, session_id, expires_at)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_s)
    session_id = await conn.scalar(
        text(
            "INSERT INTO user_session (user_id, token_hash, role, created_at, expires_at,"
            " last_seen_at) VALUES (CAST(:user_id AS uuid), :token_hash, :role, :now,"
            " :expires_at, :now) RETURNING id"
        ),
        {
            "user_id": user_id,
            "token_hash": _hash(token),
            "role": role,
            "now": now,
            "expires_at": expires_at,
        },
    )
    return token, str(session_id), expires_at


async def lookup(conn: AsyncConnection, token: str, *, idle_s: int) -> Session | None:
    """Valid session for the token, or None. Idle sessions are revoked on the spot."""
    row = (
        await conn.execute(
            text(
                "SELECT s.id, s.user_id, s.role, s.expires_at, s.last_seen_at,"
                " u.username, u.display_name, u.is_breakglass"
                " FROM user_session s JOIN app_user u ON u.id = s.user_id"
                " WHERE s.token_hash = :h AND s.revoked_at IS NULL AND s.expires_at > now()"
                " AND u.is_active"
            ),
            {"h": _hash(token)},
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
    return Session(
        id=str(row.id),
        user_id=str(row.user_id),
        username=row.username,
        display_name=row.display_name,
        role=row.role,
        method="breakglass" if row.is_breakglass else "ldap",
        expires_at=row.expires_at,
    )


async def revoke(conn: AsyncConnection, session_id: str) -> None:
    await conn.execute(
        text(
            "UPDATE user_session SET revoked_at = now()"
            " WHERE id = CAST(:id AS uuid) AND revoked_at IS NULL"
        ),
        {"id": session_id},
    )
