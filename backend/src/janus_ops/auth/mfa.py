"""Optional TOTP second factor: enrolment, verification, recovery codes, admin reset."""

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.auth import totp
from janus_ops.crypto import DecryptionError, SecretBox


class TotpUnavailable(Exception):
    """JANUS_SECRET_KEY is not configured: secrets cannot be stored."""


class TotpStateError(Exception):
    """Operation not valid in the current state (code is the API error code)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Enrollment:
    secret: str
    uri: str
    qr_svg: str


Factor = Literal["totp", "recovery_code"]


def _aad(user_id: str) -> bytes:
    return f"totp:{user_id}".encode()


def _box(box: SecretBox | None) -> SecretBox:
    if box is None:
        raise TotpUnavailable
    return box


async def status(conn: AsyncConnection, user_id: str) -> tuple[bool, int]:
    row = (
        await conn.execute(
            text(
                "SELECT u.totp_enabled, (SELECT count(*) FROM totp_recovery_code c"
                " WHERE c.user_id = u.id AND c.used_at IS NULL) AS left"
                " FROM app_user u WHERE u.id = CAST(:id AS uuid)"
            ),
            {"id": user_id},
        )
    ).one()
    return bool(row.totp_enabled), int(row.left)


async def start_enrollment(
    conn: AsyncConnection, box: SecretBox | None, user_id: str, account: str
) -> Enrollment:
    sb = _box(box)
    enabled, _ = await status(conn, user_id)
    if enabled:
        raise TotpStateError("totp_already_enabled")
    secret = totp.new_secret()
    await conn.execute(
        text(
            "UPDATE app_user SET totp_secret_enc = :enc, totp_last_step = NULL"
            " WHERE id = CAST(:id AS uuid)"
        ),
        {"enc": sb.encrypt(secret.encode(), _aad(user_id)), "id": user_id},
    )
    uri = totp.provisioning_uri(secret, account)
    return Enrollment(secret=secret, uri=uri, qr_svg=totp.qr_svg(uri))


async def _secret(
    conn: AsyncConnection, sb: SecretBox, user_id: str
) -> tuple[str, int | None, bool]:
    row = (
        await conn.execute(
            text(
                "SELECT totp_secret_enc, totp_last_step, totp_enabled FROM app_user"
                " WHERE id = CAST(:id AS uuid) FOR UPDATE"
            ),
            {"id": user_id},
        )
    ).one()
    if row.totp_secret_enc is None:
        raise TotpStateError("totp_not_started")
    try:
        secret = sb.decrypt(bytes(row.totp_secret_enc), _aad(user_id)).decode()
    except DecryptionError as exc:
        # Wrong/rotated JANUS_SECRET_KEY: the user must be reset by an admin
        raise TotpStateError("totp_secret_unreadable") from exc
    return secret, row.totp_last_step, bool(row.totp_enabled)


async def _store_recovery_codes(conn: AsyncConnection, user_id: str) -> list[str]:
    await conn.execute(
        text("DELETE FROM totp_recovery_code WHERE user_id = CAST(:id AS uuid)"), {"id": user_id}
    )
    codes = totp.new_recovery_codes()
    for code in codes:
        await conn.execute(
            text(
                "INSERT INTO totp_recovery_code (user_id, code_hash) VALUES (CAST(:id AS uuid), :h)"
            ),
            {"id": user_id, "h": totp.hash_recovery_code(code)},
        )
    return codes


async def confirm_enrollment(
    conn: AsyncConnection, box: SecretBox | None, user_id: str, code: str
) -> list[str]:
    """Activate TOTP once the user proves the app works. Returns the recovery codes."""
    secret, last_step, enabled = await _secret(conn, _box(box), user_id)
    if enabled:
        raise TotpStateError("totp_already_enabled")
    step = totp.verify(secret, code, last_step=last_step)
    if step is None:
        raise TotpStateError("invalid_code")
    await conn.execute(
        text(
            "UPDATE app_user SET totp_enabled = true, totp_last_step = :s"
            " WHERE id = CAST(:id AS uuid)"
        ),
        {"s": step, "id": user_id},
    )
    return await _store_recovery_codes(conn, user_id)


async def verify_factor(
    conn: AsyncConnection, box: SecretBox | None, user_id: str, code: str
) -> Factor | None:
    """Check a TOTP code or an unused recovery code for an enrolled user."""
    candidate = code.strip()
    if "-" in candidate or len(candidate) > totp.DIGITS:
        used = await conn.scalar(
            text(
                "UPDATE totp_recovery_code SET used_at = now()"
                " WHERE user_id = CAST(:id AS uuid) AND code_hash = :h AND used_at IS NULL"
                " RETURNING 1"
            ),
            {"id": user_id, "h": totp.hash_recovery_code(candidate)},
        )
        return "recovery_code" if used else None

    secret, last_step, enabled = await _secret(conn, _box(box), user_id)
    if not enabled:
        return None
    step = totp.verify(secret, candidate, last_step=last_step)
    if step is None:
        return None
    await conn.execute(
        text("UPDATE app_user SET totp_last_step = :s WHERE id = CAST(:id AS uuid)"),
        {"s": step, "id": user_id},
    )
    return "totp"


async def regenerate_recovery_codes(
    conn: AsyncConnection, box: SecretBox | None, user_id: str, code: str
) -> list[str]:
    if await verify_factor(conn, box, user_id, code) is None:
        raise TotpStateError("invalid_code")
    return await _store_recovery_codes(conn, user_id)


async def disable(conn: AsyncConnection, user_id: str) -> None:
    """Remove TOTP (self-service after verification, or admin reset)."""
    await conn.execute(
        text(
            "UPDATE app_user SET totp_enabled = false, totp_secret_enc = NULL,"
            " totp_last_step = NULL WHERE id = CAST(:id AS uuid)"
        ),
        {"id": user_id},
    )
    await conn.execute(
        text("DELETE FROM totp_recovery_code WHERE user_id = CAST(:id AS uuid)"), {"id": user_id}
    )
