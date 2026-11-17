"""The systemd unit written by the bootstrap: sandbox, shell (FR-83), update rollback (FR-84)."""

import subprocess
from pathlib import Path

from janus_ops.agents import bootstrap


def _plan(**kw: object) -> bootstrap.Plan:
    return bootstrap.Plan(
        node_id="00000000-0000-0000-0000-000000000001",
        server_url="https://s",
        token="t",
        interval_s=10,
        agent_dir=Path("/x"),
        **kw,  # type: ignore[arg-type]
    )


def _guard_script(state: Path) -> str:
    """The guard's shell script as systemd runs it (`$$` is a literal `$`), on a scratch state
    directory and a scratch binary."""
    line = bootstrap.ROLLBACK_GUARD
    script = line.split("-c '", 1)[1].rsplit("'", 1)[0].replace("$$", "$")
    return script.replace("/var/lib/janus-agent", str(state)).replace(
        "/usr/local/bin/janus-agent", str(state / "agent")
    )


def test_the_default_unit_is_sandboxed_but_may_replace_the_binary() -> None:
    unit = bootstrap.render_unit(_plan())
    assert "ProtectSystem=strict" in unit and "NoNewPrivileges=true" in unit
    assert "ReadWritePaths=/etc/janus-agent /usr/local/bin" in unit  # self-update (FR-84)


def test_a_unit_with_a_shell_is_not_sandboxed() -> None:
    unit = bootstrap.render_unit(_plan(allow_pty=True))
    for directive in ("ProtectSystem", "NoNewPrivileges", "PrivateTmp", "RestrictSUIDSGID"):
        assert directive not in unit
    assert "ExecStart=/usr/local/bin/janus-agent run" in unit and "[Install]" in unit


def test_the_guard_restores_the_old_binary_after_four_starts(tmp_path: Path) -> None:
    script = _guard_script(tmp_path)
    (tmp_path / "agent").write_text("new")
    (tmp_path / "agent.old").write_text("old")
    marker = tmp_path / "update-pending.json"
    marker.write_text("{}")
    for _ in range(4):  # a binary that never confirms: still the new one for four starts
        subprocess.run(["/bin/sh", "-c", script], check=True)  # noqa: S603
        assert (tmp_path / "agent").read_text() == "new"
    subprocess.run(["/bin/sh", "-c", script], check=True)  # noqa: S603
    assert (tmp_path / "agent").read_text() == "old"
    assert not marker.exists()
    assert not (tmp_path / "update-starts").exists()


def test_the_guard_leaves_a_confirmed_update_alone(tmp_path: Path) -> None:
    (tmp_path / "agent").write_text("new")
    (tmp_path / "update-starts").write_text("3")  # left over; no marker means it was confirmed
    subprocess.run(["/bin/sh", "-c", _guard_script(tmp_path)], check=True)  # noqa: S603
    assert (tmp_path / "agent").read_text() == "new"
    assert not (tmp_path / "update-starts").exists()
