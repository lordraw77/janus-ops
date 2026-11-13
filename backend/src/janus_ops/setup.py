"""Initial administrator: created in the directory from JANUS_ADMIN_* if it does not exist.

Create-only on purpose: once the account exists its password is managed in the UI and
never overwritten from .env again.
"""

import logging

from sqlalchemy import text

from janus_ops import audit
from janus_ops.config import Settings
from janus_ops.db import create_engine
from janus_ops.directory import LldapAdmin
from janus_ops.rbac import ADMIN_ROLE

log = logging.getLogger(__name__)

MIN_PASSWORD = 12


class SetupError(Exception):
    pass


async def ensure_admin(settings: Settings, directory: LldapAdmin | None = None) -> str:
    """Return "created", "exists" or "skipped"."""
    if settings.admin_password is None:
        log.info("JANUS_ADMIN_PASSWORD not set: no initial administrator created")
        return "skipped"
    if not settings.directory_managed:
        log.warning("directory not managed by Janus-Ops: create the administrator there")
        return "skipped"
    password = settings.admin_password.get_secret_value()
    if len(password) < MIN_PASSWORD:
        raise SetupError(f"JANUS_ADMIN_PASSWORD must be at least {MIN_PASSWORD} characters")

    admin = directory or LldapAdmin(settings)
    username = settings.admin_username
    if any(u.id == username for u in await admin.list_users()):
        log.info("initial administrator already exists: left unchanged")
        return "exists"

    engine = create_engine(settings)
    try:
        async with engine.connect() as conn:
            group = await conn.scalar(
                text("SELECT ldap_group FROM role WHERE name = :n"), {"n": ADMIN_ROLE}
            )
        await admin.create_user(
            username,
            settings.admin_email or f"{username}@janus-ops.invalid",
            "Janus-Ops administrator",
            password,
            {str(group)},
        )
        async with engine.begin() as conn:
            await audit.append(
                conn,
                action="users.created",
                actor_name="setup",
                target_type="user",
                target_id=username,
                payload={"roles": [ADMIN_ROLE], "initial_admin": True},
            )
    finally:
        await engine.dispose()
    log.info("initial administrator created")
    return "created"
