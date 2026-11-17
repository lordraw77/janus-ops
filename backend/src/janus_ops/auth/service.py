"""Login flow: rate limit, break-glass or LDAP, roles, upsert, optional TOTP, session, audit."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from janus_ops import audit
from janus_ops.auth import mfa, sessions
from janus_ops.auth.ldap import DirectoryBackend, DirectoryUnavailable, InvalidCredentials
from janus_ops.auth.passwords import verify_password
from janus_ops.auth.ratelimit import LoginRateLimiter
from janus_ops.config import Settings
from janus_ops.crypto import Box
from janus_ops.rbac import ADMIN_ROLE, roles_for_groups
from janus_ops.transit import build_box

log = logging.getLogger(__name__)

MAX_MFA_FAILURES = 5


class LoginError(Exception):
    """Login refused. `code` is returned to the client; `status` is the HTTP status."""

    def __init__(self, code: str, status: int, retry_after: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class LoginResult:
    token: str
    mfa_required: bool
    ttl_s: int


class AuthService:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        directory: DirectoryBackend | None,
        user_limiter: LoginRateLimiter | None = None,
        ip_limiter: LoginRateLimiter | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._directory = directory
        self._user_limiter = user_limiter or LoginRateLimiter(max_failures=5, window_s=900)
        self._ip_limiter = ip_limiter or LoginRateLimiter(max_failures=30, window_s=900)
        self.box: Box | None = build_box(settings)

    async def _fail(
        self, username: str, ip: str | None, reason: str, status: int, *, count: bool
    ) -> LoginError:
        if count:
            self._user_limiter.record_failure(username)
            if ip:
                self._ip_limiter.record_failure(ip)
        async with self._engine.begin() as conn:
            await audit.append(
                conn,
                action="auth.login_failed",
                actor_name=username,
                source_ip=ip,
                payload={"reason": reason},
            )
        log.info("login refused", extra={"reason": reason})
        return LoginError(reason, status)

    async def login(self, username: str, password: str, ip: str | None) -> LoginResult:
        username = username.lower()
        wait = max(
            self._user_limiter.retry_after(username),
            self._ip_limiter.retry_after(ip) if ip else 0,
        )
        if wait:
            log.warning("login rate limited")
            raise LoginError("too_many_attempts", 429, retry_after=wait)

        async with self._engine.connect() as conn:
            breakglass = (
                await conn.execute(
                    text(
                        "SELECT id, password_hash, is_active, totp_enabled FROM app_user"
                        " WHERE is_breakglass AND lower(username) = :u"
                    ),
                    {"u": username},
                )
            ).first()

        if breakglass is not None:
            # The break-glass name never falls through to LDAP
            if not (breakglass.is_active and verify_password(password, breakglass.password_hash)):
                raise await self._fail(username, ip, "invalid_credentials", 401, count=True)
            return await self._open_session(
                str(breakglass.id),
                username,
                [ADMIN_ROLE],
                "breakglass",
                ip,
                totp_enabled=breakglass.totp_enabled,
            )

        if self._directory is None:
            raise await self._fail(username, ip, "invalid_credentials", 401, count=True)
        try:
            user = await self._directory.authenticate(username, password)
        except InvalidCredentials:
            raise await self._fail(username, ip, "invalid_credentials", 401, count=True) from None
        except DirectoryUnavailable:
            log.error("LDAP directory unavailable")
            raise await self._fail(
                username, ip, "directory_unavailable", 503, count=False
            ) from None

        async with self._engine.connect() as conn:
            roles = await roles_for_groups(conn, user.groups)
        if not roles:
            raise await self._fail(user.username, ip, "no_role", 403, count=False)

        try:
            async with self._engine.begin() as conn:
                row = (
                    await conn.execute(
                        text(
                            "INSERT INTO app_user (username, ldap_dn, display_name, email,"
                            " last_login_at) VALUES (:u, :dn, :name, :mail, :now)"
                            " ON CONFLICT (ldap_dn) DO UPDATE SET username = EXCLUDED.username,"
                            " display_name = EXCLUDED.display_name, email = EXCLUDED.email"
                            " RETURNING id, is_active, totp_enabled"
                        ),
                        {
                            "u": user.username,
                            "dn": user.dn,
                            "name": user.display_name,
                            "mail": user.email,
                            "now": datetime.now(UTC),
                        },
                    )
                ).one()
        except IntegrityError:
            # Same username already bound to another DN (or to the break-glass account);
            # the exception leaves the block, so the transaction is rolled back, not committed
            row = None
        if row is None:
            raise await self._fail(user.username, ip, "account_conflict", 403, count=False)
        if not row.is_active:
            raise await self._fail(user.username, ip, "account_disabled", 403, count=False)
        return await self._open_session(
            str(row.id), user.username, roles, "ldap", ip, totp_enabled=row.totp_enabled
        )

    async def _open_session(
        self,
        user_id: str,
        username: str,
        roles: list[str],
        method: str,
        ip: str | None,
        *,
        totp_enabled: bool,
        factor: str | None = None,
    ) -> LoginResult:
        self._user_limiter.reset(username)
        async with self._engine.begin() as conn:
            if totp_enabled and factor is None:
                token, _session_id, _ = await sessions.create(
                    conn,
                    user_id=user_id,
                    roles=roles,
                    ttl_s=sessions.MFA_PENDING_TTL_S,
                    stage="mfa_pending",
                )
                await audit.append(
                    conn,
                    action="auth.password_verified",
                    actor_id=user_id,
                    actor_name=username,
                    source_ip=ip,
                    payload={"method": method, "next": "totp"},
                )
                return LoginResult(token, True, sessions.MFA_PENDING_TTL_S)

            await conn.execute(
                text("UPDATE app_user SET last_login_at = now() WHERE id = CAST(:id AS uuid)"),
                {"id": user_id},
            )
            token, session_id, _ = await sessions.create(
                conn, user_id=user_id, roles=roles, ttl_s=self._settings.session_ttl_s
            )
            payload: dict[str, object] = {
                "method": method,
                "roles": roles,
                "session_id": session_id,
            }
            if factor:
                payload["mfa"] = factor
            await audit.append(
                conn,
                action="auth.login",
                actor_id=user_id,
                actor_name=username,
                source_ip=ip,
                payload=payload,
            )
        if method == "breakglass":
            log.warning("break-glass account used")
        return LoginResult(token, False, self._settings.session_ttl_s)

    async def complete_mfa(
        self, pending: sessions.Session, code: str, ip: str | None
    ) -> LoginResult:
        """Second step: a TOTP or recovery code turns the pending session into a full one."""
        locked = False
        async with self._engine.begin() as conn:
            try:
                factor = await mfa.verify_factor(conn, self.box, pending.user_id, code)
            except mfa.TotpUnavailable:
                raise LoginError("secret_manager_unavailable", 503) from None
            if factor is None:
                failures = await sessions.record_mfa_failure(conn, pending.id)
                locked = failures >= MAX_MFA_FAILURES
                if locked:
                    await sessions.revoke(conn, pending.id)
                await audit.append(
                    conn,
                    action="auth.login_failed",
                    actor_id=pending.user_id,
                    actor_name=pending.username,
                    source_ip=ip,
                    payload={"reason": "mfa_locked" if locked else "invalid_code"},
                )
            else:
                await sessions.revoke(conn, pending.id)  # a new token is issued below
        if factor is None:
            raise LoginError("mfa_locked" if locked else "invalid_code", 401)
        return await self._open_session(
            pending.user_id,
            pending.username,
            list(pending.roles),
            pending.method,
            ip,
            totp_enabled=True,
            factor=factor,
        )

    async def logout(
        self, conn: AsyncConnection, session: sessions.Session, ip: str | None
    ) -> None:
        await sessions.revoke(conn, session.id)
        await audit.append(
            conn,
            action="auth.logout",
            actor_id=session.user_id,
            actor_name=session.username,
            source_ip=ip,
            payload={"session_id": session.id},
        )
