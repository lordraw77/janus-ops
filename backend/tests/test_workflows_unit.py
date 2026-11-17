"""Workflow definitions (FR-80): what a definition may contain, and when a step waits for a
person. No database and no agent."""

import uuid

import pytest
from pydantic import ValidationError

from janus_ops.workflows.model import DefinitionIn, condition_met, exec_nodes, needs_approval

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


def _wf(*steps: dict[str, object]) -> dict[str, object]:
    return {"trigger": {"type": "manual"}, "steps": list(steps)}


def _approval(step_id: str, **extra: object) -> dict[str, object]:
    return {"id": step_id, "action": "approval", "params": {}, **extra}


def test_a_condition_may_only_refer_to_an_earlier_step() -> None:
    DefinitionIn.model_validate(_wf(_approval("a"), _approval("b", when={"step": "a"})))
    for bad in ("b", "later", "missing"):
        with pytest.raises(ValueError, match="earlier step"):
            DefinitionIn.model_validate(_wf(_approval("a", when={"step": bad}), _approval("later")))


def test_condition_fields_are_closed() -> None:
    with pytest.raises(ValueError):
        DefinitionIn.model_validate(
            _wf(_approval("a"), _approval("b", when={"step": "a", "shell": "x"}))
        )
    with pytest.raises(ValueError):
        DefinitionIn.model_validate(
            _wf(_approval("a"), _approval("b", when={"step": "a", "outcome": "maybe"}))
        )


def test_the_normal_form_keeps_conditions_and_drops_empty_ones() -> None:
    d = DefinitionIn.model_validate(
        _wf(_approval("a", on_failure="continue"), _approval("b", when={"step": "a"}))
    ).normal()
    assert d["steps"][0]["on_failure"] == "continue"
    assert "when" not in d["steps"][0]
    assert d["steps"][1]["when"] == {"step": "a", "outcome": "ok"}


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (None, True),
        ({"step": "a"}, True),
        ({"step": "a", "outcome": "failed"}, False),
        ({"step": "b", "outcome": "failed"}, True),
        ({"step": "c", "outcome": "skipped"}, True),
        ({"step": "c"}, False),
        ({"step": "a", "contains": "disk full"}, False),
        ({"step": "a", "contains": "all good"}, True),
        ({"step": "missing"}, False),
    ],
)
def test_condition_met(when: dict[str, object] | None, expected: bool) -> None:
    results = {"a": ("done", "all good"), "b": ("failed", "exit 1"), "c": ("skipped", "")}
    assert condition_met(when, results) is expected


def test_a_webhook_trigger_takes_no_options() -> None:
    DefinitionIn.model_validate({"trigger": {"type": "webhook"}, "steps": [_approval("a")]})
    with pytest.raises(ValueError):
        DefinitionIn.model_validate(
            {"trigger": {"type": "webhook", "interval_s": 60}, "steps": [_approval("a")]}
        )


def test_the_webhook_signature() -> None:
    import hashlib
    import hmac

    from janus_ops.workflows.router import verify_signature

    body = b'{"x": 1}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_signature("s3cret", body, good)
    assert not verify_signature("other", body, good)
    assert not verify_signature("s3cret", b"{}", good)
    assert not verify_signature("s3cret", body, "")
