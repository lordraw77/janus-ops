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
    db_pool_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Connections kept open to the database. 500 agents at 10 s with 200 series"
        " each (the NFR-05 estimate, ~10k points/s) starve a pool of 5",
    )
    db_max_overflow: int = Field(
        default=20, ge=0, le=100, description="Extra connections in a burst"
    )
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
    ldap_username_attr: str = Field(
        default="uid",
        pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$",
        description="Attribute with the login name: uid (lldap, OpenLDAP), sAMAccountName (AD)",
    )
    ldap_start_tls: bool = Field(
        default=False, description="Upgrade an ldap:// connection with StartTLS (verified)"
    )
    ldap_nested_groups: bool = Field(
        default=False,
        description="Active Directory: also resolve groups of groups (matching rule in chain)",
    )
    ldap_group_base_dn: str | None = Field(
        default=None, description="Where groups live for nested lookups; default the base DN"
    )
    ldap_bind_dn: str | None = Field(default=None, description="Read-only service account")
    ldap_bind_password: SecretStr | None = None
    ldap_timeout_s: int = Field(default=5, ge=1, le=60)  # ldap3 needs whole seconds
    ldap_ca_file: Path | None = Field(
        default=None, description="CA bundle to verify ldaps:// servers; default system CAs"
    )

    # --- Directory management (lldap): user/group administration from the UI.
    # Unset = users are managed outside Janus-Ops (e.g. corporate AD), pages are read-only.
    lldap_http_url: str | None = Field(
        default=None, description="lldap web/GraphQL endpoint, e.g. http://ldap:17170"
    )
    ldap_manager_user: str = Field(
        default="janus-manager", description="lldap account in group lldap_admin"
    )
    ldap_manager_password: SecretStr | None = None

    # --- Initial administrator, created in the directory by `janus-ops setup` if missing
    admin_username: str = Field(default="janus-admin", pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    admin_password: SecretStr | None = Field(
        default=None, description="Unset = no initial admin is created"
    )
    admin_email: str | None = None

    # --- Encryption key for secrets at rest (TOTP seeds): 64 hex chars = 32 bytes
    secret_key: SecretStr | None = Field(default=None, description="Unset = TOTP cannot be enabled")

    # --- SSH bastion (F1)
    recordings_dir: Path | None = Field(
        default=None, description="Where terminal recordings (.cast) are written; unset = off"
    )
    terminal_idle_s: int = Field(default=1800, ge=60, le=86400)
    terminal_max_per_user: int = Field(default=10, ge=1, le=100)
    ssh_connect_timeout_s: int = Field(default=10, ge=1, le=120)
    sftp_max_upload_mb: int = Field(default=1024, ge=1, le=102400)
    sftp_idle_s: int = Field(default=300, ge=30, le=3600)

    # --- Ansible catalog (F6): directories a network source may mount (comma-separated).
    # Empty = network sources disabled. Keep it narrow: anything under these roots is readable
    # by everyone with ansible.catalog.manage.
    ansible_mount_roots: str = Field(default="", max_length=4096)

    # --- AI Copilot (F7): an OpenAI-compatible endpoint, self-hosted outside the stack (A6).
    # Unset = the Copilot is disabled. Only https:// endpoints are accepted (C10).
    ai_base_url: str | None = Field(default=None, max_length=2048)
    ai_model: str | None = Field(default=None, max_length=200)
    ai_api_key: SecretStr | None = None
    ai_timeout_s: int = Field(default=120, ge=5, le=900)

    # --- Agent and telemetry (F2)
    agent_dir: Path | None = Field(
        default=None, description="Directory with janus-agent-linux-<arch> binaries for bootstrap"
    )
    agent_release_public_key: str | None = Field(
        default=None,
        description="Base64 ed25519 public key that signs agent releases (janus-ops agent-keygen);"
        " written to new agent installs, enables signed self-update (FR-84)",
    )
    agent_server_url: str | None = Field(
        default=None,
        description="https:// URL agents connect to; unset = the URL the operator is using",
    )
    agent_ca_file: Path | None = Field(
        default=None,
        description="CA certificate (PEM) agents trust for agent_server_url, besides system CAs",
    )
    agent_interval_s: int = Field(default=10, ge=5, le=15)
    agent_enroll_ttl_s: int = Field(default=900, ge=60, le=86400)
    agent_bootstrap_wait_s: int = Field(default=45, ge=0, le=300)

    # --- Alerting (F3): check engine and notification dispatcher run in this process
    monitoring_enabled: bool = Field(
        default=True, description="Run checks and send notifications (off: API only, tests)"
    )

    # --- Sessions
    session_ttl_s: int = Field(default=8 * 3600, ge=300, le=7 * 86400)
    session_idle_s: int = Field(default=3600, ge=60, le=86400)
    cookie_secure: bool = Field(default=True, description="Send the session cookie over HTTPS only")

    # --- Master key in an external secret manager (FR-86, ADR-011). local = JANUS_SECRET_KEY.
    secret_backend: Literal["local", "openbao-transit", "openbao-kv"] = "local"  # noqa: S105
    bao_addr: str | None = Field(default=None, description="https:// URL of OpenBao / Vault")
    bao_role_id: str | None = None
    bao_secret_id: SecretStr | None = None
    bao_transit_mount: str = Field(default="transit", pattern=r"^[A-Za-z0-9_/-]{1,64}$")
    bao_transit_key: str = Field(default="janus-kek", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    bao_kv_mount: str = Field(default="secret", pattern=r"^[A-Za-z0-9_/-]{1,64}$")
    bao_kv_prefix: str = Field(default="janus-ops", pattern=r"^[A-Za-z0-9_/-]{1,128}$")
    bao_ca_file: Path | None = Field(default=None, description="CA bundle for the secret manager")
    bao_timeout_s: int = Field(default=5, ge=1, le=60)
    bao_dek_cache_s: int = Field(
        default=300, ge=0, le=3600, description="How long unwrapped data keys stay in memory"
    )

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
        if self.ldap_start_tls and self.ldap_url.startswith("ldaps://"):
            raise ValueError("JANUS_LDAP_START_TLS is for ldap:// URLs, ldaps:// is already TLS")
        return self

    @model_validator(mode="after")
    def _secret_backend_complete(self) -> "Settings":
        if self.secret_backend == "local":  # noqa: S105
            return self
        missing = [
            name
            for name, value in (
                ("JANUS_BAO_ADDR", self.bao_addr),
                ("JANUS_BAO_ROLE_ID", self.bao_role_id),
                ("JANUS_BAO_SECRET_ID", self.bao_secret_id),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"secret backend {self.secret_backend} but missing: {', '.join(missing)}"
            )
        return self

    @model_validator(mode="after")
    def _manager_complete(self) -> "Settings":
        if self.lldap_http_url is None:
            return self
        if self.ldap_url is None or self.ldap_manager_password is None:
            raise ValueError(
                "JANUS_LLDAP_HTTP_URL needs JANUS_LDAP_URL and JANUS_LDAP_MANAGER_PASSWORD"
            )
        if not self.lldap_http_url.startswith(("http://", "https://")):
            raise ValueError("JANUS_LLDAP_HTTP_URL must start with http:// or https://")
        return self

    @field_validator("secret_key")
    @classmethod
    def _secret_key_format(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value():
            return None  # compose passes an empty value for a key that is not set
        if value is not None:
            raw = value.get_secret_value()
            try:
                ok = len(bytes.fromhex(raw)) == 32
            except ValueError:
                ok = False
            if not ok:
                raise ValueError("must be 64 hexadecimal characters (32 bytes)")
        return value

    @property
    def ansible_roots(self) -> list[Path]:
        return [Path(p.strip()) for p in self.ansible_mount_roots.split(",") if p.strip()]

    @property
    def directory_managed(self) -> bool:
        return self.lldap_http_url is not None

    def user_dn(self, user_id: str) -> str:
        return f"uid={user_id},{self.ldap_users_dn}"

    @property
    def ldap_users_dn(self) -> str:
        return self.ldap_user_base_dn or f"ou=people,{self.ldap_base_dn}"

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("must start with postgresql+asyncpg://")
        return value

    @field_validator("ai_api_key", mode="before")
    @classmethod
    def _empty_key_is_unset(cls, value: object) -> object:
        # JANUS_AI_API_KEY= (empty in the env file) means "no key", not an empty bearer token
        return None if value is None or (isinstance(value, str) and not value.strip()) else value

    @field_validator("ai_base_url")
    @classmethod
    def _ai_url_https(cls, value: str | None) -> str | None:
        if not value or not value.strip():
            return None  # empty = Copilot disabled
        if not value.startswith("https://"):
            raise ValueError("must start with https://")
        return value.rstrip("/")

    @field_validator("agent_server_url")
    @classmethod
    def _agent_url_https(cls, value: str | None) -> str | None:
        if not value or not value.strip():
            return None  # empty = derive from the request
        if not value.startswith("https://"):
            raise ValueError("must start with https://")
        return value.rstrip("/")

    @field_validator(
        "static_dir",
        "recordings_dir",
        "ldap_ca_file",
        "agent_dir",
        "agent_ca_file",
        "bao_ca_file",
        mode="before",
    )
    @classmethod
    def _empty_path_is_unset(cls, value: object) -> object:
        # JANUS_RECORDINGS_DIR= (empty) must mean "off", not Path("") == current directory
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("static_dir")
    @classmethod
    def _static_dir_exists(cls, value: Path | None) -> Path | None:
        if value is not None and not (value / "index.html").is_file():
            raise ValueError(f"{value} does not contain index.html")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment (pydantic plugin knows)
