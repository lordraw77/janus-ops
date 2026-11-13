"""Database engine and schema helpers."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from janus_ops.config import Settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"timeout": settings.db_connect_timeout_s},
    )


def alembic_config(database_url: str | None = None) -> Config:
    """Programmatic Alembic config: no alembic.ini needed inside the image.

    The URL travels in `attributes`, not in the ini options, so passwords with '%' are safe.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if database_url is not None:
        cfg.attributes["database_url"] = database_url
    return cfg


def schema_head() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("no Alembic migrations found")
    return head
