"""SNMP check templates (FR-82): item validation, and the built-in seed passes it. No network."""

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from janus_ops.monitoring.snmp_templates import Item

SEED_FILE = (
    Path(__file__).resolve().parents[1] / "src/janus_ops/migrations/versions/0018_snmp_templates.py"
)


def _seed() -> list[dict[str, object]]:
    spec = importlib.util.spec_from_file_location("seed_0018", SEED_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [item for template in module.SEED for item in template["items"]]


@pytest.mark.parametrize("raw", _seed(), ids=lambda r: str(r["name"]))
def test_every_built_in_item_passes_the_same_validation_as_a_user_item(
    raw: dict[str, object],
) -> None:
    item = Item.model_validate(raw)
    assert item.normal_oid().startswith("1.3.6.1.4.1.6574")


@pytest.mark.parametrize(
    ("overrides", "ok"),
    [
        ({"comparison": "gt", "warn": 45, "crit": 55}, True),
        ({"comparison": "lt", "warn": 20, "crit": 10}, True),
        ({"comparison": "gt", "warn": 55, "crit": 45}, False),  # warn more severe than crit
        ({"comparison": "lt", "warn": 10, "crit": 20}, False),
        ({"comparison": None, "warn": 1}, False),  # thresholds without a comparison
        ({"comparison": "gt"}, False),  # a comparison without thresholds
        ({"comparison": None}, True),
        ({"oid": "SNMPv2-MIB::sysName.0"}, False),
        ({"oid": ".1.3.6.1.2.1.1.5.0"}, True),
        ({"scale": 0}, False),
        ({"interval_s": 5}, False),
    ],
)
def test_item_thresholds_and_oids(overrides: dict[str, object], ok: bool) -> None:
    raw: dict[str, object] = {"name": "x", "oid": "1.3.6.1.2.1.1.5.0", "comparison": None}
    raw.update(overrides)
    if ok:
        Item.model_validate(raw)
    else:
        with pytest.raises(ValidationError):
            Item.model_validate(raw)


def test_a_leading_dot_is_removed_from_the_stored_oid() -> None:
    assert (
        Item.model_validate({"name": "x", "oid": ".1.3.6.1.2.1.1.5.0"}).normal_oid()
        == "1.3.6.1.2.1.1.5.0"
    )
