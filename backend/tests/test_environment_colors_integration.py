"""Environment badge colours: the five presets and any #rrggbb, nothing else; and the migration that
widened the column."""

from collections.abc import Iterator

import pytest
from alembic import command

from janus_ops.db import alembic_config

from ._it import CONFIGURED, DB_URL, run_sql, signed_in

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def _colors(client: object) -> dict[str, str | None]:
    rows = client.get("/api/v1/environments").json()  # type: ignore[attr-defined]
    return {r["name"]: r["color"] for r in rows}


@pytest.mark.parametrize("color", ["ok", "primary", "#ff8800", "#00AAff", None])
def test_a_preset_a_hex_colour_or_none_is_accepted(color: str | None) -> None:
    name = "c" + (color or "none").lstrip("#").lower()
    with signed_in("alice") as admin:
        r = admin.post("/api/v1/environments", json={"name": name, "label": "x", "color": color})
        assert r.status_code == 201, r.text
        assert _colors(admin)[name] == color


@pytest.mark.parametrize(
    "color", ["#fff", "#ggg000", "red", "rgb(1,2,3)", "#1234567", "javascript:1"]
)
def test_anything_else_is_refused(color: str) -> None:
    with signed_in("alice") as admin:
        r = admin.post("/api/v1/environments", json={"name": "bad", "label": "x", "color": color})
    assert r.status_code == 422


def test_a_colour_can_be_changed_to_a_custom_one_and_cleared() -> None:
    with signed_in("alice") as admin:
        admin.post("/api/v1/environments", json={"name": "chg", "label": "x", "color": "warn"})
        assert admin.patch("/api/v1/environments/chg", json={"color": "#123abc"}).status_code == 204
        assert _colors(admin)["chg"] == "#123abc"
        assert admin.patch("/api/v1/environments/chg", json={"color": "#12345"}).status_code == 422


def test_the_database_itself_refuses_a_bad_colour_and_the_downgrade_clears_custom_ones() -> None:
    with pytest.raises(Exception, match="environment_color_check"):
        run_sql("INSERT INTO environment (name, label, color) VALUES ('dbbad', 'x', 'red')")
    run_sql("INSERT INTO environment (name, label, color) VALUES ('dbhex', 'x', '#abcdef')")
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "0028")
    assert run_sql("SELECT color FROM environment WHERE name = 'dbhex'") == [(None,)]
    command.upgrade(cfg, "head")
