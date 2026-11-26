"""Workflow definitions: triggers, steps and what each action needs (FR-80).

A definition is JSON: a trigger and an ordered list of steps. Each step has an id, an action
(exec, notify, approval, or one of the management actions: service, process, container, http,
port, wait, check, proxmox) and its parameters (and, for the visual editor, the position it is
drawn at). A step may carry a `when` condition on an earlier step (its outcome and, optionally, a
text its output contains): when it is not met the step is
skipped. A step with `on_failure: continue` does not fail the run, so later steps can branch on
its failure. The rules that matter for safety live here:
- an `exec` step runs a command on a node through its agent, without a shell. It is destructive
  unless `read_only` is true, and a destructive step waits for an approval before it runs;
- the management actions build their command themselves from validated fields (a unit name, a
  process name, a container name, a guest id), so nothing a user types is ever parsed by a shell.
  Those that change something (see `changes_things`) wait for an approval too, unless the step is
  marked `unattended`: the author's explicit choice, recorded in the audit with every run;
- any step can be retried (`retries`, `retry_delay_s`) when it fails;
- an `approval` step always waits for a person;
- a `notify` step records its message in the run and the audit (channel delivery comes later).
"""

import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

STEP_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_STEPS = 20
Action = Literal[
    "exec",
    "notify",
    "approval",
    "service",
    "process",
    "container",
    "http",
    "port",
    "wait",
    "check",
    "proxmox",
]
Expect = Literal["running", "stopped"]

UNIT = r"^[A-Za-z0-9@._:-]{1,128}$"  # a systemd unit or a Windows service name
PROCESS = r"^[A-Za-z0-9._+-]{1,64}$"
CONTAINER = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
PVE_NODE = r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$"
SNAPSHOT = r"^[A-Za-z][A-Za-z0-9_-]{1,39}$"
URL_PATH = r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]{0,199}$"
TEMPLATE = re.compile(r"\{\{\s*([a-z][a-z0-9_-]{0,31})\.(output|status)\s*\}\}")


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

    channel_id: uuid.UUID  # a notification channel: its secrets stay in the vault
    message: str = Field(min_length=1, max_length=500)


class ApprovalParams(BaseModel):
    # an unknown parameter (for example a shell switch) is an error, not ignored
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=500)


class ServiceParams(BaseModel):
    """A systemd unit (Linux) or a service (Windows) on a node with an agent."""

    model_config = ConfigDict(extra="forbid")

    node_id: uuid.UUID
    name: str = Field(pattern=UNIT)
    op: Literal["status", "start", "stop", "restart", "reload"] = "status"
    expect: Expect = "running"  # for status: the step succeeds when the service is in this state
    timeout_s: int = Field(default=60, ge=1, le=300)


class ProcessParams(BaseModel):
    """A process by name (Linux: exact name, 15 characters at most as the kernel keeps it)."""

    model_config = ConfigDict(extra="forbid")

    node_id: uuid.UUID
    name: str = Field(pattern=PROCESS)
    op: Literal["check", "kill"] = "check"
    expect: Expect = "running"  # for check
    signal: Literal["TERM", "KILL", "HUP", "INT"] = "TERM"  # for kill (Windows always stops it)
    timeout_s: int = Field(default=30, ge=1, le=300)


class ContainerParams(BaseModel):
    """A Docker container on a host with the Docker capability, reached through its agent."""

    model_config = ConfigDict(extra="forbid")

    node_id: uuid.UUID
    name: str = Field(pattern=CONTAINER)
    op: Literal["status", "start", "stop", "restart"] = "status"
    expect: Expect = "running"  # for status


class HttpParams(BaseModel):
    """An HTTP(S) request to the node's own address, like the HTTP check (no free URL)."""

    model_config = ConfigDict(extra="forbid")

    node_id: uuid.UUID
    scheme: Literal["http", "https"] = "http"
    port: int = Field(default=80, ge=1, le=65535)
    path: str = Field(default="/", pattern=URL_PATH)
    method: Literal["GET", "HEAD"] = "GET"
    expect_status: int = Field(default=200, ge=100, le=599)
    contains: str | None = Field(default=None, min_length=1, max_length=200)
    verify_tls: bool = False
    timeout_s: int = Field(default=10, ge=1, le=60)


class PortParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: uuid.UUID
    port: int = Field(ge=1, le=65535)
    expect: Literal["open", "closed"] = "open"
    timeout_s: int = Field(default=5, ge=1, le=60)


class WaitParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seconds: int = Field(default=10, ge=1, le=600)


class CheckParams(BaseModel):
    """Looks at the current state of a check, optionally waiting until it is the expected one."""

    model_config = ConfigDict(extra="forbid")

    check_id: uuid.UUID
    state: Literal["ok", "warn", "crit", "unknown"] = "ok"
    wait_s: int = Field(default=0, ge=0, le=600)  # 0: look once


class ProxmoxParams(BaseModel):
    """A Proxmox VE guest: `cluster` is any Proxmox host of the cluster (as in the Proxmox page)."""

    model_config = ConfigDict(extra="forbid")

    cluster: uuid.UUID
    pve_node: str = Field(pattern=PVE_NODE)
    kind: Literal["qemu", "lxc"] = "qemu"
    vmid: int = Field(ge=100, le=999999999)
    op: Literal["status", "start", "shutdown", "stop", "reboot", "snapshot"] = "status"
    expect: Expect = "running"  # for status
    snapshot: str | None = Field(default=None, pattern=SNAPSHOT)  # for snapshot
    wait_s: int = Field(default=120, ge=0, le=600)  # wait for the task to finish; 0: do not wait

    @model_validator(mode="after")
    def _snapshot(self) -> "ProxmoxParams":
        if self.op == "snapshot" and not self.snapshot:
            raise ValueError("a snapshot step needs a name")
        if self.op != "snapshot" and self.snapshot is not None:
            raise ValueError("snapshot is only for the snapshot operation")
        return self


