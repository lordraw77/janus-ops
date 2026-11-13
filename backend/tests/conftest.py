from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from janus_ops.config import Settings
from janus_ops.main import create_app

# Port 1 on loopback: connection refused immediately, no real database needed
UNREACHABLE_DB = "postgresql+asyncpg://janus:secret-pw@127.0.0.1:1/janus"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"database_url": UNREACHABLE_DB, "db_connect_timeout_s": 1.0}
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture
def ui_dir(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<html>janus-ui-index</html>")
    (tmp_path / "404.html").write_text("<html>janus-ui-404</html>")
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory" / "index.html").write_text("<html>janus-ui-inventory</html>")
    return tmp_path


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(make_settings())) as c:
        yield c


@pytest.fixture
def ui_client(ui_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(make_settings(static_dir=ui_dir))) as c:
        yield c
