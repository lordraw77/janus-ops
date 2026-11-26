"""The management actions of a workflow step: the command each one builds and how it reads the
answer. The agent, Docker and Proxmox are replaced by fakes; nothing runs and no network is used."""

import asyncio
import functools
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from janus_ops.agents import exec as node_exec
from janus_ops.workflows import actions


def sync(test: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., None]:
    """These tests are coroutines: run each one in its own loop (no async plugin is installed)."""

    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


NODE = str(uuid.UUID("00000000-0000-0000-0000-00000000000a"))


class Agent:
    """Records the argument vector and answers with a canned result."""

    def __init__(self, exit_code: int | None, output: str = "", windows: bool = False) -> None:
        self.result = node_exec.ExecResult(exit_code, f"exit:{exit_code}", output, False)
        self.windows = windows
        self.argv: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def is_windows(_e: Any, _n: str) -> bool:
            return self.windows

        async def agent(_e: Any, _h: Any, _n: str, argv: list[str], _t: int) -> Any:
            self.argv = argv
            return self.result

        monkeypatch.setattr(actions, "_is_windows", is_windows)
        monkeypatch.setattr(actions, "_agent", agent)


def _service(op: str, **extra: Any) -> dict[str, Any]:
    return {
        "node_id": NODE,
        "name": "nginx",
        "op": op,
        "expect": "running",
        "timeout_s": 30,
        **extra,
    }


def _process(op: str, **extra: Any) -> dict[str, Any]:
    return {
        "node_id": NODE, "name": "java", "op": op, "expect": "running", "signal": "TERM",
        "timeout_s": 30, **extra,
    }  # fmt: skip


