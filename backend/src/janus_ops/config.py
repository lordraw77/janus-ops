"""Runtime configuration, read from JANUS_* environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    """Every field maps to JANUS_<FIELD_NAME>. Documented in deploy/.env.example."""

    model_config = SettingsConfigDict(env_prefix="JANUS_", extra="ignore", frozen=True)

    database_url: SecretStr = Field(
        description="SQLAlchemy URL, driver must be postgresql+asyncpg",
    )
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: LogLevel = "INFO"
    static_dir: Path | None = Field(
        default=None,
        description="Directory with the exported web UI; unset = API only",
    )
    db_connect_timeout_s: float = Field(default=5.0, gt=0, le=60)
    forwarded_allow_ips: str = Field(
        default="127.0.0.1",
        description="Proxies trusted for X-Forwarded-* headers (uvicorn syntax)",
    )

    # --- LDAP directory (ADR-008). ldap_url unset = LDAP login disabled (break-glass only)
    ldap_url: str | None = Field(default=None, description="ldap://host:port or ldaps://host:port")
    ldap_base_dn: str = ""
    ldap_user_base_dn: str | None = Field(
        default=None, description="Where users live; default ou=people,<base_dn>"
    )
    ldap_user_filter: str = Field(
        default="(&(objectClass=person)(uid={username}))",
        description="{username} is replaced by the escaped login name",
    )
    ldap_bind_dn: str | None = Field(default=None, description="Read-only service account")
    ldap_bind_password: SecretStr | None = None
    ldap_group_admin: str = "janus-admins"
    ldap_group_operator: str = "janus-operators"
    ldap_group_auditor: str = "janus-auditors"
    ldap_timeout_s: int = Field(default=5, ge=1, le=60)  # ldap3 needs whole seconds
    ldap_ca_file: Path | None = Field(
        default=None, description="CA bundle to verify ldaps:// servers; default system CAs"
    )

    # --- Sessions
    session_ttl_s: int = Field(default=8 * 3600, ge=300, le=7 * 86400)
    session_idle_s: int = Field(default=3600, ge=60, le=86400)
    cookie_secure: bool = Field(default=True, description="Send the session cookie over HTTPS only")

    # --- Break-glass local account (FR-06)
    breakglass_username: str = Field(default="breakglass", pattern=r"^[a-z0-9._-]{3,64}$")

    @model_validator(mode="after")
    def _ldap_complete(self) -> "Settings":
        if self.ldap_url is None:
            return self
        if not self.ldap_url.startswith(("ldap://", "ldaps://")):
            raise ValueError("JANUS_LDAP_URL must start with ldap:// or ldaps://")
        missing = [
            name
            for name, value in (
                ("JANUS_LDAP_BASE_DN", self.ldap_base_dn),
                ("JANUS_LDAP_BIND_DN", self.ldap_bind_dn),
                ("JANUS_LDAP_BIND_PASSWORD", self.ldap_bind_password),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"LDAP enabled but missing: {', '.join(missing)}")
        if "{username}" not in self.ldap_user_filter:
            raise ValueError("JANUS_LDAP_USER_FILTER must contain {username}")
        return self

    @property
    def ldap_users_dn(self) -> str:
        return self.ldap_user_base_dn or f"ou=people,{self.ldap_base_dn}"

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("must start with postgresql+asyncpg://")
        return value

    @field_validator("static_dir")
    @classmethod
    def _static_dir_exists(cls, value: Path | None) -> Path | None:
        if value is not None and not (value / "index.html").is_file():
            raise ValueError(f"{value} does not contain index.html")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment (pydantic plugin knows)