PARAMS: dict[str, type[BaseModel]] = {
    "exec": ExecParams,
    "notify": NotifyParams,
    "approval": ApprovalParams,
    "service": ServiceParams,
    "process": ProcessParams,
    "container": ContainerParams,
    "http": HttpParams,
    "port": PortParams,
    "wait": WaitParams,
    "check": CheckParams,
    "proxmox": ProxmoxParams,
}


class WhenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: str  # an earlier step of the same definition
    outcome: Literal["ok", "failed", "skipped"] = "ok"
    contains: str | None = Field(default=None, min_length=1, max_length=200)


class UiPos(BaseModel):
    """Where the visual editor drew the step. Layout only: it never changes what a run does."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=-100000, le=100000)
    y: float = Field(ge=-100000, le=100000)


class StepIn(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    action: Action
    when: WhenIn | None = None
    on_failure: Literal["fail", "continue"] = "fail"
    ui: UiPos | None = None
    # a failed step is tried again `retries` times, `retry_delay_s` apart (not for a wrong setup)
    retries: int = Field(default=0, ge=0, le=5)
    retry_delay_s: int = Field(default=10, ge=1, le=300)
    # run without waiting for a person even though the step changes something (audited)
    unattended: bool = False
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
    type: Literal["manual", "interval", "alert", "webhook"]
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
        for position, step in enumerate(self.steps):
            if step.when is not None and step.when.step not in ids[:position]:
                raise ValueError("a condition must refer to an earlier step")
            if step.action == "notify":
                for ref, _ in TEMPLATE.findall(step.params["message"]):
                    if ref not in ids[:position]:
                        raise ValueError("a {{step.output}} must refer to an earlier step")
        return self

    def normal(self) -> dict[str, Any]:
        return {
            "trigger": self.trigger.model_dump(mode="json"),
            "steps": [_dump(s) for s in self.steps],
        }


def _dump(step: StepIn) -> dict[str, Any]:
    out = step.model_dump(mode="json", exclude_none=True)
    # keep the stored definition as small as before: the defaults are not written
    if not step.retries:
        out.pop("retries", None)
        out.pop("retry_delay_s", None)
    if not step.unattended:
        out.pop("unattended", None)
    return out


MUTATING_OPS = {
    "service": {"start", "stop", "restart", "reload"},
    "process": {"kill"},
    "container": {"start", "stop", "restart"},
    "proxmox": {"start", "shutdown", "stop", "reboot", "snapshot"},
}


def changes_things(step: dict[str, Any]) -> bool:
    """Whether the step changes something on a host (so it is destructive until proven harmless)."""
    action, params = step["action"], step["params"]
    if action == "exec":
        return not params.get("read_only", False)
    return params.get("op") in MUTATING_OPS.get(action, set())


def needs_approval(step: dict[str, Any]) -> bool:
    """A step waits for a person before it runs: approval steps always; the steps that change
    something unless the author marked them `unattended`."""
    if step["action"] == "approval":
        return True
    return changes_things(step) and not step.get("unattended", False)


def describe(step: dict[str, Any]) -> str:
    """One line saying what a step does, for the approval request and the audit."""
    a, p = step["action"], step["params"]
    where = p.get("node_id") or p.get("cluster")
    if a == "exec":
        return f"exec on {where}: {p['command']}"[:500]
    if a == "proxmox":
        what = f"{p['op']} {p['kind']} {p['vmid']} on {p['pve_node']}"
        return f"proxmox {what} (cluster {where})"
    if a in ("service", "process", "container"):
        return f"{a} {p['op']} {p['name']} on {where}"
    return f"{a} on {where}" if where else a


def target_nodes(definition: dict[str, Any]) -> set[str]:
    """The nodes a definition acts on (checked against the caller's scope)."""
    out: set[str] = set()
    for s in definition["steps"]:
        node = s["params"].get("node_id") or s["params"].get("cluster")
        if node:
            out.add(str(node))
    return out


exec_nodes = target_nodes  # the name used before the management actions existed


def render(message: str, results: dict[str, tuple[str, str]], limit: int = 300) -> str:
    """Fills `{{step.output}}` and `{{step.status}}` of a message from the finished steps."""

    def fill(m: re.Match[str]) -> str:
        found = results.get(m.group(1))
        if found is None:
            return ""
        status, output = found
        return output.strip()[:limit] if m.group(2) == "output" else OUTCOME_OF.get(status, status)

    return TEMPLATE.sub(fill, message)


OUTCOME_OF = {"done": "ok", "failed": "failed", "skipped": "skipped"}


def condition_met(when: dict[str, Any] | None, results: dict[str, tuple[str, str]]) -> bool:
    """Whether a step runs: `results` maps a finished step id to (status, output)."""
    if not when:
        return True
    found = results.get(when["step"])
    if found is None:
        return False
    status, output = found
    if OUTCOME_OF.get(status) != when.get("outcome", "ok"):
        return False
    contains = when.get("contains")
    return contains is None or contains in output