@sync
async def test_a_service_restart_is_one_systemctl_call(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent(0)
    agent.install(monkeypatch)
    out = await actions.service(None, None, _service("restart"))  # type: ignore[arg-type]
    assert agent.argv == ["systemctl", "restart", "nginx"]
    assert out["ok"] is True


@sync
async def test_a_failed_systemctl_is_a_failed_step(monkeypatch: pytest.MonkeyPatch) -> None:
    Agent(5, "Failed to restart").install(monkeypatch)
    out = await actions.service(None, None, _service("restart"))  # type: ignore[arg-type]
    assert (out["ok"], out["error"], out["exit_code"]) == (False, "exit 5", 5)


@pytest.mark.parametrize(
    ("exit_code", "expect", "ok"),
    [(0, "running", True), (3, "running", False), (3, "stopped", True), (0, "stopped", False)],
)
@sync
async def test_a_service_status_is_read_against_what_is_expected(
    monkeypatch: pytest.MonkeyPatch, exit_code: int, expect: str, ok: bool
) -> None:
    agent = Agent(exit_code, "active" if exit_code == 0 else "inactive")
    agent.install(monkeypatch)
    out = await actions.service(None, None, _service("status", expect=expect))  # type: ignore[arg-type]
    assert agent.argv == ["systemctl", "is-active", "nginx"]
    assert out["ok"] is ok


@sync
async def test_an_unknown_unit_is_an_error_not_a_stopped_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Agent(4, "").install(monkeypatch)
    out = await actions.service(None, None, _service("status", expect="stopped"))  # type: ignore[arg-type]
    assert out["ok"] is False and out["error"] == "exit 4"


@sync
async def test_windows_uses_powershell_with_the_name_quoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(0, windows=True)
    agent.install(monkeypatch)
    await actions.service(None, None, _service("restart", name="W3SVC"))  # type: ignore[arg-type]
    assert agent.argv[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert "Restart-Service -Name 'W3SVC'" in agent.argv[4]
    out = await actions.service(None, None, _service("reload", name="W3SVC"))  # type: ignore[arg-type]
    assert out["error"] == "unsupported"


@sync
async def test_a_process_check_counts_and_compares(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent(0, "3\n")
    agent.install(monkeypatch)
    out = await actions.process(None, None, _process("check"))  # type: ignore[arg-type]
    assert agent.argv == ["pgrep", "-c", "-x", "java"]
    assert out["ok"] is True and "3 process" in out["output"]
    Agent(1, "0\n").install(monkeypatch)
    gone = await actions.process(None, None, _process("check", expect="stopped"))  # type: ignore[arg-type]
    assert gone["ok"] is True
    missing = await actions.process(None, None, _process("check"))  # type: ignore[arg-type]
    assert (missing["ok"], missing["error"]) == (False, "expected_running")


@sync
async def test_a_kill_sends_the_chosen_signal_to_the_exact_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(0)
    agent.install(monkeypatch)
    await actions.process(None, None, _process("kill", signal="HUP"))  # type: ignore[arg-type]
    assert agent.argv == ["pkill", "-HUP", "-x", "java"]
    Agent(1).install(monkeypatch)
    nothing = await actions.process(None, None, _process("kill"))  # type: ignore[arg-type]
    assert nothing["error"] == "no_such_process"


@sync
async def test_a_refusal_of_the_agent_comes_back_as_the_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def is_windows(_e: Any, _n: str) -> bool:
        return False

    async def agent(*_a: Any) -> Any:
        return actions.fail("exec_not_allowed")

    monkeypatch.setattr(actions, "_is_windows", is_windows)
    monkeypatch.setattr(actions, "_agent", agent)
    out = await actions.service(None, None, _service("restart"))  # type: ignore[arg-type]
    assert out["error"] == "exec_not_allowed"
    assert "exec_not_allowed" in actions.NO_RETRY


@sync
async def test_a_wait_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(actions.asyncio, "sleep", sleep)
    out = await actions.wait({"seconds": 7})
    assert slept == [7] and out["ok"] is True


class Pve:
    def __init__(self, status: str = "running", task: list[dict[str, Any]] | None = None) -> None:
        self.status, self.task, self.calls = status, task or [], []

    async def call(
        self, key: str, method: str, path: str, params: Any = None, data: Any = None
    ) -> Any:
        self.calls.append((method, path, data))
        if path.endswith("/status/current"):
            return {"status": self.status}
        if "/tasks/" in path:
            return self.task.pop(0)
        return "UPID:pve1:0001"


def _guest(op: str, **extra: Any) -> dict[str, Any]:
    return {
        "cluster": NODE, "pve_node": "pve1", "kind": "qemu", "vmid": 101, "op": op,
        "expect": "running", "snapshot": None, "wait_s": 10, **extra,
    }  # fmt: skip


@sync
async def test_a_guest_reboot_waits_for_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    pve = Pve(task=[{"status": "running"}, {"status": "stopped", "exitstatus": "OK"}])

    async def sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(actions.asyncio, "sleep", sleep)
    monkeypatch.setattr(actions, "SERVICES", actions.Services(proxmox=pve))  # type: ignore[arg-type]
    out = await actions.proxmox(_guest("reboot"))
    assert pve.calls[0] == ("POST", "/nodes/pve1/qemu/101/status/reboot", None)
    assert out["ok"] is True


@sync
async def test_a_failed_task_fails_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    pve = Pve(task=[{"status": "stopped", "exitstatus": "VM is locked"}])
    monkeypatch.setattr(actions, "SERVICES", actions.Services(proxmox=pve))  # type: ignore[arg-type]
    out = await actions.proxmox(_guest("stop"))
    assert (out["ok"], out["error"]) == (False, "task_failed")


@sync
async def test_a_snapshot_sends_its_name_and_status_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pve = Pve(status="stopped")
    monkeypatch.setattr(actions, "SERVICES", actions.Services(proxmox=pve))  # type: ignore[arg-type]
    await actions.proxmox(_guest("snapshot", snapshot="before-upgrade", wait_s=0))
    assert pve.calls[0] == ("POST", "/nodes/pve1/qemu/101/snapshot", {"snapname": "before-upgrade"})
    down = await actions.proxmox(_guest("status", expect="stopped"))
    assert down["ok"] is True


@sync
async def test_actions_without_their_service_fail_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions, "SERVICES", actions.Services())
    box = {"node_id": NODE, "name": "web", "op": "restart", "expect": "running"}
    assert (await actions.container(box, "me"))["error"] == "docker_unavailable"
    assert (await actions.proxmox(_guest("status")))["error"] == "proxmox_unavailable"


@sync
async def test_an_unknown_action_is_not_swallowed() -> None:
    with pytest.raises(KeyError):
        await actions.run(None, None, {"action": "nope", "params": {}}, "me")  # type: ignore[arg-type]
