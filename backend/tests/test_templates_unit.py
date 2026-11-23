"""Check templates (F8): macros, rendering, validation and the shipped files. No network."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from janus_ops.monitoring.templates import (
    BuiltinFile,
    Item,
    Macro,
    TemplateError,
    TemplateIn,
    builtin_files,
    defaults,
    render,
)

SEED_FILE = (
    Path(__file__).resolve().parents[1] / "src/janus_ops/migrations/versions/0018_snmp_templates.py"
)
VALUES: dict[str, Any] = {"NODE_ADDRESS": "10.0.0.5", "NODE_NAME": "db1", "PG_PORT": 5432}


def _item(**overrides: Any) -> Item:
    raw: dict[str, Any] = {"name": "x", "kind": "tcp", "params": {"port": 22}}
    raw.update(overrides)
    return Item.model_validate(raw)


def _snmp_seed() -> list[dict[str, Any]]:
    spec = importlib.util.spec_from_file_location("seed_0018", SEED_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [item for template in module.SEED for item in template["items"]]


@pytest.mark.parametrize("raw", _snmp_seed(), ids=lambda r: str(r["name"]))
def test_the_seeded_snmp_items_pass_as_items_of_kind_snmp(raw: dict[str, Any]) -> None:
    item = Item.model_validate(
        {
            "name": raw["name"],
            "kind": "snmp",
            "params": {"oid": raw["oid"], "scale": raw["scale"]},
            "indexed": raw["indexed"],
            "comparison": raw["comparison"],
            "warn": raw["warn"],
            "crit": raw["crit"],
            "interval_s": raw["interval_s"],
            "check_cycles": raw["check_cycles"],
        }
    )
    check = render(item, VALUES, instance=3 if item.indexed else None)
    assert check.params["oid"].startswith("1.3.6.1.4.1.6574")
    assert check.name.endswith("#3") == item.indexed


@pytest.mark.parametrize("raw", builtin_files(), ids=lambda r: str(r["slug"]))
def test_every_shipped_template_is_valid_and_has_a_unique_slug(raw: dict[str, Any]) -> None:
    BuiltinFile.model_validate(raw)


def test_shipped_slugs_and_names_are_unique() -> None:
    files = builtin_files()
    assert len({f["slug"] for f in files}) == len(files)
    assert len({f["name"].lower() for f in files}) == len(files)


def test_a_macro_that_is_the_whole_value_keeps_its_type() -> None:
    check = render(_item(params={"port": "{$PG_PORT}"}), VALUES)
    assert check.params["port"] == 5432


def test_macros_inside_a_string_are_replaced_and_system_macros_exist() -> None:
    item = _item(
        name="Port {$PG_PORT} of {$NODE_NAME}",
        kind="http",
        params={"url": "http://{$NODE_ADDRESS}:{$PG_PORT}/"},
    )
    check = render(item, VALUES)
    assert check.name == "Port 5432 of db1"
    assert check.params["url"] == "http://10.0.0.5:5432/"


def test_thresholds_can_be_macros_and_are_checked_after_substitution() -> None:
    item = _item(kind="tcp", comparison="gt", warn="{$W}", crit="{$C}")
    ok = render(item, VALUES | {"W": 50, "C": "90"})
    assert (ok.warn, ok.crit) == (50.0, 90.0)
    with pytest.raises(TemplateError, match="less severe"):
        render(item, VALUES | {"W": 95, "C": 90})
    with pytest.raises(TemplateError, match="not a number"):
        render(item, VALUES | {"W": "abc", "C": 90})


def test_an_undefined_macro_is_an_error() -> None:
    with pytest.raises(TemplateError, match="undefined macro NOPE"):
        render(_item(params={"port": "{$NOPE}"}), VALUES)


def test_params_that_the_kind_rejects_are_an_error() -> None:
    with pytest.raises(TemplateError, match="params port"):
        render(_item(params={"port": "{$PG_PORT}"}), VALUES | {"PG_PORT": 99999})
    with pytest.raises(TemplateError, match="params"):
        render(_item(params={"port": 22, "shell": "rm -rf /"}), VALUES)


@pytest.mark.parametrize(
    ("overrides", "ok"),
    [
        ({"comparison": "gt", "warn": 45, "crit": 55}, True),
        ({"comparison": None, "warn": 1}, False),  # thresholds without a comparison
        ({"comparison": "gt"}, False),  # a comparison without thresholds
        ({"warn": "not-a-macro", "comparison": "gt"}, False),
        ({"kind": "systemd", "params": {}, "comparison": "gt", "warn": 1}, False),  # no thresholds
        ({"indexed": True}, False),  # only SNMP items are indexed
        ({"interval_s": 5}, False),
        ({"unknown_field": 1}, False),
    ],
)
def test_item_shape(overrides: dict[str, Any], ok: bool) -> None:
    if ok:
        _item(**overrides)
    else:
        with pytest.raises(ValidationError):
            _item(**overrides)


def _template(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": "t",
        "category": "generic",
        "macros": [{"name": "PORT", "default": 22}],
        "items": [{"name": "ssh", "kind": "tcp", "params": {"port": "{$PORT}"}}],
    }
    raw.update(overrides)
    return raw


def test_a_template_must_render_with_its_defaults() -> None:
    TemplateIn.model_validate(_template())
    for bad in (
        _template(items=[{"name": "ssh", "kind": "tcp", "params": {"port": "{$MISSING}"}}]),
        _template(macros=[{"name": "PORT", "default": 70000}]),
        _template(macros=[{"name": "PORT", "default": 22}, {"name": "PORT", "default": 23}]),
        _template(macros=[{"name": "NODE_NAME", "default": "x"}]),  # reserved
        _template(macros=[{"name": "lower", "default": 1}]),
        _template(items=[{"name": "a", "kind": "tcp", "params": {"port": 1}}] * 2),  # same name
    ):
        with pytest.raises(ValidationError):
            TemplateIn.model_validate(bad)


def test_a_kind_that_needs_a_capability_requires_it() -> None:
    snmp = {"name": "s", "kind": "snmp", "params": {"oid": "1.3.6.1.2.1.1.5.0"}}
    with pytest.raises(ValidationError, match="requires=snmp"):
        TemplateIn.model_validate(_template(macros=[], items=[snmp]))
    TemplateIn.model_validate(_template(macros=[], requires="snmp", items=[snmp]))


def test_defaults_and_macro_model() -> None:
    macros = [Macro(name="A", default=1), Macro(name="B", default="x")]
    assert defaults(macros) == {"A": 1, "B": "x"}


def test_the_server_software_catalog_ships_a_template_per_product() -> None:
    from janus_ops.monitoring import service_catalog

    shipped = {str(f["slug"]): f for f in builtin_files()}
    products = service_catalog.PRODUCTS
    assert len(products) >= 120
    assert len({p.slug for p in products}) == len(products)
    assert len({p.name for p in products}) == len(products)
    for product in products:
        raw = shipped[product.slug]
        assert raw["requires"] == "agent"
        # a product is found by something that is its own: a unit, a process or a port
        detect = raw["detect"]
        assert detect["units"] or detect["processes"] or detect["ports"], product.slug
    # PostgreSQL and Apache keep their own files, with their probes
    assert "postgresql" in shipped
    assert "apache-httpd" in shipped
    assert "postgresql" not in {p.slug for p in products}


def test_every_product_is_a_service_kind_and_its_template_probes_exist() -> None:
    from janus_ops.inventory.services import CAPABILITY_KINDS, SERVICE_TYPES, ServiceIn
    from janus_ops.monitoring import probes, service_catalog

    for product in service_catalog.PRODUCTS:
        stype = SERVICE_TYPES[product.slug]
        assert not stype.credentials
        # the config defaults are valid and the form draws exactly the config's fields
        config = stype.config().model_dump(mode="json")
        assert {f["name"] for f in stype.fields} == set(config)
        ServiceIn.model_validate({"kind": product.slug, "config": config})
        for item in service_catalog.template(product)["items"]:
            if item["kind"] == "probe":
                assert item["params"]["probe"] in probes.CATALOG
    with pytest.raises(ValidationError):
        ServiceIn.model_validate({"kind": "no-such-product"})
    assert "docker" in CAPABILITY_KINDS and "docker" in SERVICE_TYPES


def test_the_service_probes_extract_numbers() -> None:
    from janus_ops.monitoring import probes

    tcp = probes.CATALOG["service_tcp.latency_ms"].extract({"tcp_ms": 12.5, "port": 6379}, {})
    assert tcp[0] == 12.5 and "6379" in tcp[1]
    http = probes.CATALOG["service_http.status"].extract({"http_status": 204, "http_ms": 3.0}, {})
    assert http[0] == 204
