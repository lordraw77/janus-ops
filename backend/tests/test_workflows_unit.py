"""Workflow definitions (FR-80): what a definition may contain, and when a step waits for a
person. No database and no agent."""

import uuid

import pytest
from pydantic import ValidationError

from janus_ops.workflows.model import DefinitionIn, exec_nodes, needs_approval

NODE = str(uuid.UUID("00000000-0000-0000-0000-00000000000a"))


def _definition(
    steps: list[dict[str, object]], trigger: dict[str, object] | None = None
) -> dict[str, object]:
    return {"trigger": trigger or {"type": "manual"}, "steps": steps}


def test_a_notify_step_needs_no_approval() -> None:
    d = DefinitionIn.model_validate(
        _definition(
            [{"id": "a", "action": "notify", "params": {"channel_id": NODE, "message": "hi"}}]
        )
    )
    assert [needs_approval(s) for s in d.normal()["steps"]] == [False]


def test_a_destructive_exec_waits_for_approval_and_a_read_only_one_does_not() -> None:
    steps = [
        {"id": "clean", "action": "exec", "params": {"node_id": NODE, "command": "rm -rf /tmp/x"}},
        {
            "id": "look",
            "action": "exec",
            "params": {"node_id": NODE, "command": "uptime", "read_only": True},
        },
    ]
    out = DefinitionIn.model_validate(_definition(steps)).normal()["steps"]
    assert [needs_approval(s) for s in out] == [True, False]


def test_a_notify_step_without_a_channel_is_refused() -> None:
    with pytest.raises(ValidationError):
        DefinitionIn.model_validate(
            _definition([{"id": "a", "action": "notify", "params": {"message": "x"}}])
        )


def test_an_approval_step_always_waits() -> None:
    d = DefinitionIn.model_validate(
        _definition([{"id": "ok", "action": "approval", "params": {"reason": "go?"}}])
    )
    assert needs_approval(d.normal()["steps"][0]) is True


def test_exec_nodes_are_listed_for_the_scope_check() -> None:
    steps = [
        {
            "id": "a",
            "action": "exec",
            "params": {"node_id": NODE, "command": "uptime", "read_only": True},
        },
        {"id": "b", "action": "notify", "params": {"channel_id": NODE, "message": "x"}},
    ]
    assert exec_nodes(DefinitionIn.model_validate(_definition(steps)).normal()) == {NODE}


@pytest.mark.parametrize(
    "definition",
    [
        _definition([]),  # no steps
        _definition(
            [{"id": "a", "action": "notify", "params": {"channel_id": NODE, "message": "x"}}] * 2
        ),  # same id
        _definition([{"id": "A!", "action": "notify", "params": {"message": "x"}}]),  # bad id
        _definition([{"id": "a", "action": "exec", "params": {"command": "uptime"}}]),  # no node
        _definition(
            [
                {
                    "id": "a",
                    "action": "exec",
                    "params": {"node_id": NODE, "command": "x", "shell": True},
                }
            ]
        ),
        _definition(
            [{"id": "a", "action": "notify", "params": {"channel_id": NODE, "message": "x"}}],
            {"type": "cron"},
        ),
        _definition(
            [{"id": "a", "action": "notify", "params": {"channel_id": NODE, "message": "x"}}],
            {"type": "interval"},
        ),
        _definition(
            [{"id": "a", "action": "notify", "params": {"channel_id": NODE, "message": "x"}}],
            {"type": "manual", "interval_s": 60},
        ),
    ],
)
def test_invalid_definitions_are_refused(definition: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DefinitionIn.model_validate(definition)


def test_an_interval_trigger_needs_a_period_of_at_least_a_minute() -> None:
    ok = _definition(
        [{"id": "a", "action": "notify", "params": {"channel_id": NODE, "message": "x"}}],
        {"type": "interval", "interval_s": 60},
    )
    DefinitionIn.model_validate(ok)
    bad = _definition(
        [{"id": "a", "action": "notify", "params": {"channel_id": NODE, "message": "x"}}],
        {"type": "interval", "interval_s": 30},
    )
    with pytest.raises(ValidationError):
        DefinitionIn.model_validate(bad)
