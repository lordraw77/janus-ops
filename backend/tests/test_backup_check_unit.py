"""Backup alerts (kind `backup`, FR-53 + F3) and the backup audit collection, without PVE or a DB.

The PBS case uses PVE's storage-content shape for a PBS datastore (volid `<storage>:backup/...`,
`verification` set): it checks that the audit reads it like a vzdump file. It is a fixture,
not a real PBS server.
"""

import asyncio
import time
from typing import Any

import pytest
from pydantic import ValidationError

from janus_ops.monitoring import checks
from janus_ops.monitoring.checks import BackupParams, Check
from janus_ops.proxmox.backup_audit import collect
from janus_ops.proxmox.client import PveError

NOW = int(time.time())
HOUR = 3600


def check(params: dict[str, Any] | None = None) -> Check:
    return Check(
        id="c1",
        node_id="n1",
        node_name="pve1",
        address="192.0.2.10",
        kind="backup",
        params=params or {},
        comparison=None,
        warn=None,
        crit=None,
    )


class FakePve:
    """Stands in for ProxmoxAccess.call: answers the paths the audit asks for."""

    def __init__(self, guests: list[dict[str, Any]], archives: list[dict[str, Any]]) -> None:
        self.guests = guests
        self.archives = archives

    async def call(
        self,
        key: str,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        if path == "/cluster/resources":
            return [
                {"type": "node", "node": "pve1", "status": "online"},
                {
                    "type": "storage",
                    "node": "pve1",
                    "storage": "pbs-dc",
                    "status": "available",
                    "content": "backup",
                    "shared": 1,
                },
                *self.guests,
            ]
        if path == "/cluster/backup":
            return [
                {
                    "id": "backup-daily",
                    "enabled": 1,
                    "schedule": "02:00",
                    "all": 1,
                    "storage": "pbs-dc",
                }
            ]
        if path.endswith("/content"):
            return self.archives
        if path.endswith("/tasks"):
            return []
        raise AssertionError(f"unexpected path {path}")


def guest(vmid: int) -> dict[str, Any]:
    return {"type": "qemu", "vmid": vmid, "name": f"vm{vmid}", "node": "pve1", "status": "running"}


def pbs_snapshot(vmid: int, ago_s: int) -> dict[str, Any]:
    return {
        "vmid": vmid,
        "volid": f"pbs-dc:backup/vm/{vmid}/{NOW - ago_s}",
        "ctime": NOW - ago_s,
        "size": 1024,
        "content": "backup",
        "format": "pbs-vm",
        "verification": {"state": "ok"},
    }


def run(access: Any, **params: Any) -> checks.Result:
    return asyncio.run(checks._backup(check(params), access))


def test_params_limit_the_age() -> None:
    assert BackupParams.model_validate({}).max_age_h is None
    with pytest.raises(ValidationError):
        BackupParams.model_validate({"max_age_h": 0})


def test_no_proxmox_access_is_unknown() -> None:
    result = asyncio.run(checks._backup(check(), None))
    assert result.state == "unknown"


def test_pbs_snapshots_are_read_like_backups() -> None:
    access = FakePve(
        guests=[guest(101), guest(102)],
        archives=[pbs_snapshot(101, HOUR), pbs_snapshot(102, 2 * HOUR)],
    )
    audit = asyncio.run(collect(access, "n1"))
    assert audit["storages"] == ["pbs-dc"]
    assert audit["summary"] == {"ok": 2}
    assert {g["storage"] for g in audit["guests"]} == {"pbs-dc"}


def test_all_guests_backed_up_is_ok() -> None:
    access = FakePve([guest(101)], [pbs_snapshot(101, HOUR)])
    result = run(access)
    assert result.state == "ok"
    assert result.perf == {"attention": 0}


def test_missing_backup_is_crit() -> None:
    access = FakePve([guest(101), guest(102)], [pbs_snapshot(101, HOUR)])
    result = run(access)
    assert result.state == "crit"
    assert "1 failed or missing" in result.output
    assert "102 missing" in result.output


def test_stale_backup_is_warn_when_nothing_is_missing() -> None:
    # 2 days old for a daily job: stale (period 1 day + 6 h grace)
    access = FakePve([guest(101)], [pbs_snapshot(101, 2 * 24 * HOUR)])
    result = run(access)
    assert result.state == "warn"
    assert "101 stale" in result.output


def test_max_age_parameter_is_used() -> None:
    access = FakePve([guest(101)], [pbs_snapshot(101, 5 * HOUR)])
    assert run(access).state == "ok"
    assert run(access, max_age_h=2).state == "warn"


def test_proxmox_error_is_unknown() -> None:
    class Down:
        async def call(self, *args: Any, **kwargs: Any) -> Any:
            raise PveError("host_unreachable")

    result = run(Down())
    assert result.state == "unknown"
    assert "host_unreachable" in result.output


def test_ignored_guests_are_left_out_of_the_check() -> None:
    access = FakePve([guest(101), guest(102), guest(103)], [pbs_snapshot(101, HOUR)])
    assert run(access).state == "crit"  # 102 and 103 have no backup
    result = run(access, ignore_vmids=[102, 103])
    assert result.state == "ok"
    assert "2 ignored" in result.output
    partial = run(access, ignore_vmids=[102])  # 103 is still a problem
    assert partial.state == "crit"
    assert "103 missing" in partial.output and "102 missing" not in partial.output


def test_ignore_list_is_validated() -> None:
    assert BackupParams.model_validate({"ignore_vmids": [100, 101]}).ignore_vmids == [100, 101]
    for bad in ([0], ["x"], list(range(1, 202))):
        with pytest.raises(ValidationError):
            BackupParams.model_validate({"ignore_vmids": bad})
