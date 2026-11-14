from pathlib import Path

import pytest
from pydantic import ValidationError

from janus_ops import __version__
from janus_ops.cli import main
from janus_ops.config import get_settings
from janus_ops.db import schema_head

from .conftest import UNREACHABLE_DB, make_settings


def test_rejects_non_asyncpg_url() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        make_settings(database_url="postgresql://u:p@db/janus")


def test_rejects_static_dir_without_index(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=r"index\.html"):
        make_settings(static_dir=tmp_path)


@pytest.mark.parametrize("port", [0, 65536])
def test_rejects_invalid_port(port: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(port=port)


def test_database_url_hidden_in_repr() -> None:
    settings = make_settings()
    assert "secret-pw" not in repr(settings)
    assert "secret-pw" not in str(settings.model_dump())


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("JANUS_DATABASE_URL", UNREACHABLE_DB)
    monkeypatch.setenv("JANUS_PORT", "9000")
    try:
        assert get_settings().port == 9000
    finally:
        get_settings.cache_clear()


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_cli_reports_bad_config_without_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("JANUS_DATABASE_URL", "mysql://u:leaked-pw@db/x")
    try:
        assert main(["serve"]) == 2
    finally:
        get_settings.cache_clear()
    err = capsys.readouterr().err
    assert "JANUS_DATABASE_URL" in err
    assert "leaked-pw" not in err


def test_cli_rejects_unknown_command() -> None:
    with pytest.raises(SystemExit):
        main(["frobnicate"])


def test_schema_head_is_latest_migration() -> None:
    assert schema_head() == "0011"


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_recordings_dir_disables_recording(value: str) -> None:
    assert make_settings(recordings_dir=value).recordings_dir is None
