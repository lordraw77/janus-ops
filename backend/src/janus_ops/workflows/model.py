"""Workflow definitions: triggers, steps and what each action needs (FR-80).

A definition is JSON: a trigger and an ordered list of steps. Each step has an id, an action
(exec, notify or approval) and its parameters. The rules that matter for safety live here:
- an `exec` step runs a command on a node through its agent, without a shell. It is destructive
  unless `read_only` is true, and a destructive step waits for an approval before it runs;
- an `approval` step always waits for a person;
- a `notify` step records its message in the run and the audit (channel delivery comes later).
"""

import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STEP_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_STEPS = 20
Action = Literal["exec", "notify", "approval"]


class ExecParams(BaseModel):
    # an unknown parameter (for example a shell switch) is an error, not ignored
    model_config = ConfigDict(extra="forbid")

    node_id: uuid.UUID
    command: str = Field(min_length=1, max_length=1000)
    read_only: bool = False
    timeout_s: int = Field(default=60, ge=1, le=300)


class NotifyParams(BaseModel):
    # an unknown parameter (for example a shell switch) is an error, not ignored
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=500)


class ApprovalParams(BaseModel):
    # an unknown parameter (for example a shell switch) is an error, not ignored
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


PARAMS: dict[str, type[BaseModel]] = {
    "exec": ExecParams,
    "notify": NotifyParams,
    "approval": ApprovalParams,
}


class StepIn(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    action: Action
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not STEP_ID.fullmatch(value):
            raise ValueError("invalid step id")
        return value

    @model_validator(mode="after")
    def _params(self) -> "StepIn":
        try:
            model = PARAMS[self.action].model_validate(self.params)
        except ValueError as exc:
            raise ValueError(f"invalid params for {self.action}") from exc
        self.params = model.model_dump(mode="json")
        return self


class TriggerIn(BaseModel):
    type: Literal["manual", "interval", "alert"]
    interval_s: int | None = Field(default=None, ge=60, le=86400)
    check_id: uuid.UUID | None = None  # alert: only this check; empty = any check

    @model_validator(mode="after")
    def _consistent(self) -> "TriggerIn":
        if self.type == "interval" and self.interval_s is None:
            raise ValueError("an interval trigger needs interval_s")
        if self.type != "interval" and self.interval_s is not None:
            raise ValueError("interval_s is only for interval triggers")
        if self.type != "alert" and self.check_id is not None:
            raise ValueError("check_id is only for alert triggers")
        return self


class DefinitionIn(BaseModel):
    trigger: TriggerIn
    steps: list[StepIn] = Field(min_length=1, max_length=MAX_STEPS)

    @model_validator(mode="after")
    def _unique_ids(self) -> "DefinitionIn":
        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("step ids must be unique")
        return self

    def normal(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger.model_dump(mode="json"),
            "steps": [s.model_dump(mode="json") for s in self.steps],
        }


def needs_approval(step: dict[str, Any]) -> bool:
    """A step waits for a person before it runs: approval steps always, destructive exec steps."""
    if step["action"] == "approval":
        return True
    return step["action"] == "exec" and not step["params"].get("read_only", False)


def exec_nodes(definition: dict[str, Any]) -> set[str]:
    """The nodes a definition runs commands on (checked against the caller's scope)."""
    return {str(s["params"]["node_id"]) for s in definition["steps"] if s["action"] == "exec"}
