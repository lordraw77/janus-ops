"""Create or rotate the break-glass account (FR-06). Used by `janus-ops breakglass`."""

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from janus_ops import audit
from janus_ops.auth.passwords import hash_password
from janus_ops.config import Settings
from janus_ops.db import create_engine


class BreakglassError(Exception):
    pass


async def set_password(settings: Settings, password: str) -> str:
    """Create the account or replace its password. Returns "created" or "rotated"."""
    encoded = hash_password(password)  # raises ValueError if too short
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            updated = await conn.execute(
                text(
                    "UPDATE app_user SET password_hash = :h, username = :u, is_active = true"
                    " WHERE is_breakglass"
                ),
                {"h": encoded, "u": settings.breakglass_username},
            )
            outcome = "rotated"
            if updated.rowcount == 0:
                await conn.execute(
                    text(
                        "INSERT INTO app_user (username, display_name, is_breakglass,"
                        " password_hash) VALUES (:u, 'Break-glass administrator', true, :h)"
                    ),
                    {"u": settings.breakglass_username, "h": encoded},
                )
                outcome = "created"
            # Existing sessions of the account must not survive a password change
            await conn.execute(
                text(
                    "UPDATE user_session SET revoked_at = now() WHERE revoked_at IS NULL"
                    " AND user_id IN (SELECT id FROM app_user WHERE is_breakglass)"
                )
            )
            await audit.append(
                conn,
                action=f"auth.breakglass_{outcome}",
                actor_name="cli",
                target_type="user",
                target_id=settings.breakglass_username,
            )
    except IntegrityError as exc:
        raise BreakglassError(
            f"username '{settings.breakglass_username}' already belongs to a directory user;"
            " set JANUS_BREAKGLASS_USERNAME to another name"
        ) from exc
    finally:
        await engine.dispose()
    return outcome
