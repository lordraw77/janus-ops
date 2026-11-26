"""Workflow definitions (FR-80): what a definition may contain, and when a step waits for a
person. No database and no agent."""

import uuid

import pytest
from pydantic import ValidationError

from janus_ops.workflows.model import (
    DefinitionIn,
    condition_met,
    describe,
    exec_nodes,
    needs_approval,
    render,
    target_nodes,
)

NODE = str(uuid.UUID("00000000-0000-0000-0000-00000000000a"))
OTHER = str(uuid.UUID("00000000-0000-0000-0000-00000000000b"))


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


def test_the_position_drawn_by_the_visual_editor_is_kept_and_bounded() -> None:
    step = {"id": "a", "action": "approval", "params": {}, "ui": {"x": 12.5, "y": -40}}
    kept = DefinitionIn.model_validate(_wf(step)).normal()
    assert kept["steps"][0]["ui"] == {"x": 12.5, "y": -40}
    plain = DefinitionIn.model_validate(_wf(_approval("a"))).normal()
    assert "ui" not in plain["steps"][0]  # a step drawn by hand keeps no empty extras
    for bad in ({"x": 1}, {"x": 1, "y": 2, "z": 3}, {"x": 10**7, "y": 0}, {"x": "left", "y": 0}):
        with pytest.raises(ValidationError):
            DefinitionIn.model_validate(_wf({**step, "ui": bad}))


# ---------------------------------------------------------------- management actions


def _one(action: str, params: dict[str, object], **extra: object) -> dict[str, object]:
    step = DefinitionIn.model_validate(
        _definition([{"id": "a", "action": action, "params": params, **extra}])
    )
    return step.normal()["steps"][0]  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("action", "params", "waits"),
    [
        ("service", {"node_id": NODE, "name": "nginx", "op": "restart"}, True),
        ("service", {"node_id": NODE, "name": "nginx", "op": "status"}, False),
        ("process", {"node_id": NODE, "name": "java", "op": "kill"}, True),
        ("process", {"node_id": NODE, "name": "java", "op": "check"}, False),
        ("container", {"node_id": NODE, "name": "web", "op": "stop"}, True),
        ("container", {"node_id": NODE, "name": "web", "op": "status"}, False),
        ("proxmox", {"cluster": NODE, "pve_node": "pve1", "vmid": 101, "op": "reboot"}, True),
        ("proxmox", {"cluster": NODE, "pve_node": "pve1", "vmid": 101, "op": "status"}, False),
        ("http", {"node_id": NODE, "port": 8080}, False),
        ("port", {"node_id": NODE, "port": 22}, False),
        ("wait", {"seconds": 5}, False),
    ],
)
def test_only_the_steps_that_change_something_wait_for_approval(
    action: str, params: dict[str, object], waits: bool
) -> None:
    assert needs_approval(_one(action, params)) is waits


def test_an_unattended_step_runs_without_a_person_but_an_approval_still_waits() -> None:
    step = _one("service", {"node_id": NODE, "name": "nginx", "op": "restart"}, unattended=True)
    assert step["unattended"] is True
    assert needs_approval(step) is False
    approval = _one("approval", {"reason": "go?"}, unattended=True)
    assert needs_approval(approval) is True


def test_the_defaults_are_not_stored() -> None:
    step = _one("wait", {"seconds": 5})
    assert "retries" not in step
    assert "unattended" not in step
    kept = _one("wait", {"seconds": 5}, retries=2, retry_delay_s=30)
    assert (kept["retries"], kept["retry_delay_s"]) == (2, 30)


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("service", {"node_id": NODE, "name": "nginx; reboot"}),
        ("service", {"node_id": NODE, "name": "a b"}),
        ("process", {"node_id": NODE, "name": "java -9"}),
        ("container", {"node_id": NODE, "name": "../etc"}),
        ("service", {"node_id": NODE, "name": "nginx", "shell": "true"}),
        ("http", {"node_id": NODE, "path": "no-slash"}),
        ("http", {"node_id": NODE, "path": "/a b"}),
        ("port", {"node_id": NODE, "port": 70000}),
        ("wait", {"seconds": 601}),
        ("proxmox", {"cluster": NODE, "pve_node": "pve1", "vmid": 101, "op": "snapshot"}),
        ("proxmox", {"cluster": NODE, "pve_node": "pve1;x", "vmid": 101}),
        ("proxmox", {"cluster": NODE, "pve_node": "pve1", "vmid": 5}),
    ],
)
def test_a_wrong_management_step_is_refused(action: str, params: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _one(action, params)


def test_target_nodes_include_every_action_that_names_a_node() -> None:
    steps = [
        {"id": "a", "action": "service", "params": {"node_id": NODE, "name": "x"}},
        {
            "id": "b",
            "action": "proxmox",
            "params": {"cluster": OTHER, "pve_node": "p", "vmid": 100},
        },
        {"id": "c", "action": "wait", "params": {"seconds": 1}},
    ]
    out = DefinitionIn.model_validate(_definition(steps)).normal()
    assert target_nodes(out) == {NODE, OTHER}


def test_a_message_may_quote_an_earlier_step_only() -> None:
    notify = {"channel_id": NODE, "message": "restarted: {{svc.output}} ({{svc.status}})"}
    svc = {"id": "svc", "action": "service", "params": {"node_id": NODE, "name": "x"}}
    DefinitionIn.model_validate(
        _definition([svc, {"id": "n", "action": "notify", "params": notify}])
    )
    with pytest.raises(ValidationError):
        DefinitionIn.model_validate(
            _definition([{"id": "n", "action": "notify", "params": notify}, svc])
        )
    with pytest.raises(ValidationError):
        DefinitionIn.model_validate(
            _definition([{"id": "n", "action": "notify", "params": notify}])
        )


def test_render_fills_output_and_status() -> None:
    results = {"svc": ("done", " active \n"), "bad": ("failed", "boom")}
    assert render("{{ svc.output }}/{{svc.status}}/{{bad.status}}/{{nope.output}}!", results) == (
        "active/ok/failed/!"
    )
    assert len(render("{{bad.output}}", {"bad": ("failed", "x" * 900)})) == 300


def test_describe_says_what_the_step_does() -> None:
    step = _one("service", {"node_id": NODE, "name": "nginx", "op": "restart"})
    assert describe(step) == f"service restart nginx on {NODE}"


def test_every_example_workflow_is_a_valid_definition() -> None:
    import json
    from pathlib import Path

    found = sorted((Path(__file__).resolve().parents[2] / "examples" / "workflows").glob("*.json"))
    assert len(found) >= 4
    for path in found:
        definition = DefinitionIn.model_validate(json.loads(path.read_text()))
        assert definition.steps, path.name
