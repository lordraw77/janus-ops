"""Compose helpers of the containers router: validation that guards what reaches `sh -c`."""

import pytest
from fastapi import HTTPException

from janus_ops.containers import router


def test_compose_script_quotes_paths_and_project() -> None:
    script = router._compose_script(["/srv/a b/compose.yaml", "/srv/a b/o.yaml"], "up -d", "shop")
    assert (
        script == "docker compose -p 'shop' -f '/srv/a b/compose.yaml' -f '/srv/a b/o.yaml' up -d"
    )


def test_paths_with_quotes_or_shell_characters_are_refused() -> None:
    for bad in ("/srv/x'; rm -rf /", "/srv/$(id)", "relative/path", "/srv/a`b`", "/srv/a;b"):
        assert not router.ABS_PATH.match(bad)
    assert router.ABS_PATH.match("/opt/stacks/shop/compose.yaml")


def test_project_names_follow_compose_rules() -> None:
    assert router.PROJECT_NAME.match("shop-1_a")
    for bad in ("Shop", "-x", "a b", "a/b", "", "x" * 49):
        assert not router.PROJECT_NAME.match(bad)


def test_body_limits() -> None:
    assert router._body_text("services: {}") == "services: {}"
    with pytest.raises(HTTPException) as big:
        router._body_text("x" * (router.COMPOSE_MAX + 1))
    assert big.value.status_code == 413
    with pytest.raises(HTTPException) as nul:
        router._body_text("a\x00b")
    assert nul.value.status_code == 422
