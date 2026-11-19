"""Probes (F8, phase 3): the catalog, what each probe makes of a snapshot, how a probe is graded,
how errors read, and the snapshot cache. The connectors are replaced by canned snapshots: no
network, no database."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from janus_ops.monitoring import probes
from janus_ops.monitoring.checks import Check, ProbeParams, evaluate_probe
from janus_ops.monitoring.probes import httpd, postgres
from janus_ops.monitoring.probes import proxmox as pve
from janus_ops.monitoring.probes.base import ERRORS, ProbeError, SnapshotCache
from janus_ops.proxmox.client import PveError

CTX = probes.Ctx(db=None, vault=None, proxmox=None)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def fresh_caches() -> Iterator[None]:
    for module in (postgres, httpd, pve):
        module.CACHE._items.clear()
    yield


def _check(
    probe: str,
    comparison: str | None = None,
    warn: float | None = None,
    crit: float | None = None,
    **args: Any,
) -> Check:
    return Check(
        id="c1",
        node_id="n1",
        node_name="node",
        address="10.0.0.5",
        kind="probe",
        params=ProbeParams(probe=probe, args=args).model_dump(exclude_none=True),
        comparison=comparison,
        warn=warn,
        crit=crit,
    )


def _serve(
    monkeypatch: pytest.MonkeyPatch, connector: str, snap: dict[str, Any] | ProbeError
) -> None:
    async def fake(ctx: Any, target: Any) -> dict[str, Any]:
        if isinstance(snap, ProbeError):
            raise snap
        return snap

    monkeypatch.setitem(probes.CONNECTORS, connector, fake)


# ---------------------------------------------------------------- catalog


def test_the_catalog_is_consistent() -> None:
    assert len(probes.CATALOG) == len({p.name for p in probes.CATALOG.values()})
    for name, probe in probes.CATALOG.items():
        assert name == f"{probe.connector}.{name.split('.', 1)[1]}"
        assert probe.connector in probes.CONNECTORS
        assert probe.description and probe.requires in (None, "postgres", "proxmox")
        assert all(isinstance(required, bool) for required in probe.args.values())


@pytest.mark.parametrize(
    ("params", "ok"),
    [
        ({"probe": "postgres.connections"}, True),
        ({"probe": "postgres.nope"}, False),
        ({"probe": "postgres.connections", "args": {"database": "x"}}, False),  # no such argument
        ({"probe": "httpd.busy_workers"}, False),  # needs url
        ({"probe": "httpd.busy_workers", "args": {"url": "http://h/server-status?auto"}}, True),
        ({"probe": "httpd.busy_workers", "args": {"url": "file:///etc/passwd"}}, False),
        ({"probe": "httpd.busy_workers", "args": {"url": "ftp://h/x"}}, False),
        ({"probe": "proxmox.storage_max_pct", "args": {"storage": "local"}}, True),
        ({"probe": "postgres.connections", "timeout_s": 0}, False),
        ({"probe": "postgres.connections", "sql": "select 1"}, False),  # no free text
    ],
)
def test_probe_params(params: dict[str, Any], ok: bool) -> None:
    if ok:
        ProbeParams.model_validate(params)
    else:
        with pytest.raises(ValidationError):
            ProbeParams.model_validate(params)


# ---------------------------------------------------------------- PostgreSQL


PG: dict[str, Any] = {
    "max_connections": 100.0,
    "connections": 45.0,
    "active": 3.0,
    "idle_in_tx": 2.0,
    "longest_tx_s": 12.5,
    "lock_waits": 0.0,
    "cache_hit_pct": 99.2,
    "db_size_bytes": 1048576.0,
    "in_recovery": False,
    "replication_lag_s": 0.0,
    "max_xid_age": 214748364.7,
}


@pytest.mark.parametrize(
    ("probe", "snap", "value", "words"),
    [
        ("postgres.connections_used_pct", PG, 45.0, "45 of 100 connections"),
        ("postgres.connections", PG, 45.0, "connections 45"),
        ("postgres.active_queries", PG, 3.0, "active queries 3"),
        ("postgres.idle_in_transaction", PG, 2.0, "idle in transaction 2"),
        ("postgres.longest_transaction_s", PG, 12.5, "12.50 s"),
        ("postgres.cache_hit_pct", PG, 99.2, "99.20"),
        ("postgres.database_size_bytes", PG, 1048576.0, "database size"),
        ("postgres.replication_lag_s", PG, 0.0, "primary"),
        (
            "postgres.replication_lag_s",
            PG | {"in_recovery": True, "replication_lag_s": 42.0},
            42.0,
            "42",
        ),
        ("postgres.xid_wraparound_pct", PG, 10.0, "10.0%"),
        ("postgres.connections_used_pct", PG | {"max_connections": 0.0}, 0.0, "0 connections"),
    ],
)
def test_postgres_probes_read_the_snapshot(
    monkeypatch: pytest.MonkeyPatch, probe: str, snap: dict[str, Any], value: float, words: str
) -> None:
    _serve(monkeypatch, "postgres", snap)
    result = asyncio.run(evaluate_probe(CTX, _check(probe)))
    assert result.state == "ok" and result.perf["value"] == pytest.approx(value, rel=1e-3)
    assert words in result.output


def test_the_snapshot_query_is_fixed_and_read_only() -> None:
    sql = postgres.SNAPSHOT_SQL.lower()
    assert sql.lstrip().startswith("select")
    for word in ("insert", "update ", "delete", "drop", "alter", "create", "copy", "grant", ";"):
        assert word not in sql


# ---------------------------------------------------------------- grading and errors


@pytest.mark.parametrize(
    ("comparison", "warn", "crit", "state"),
    [
        ("gt", 40, 50, "warn"),
        ("gt", 30, 40, "crit"),
        ("gt", 60, 90, "ok"),
        ("lt", 60, 50, "warn"),  # 45 < 50 crit
        (None, None, None, "ok"),
    ],
)
def test_a_probe_is_graded_with_the_thresholds(
    monkeypatch: pytest.MonkeyPatch,
    comparison: str | None,
    warn: float | None,
    crit: float | None,
    state: str,
) -> None:
    _serve(monkeypatch, "postgres", PG)
    result = asyncio.run(
        evaluate_probe(CTX, _check("postgres.connections", comparison, warn, crit))
    )
    expected = "crit" if (comparison, warn, crit) == ("lt", 60, 50) else state
    assert result.state == expected
    if comparison:
        assert "warn" in result.output and "crit" in result.output


@pytest.mark.parametrize(
    ("error", "state", "words"),
    [
        (ProbeError("unreachable", "OSError"), "crit", "does not answer"),
        (ProbeError("timeout"), "crit", "in time"),
        (ProbeError("auth_failed"), "crit", "refused the credential"),
        (ProbeError("denied"), "unknown", "pg_monitor"),
        (ProbeError("no_capability"), "unknown", "capability"),
        (ProbeError("no_credential"), "unknown", "credential"),
        (ProbeError("vault_unavailable"), "unknown", "vault"),
        (ProbeError("bad_response", "HTTP 500"), "unknown", "unexpected"),
        (ProbeError("something_new"), "unknown", "probe failed"),
    ],
)
def test_a_service_that_cannot_be_read_gives_a_state_and_a_reason(
    monkeypatch: pytest.MonkeyPatch, error: ProbeError, state: str, words: str
) -> None:
    _serve(monkeypatch, "postgres", error)
    result = asyncio.run(evaluate_probe(CTX, _check("postgres.connections", "gt", 1, 2)))
    assert result.state == state and words in result.output
    assert result.output.startswith("postgres.connections:")
    assert all(code in ERRORS for code in ("unreachable", "timeout", "auth_failed", "denied"))


def test_an_error_detail_is_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, "postgres", ProbeError("bad_response", "HTTP 500"))
    result = asyncio.run(evaluate_probe(CTX, _check("postgres.connections")))
    assert result.output.endswith("(HTTP 500)")


# ---------------------------------------------------------------- Apache


STATUS = """localhost
ServerVersion: Apache/2.4.62
ServerUptimeSeconds: 86400
ReqPerSec: 12.5
BytesPerSec: 20480
BusyWorkers: 3
IdleWorkers: 7
Scoreboard: ____W_____
"""


def test_the_status_page_is_parsed() -> None:
    data = httpd.parse(STATUS)
    assert data["BusyWorkers"] == 3 and data["ReqPerSec"] == 12.5
    assert "ServerVersion" not in data and "Scoreboard" not in data  # not numbers


@pytest.mark.parametrize(
    ("probe", "value", "words"),
    [
        ("httpd.busy_workers", 3.0, "3 busy"),
        ("httpd.idle_workers", 7.0, "7 idle"),
        ("httpd.busy_workers_pct", 30.0, "3 of 10 workers busy (30.0%)"),
        ("httpd.requests_per_s", 12.5, "req/s"),
        ("httpd.bytes_per_s", 20480.0, "B/s"),
        ("httpd.uptime_s", 86400.0, "uptime"),
    ],
)
def test_apache_probes_read_the_status(
    monkeypatch: pytest.MonkeyPatch, probe: str, value: float, words: str
) -> None:
    monkeypatch.setattr(httpd, "_fetch", lambda url, timeout, verify: STATUS)
    result = asyncio.run(evaluate_probe(CTX, _check(probe, url="http://h/server-status?auto")))
    assert result.state == "ok" and result.perf["value"] == value and words in result.output


def test_a_page_that_is_not_the_status_page_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpd, "_fetch", lambda url, timeout, verify: "<html>It works</html>")
    result = asyncio.run(evaluate_probe(CTX, _check("httpd.busy_workers", url="http://h/x")))
    assert result.state == "unknown" and "server-status" in result.output


def test_older_apache_names_are_understood(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpd, "_fetch", lambda u, t, v: "BusyServers: 4\nIdleServers: 4\n")
    result = asyncio.run(
        evaluate_probe(CTX, _check("httpd.busy_workers_pct", url="http://h/s?auto"))
    )
    assert result.perf["value"] == 50.0


# ---------------------------------------------------------------- Proxmox


RESOURCES = [
    {"type": "node", "node": "pve1", "status": "online", "cpu": 0.30, "mem": 8, "maxmem": 16},
    {"type": "node", "node": "pve2", "status": "online", "cpu": 0.55, "mem": 12, "maxmem": 16},
    {"type": "node", "node": "pve3", "status": "offline"},
    {"type": "qemu", "vmid": 100, "status": "running"},
    {"type": "lxc", "vmid": 101, "status": "stopped"},
    {"type": "qemu", "vmid": 9000, "status": "stopped", "template": 1},
    {
        "type": "storage",
        "node": "pve1",
        "storage": "local",
        "status": "available",
        "disk": 40,
        "maxdisk": 100,
    },
    {
        "type": "storage",
        "node": "pve2",
        "storage": "local",
        "status": "available",
        "disk": 70,
        "maxdisk": 100,
    },
    {
        "type": "storage",
        "node": "pve1",
        "storage": "ceph",
        "status": "available",
        "shared": 1,
        "disk": 10,
        "maxdisk": 100,
    },
    {
        "type": "storage",
        "node": "pve2",
        "storage": "ceph",
        "status": "available",
        "shared": 1,
        "disk": 10,
        "maxdisk": 100,
    },
    {"type": "storage", "node": "pve3", "storage": "local", "status": "unknown"},
]


class _Pve:
    def __init__(self, status: list[dict[str, Any]], error: PveError | None = None) -> None:
        self.status, self.error, self.calls = status, error, 0

    async def call(self, key: str, method: str, path: str) -> Any:
        self.calls += 1
        if self.error:
            raise self.error
        return RESOURCES if path == "/cluster/resources" else self.status


def _pve_ctx(pve_access: _Pve) -> probes.Ctx:
    return probes.Ctx(db=None, vault=None, proxmox=pve_access)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("probe", "args", "value", "words"),
    [
        ("proxmox.nodes_offline", {}, 1.0, "1 of 3 nodes offline"),
        ("proxmox.node_cpu_max_pct", {}, 55.0, "55.0% CPU"),
        ("proxmox.node_mem_max_pct", {}, 75.0, "75.0% memory"),
        ("proxmox.storage_max_pct", {}, 70.0, "pve2/local is 70.0% full"),
        ("proxmox.storage_max_pct", {"storage": "ceph"}, 10.0, "ceph is 10.0%"),
        ("proxmox.guests_running", {}, 1.0, "1 guests running"),  # the template is not a guest
        ("proxmox.quorate", {}, 1.0, "has quorum"),
    ],
)
def test_proxmox_probes_read_the_cluster(
    probe: str, args: dict[str, Any], value: float, words: str
) -> None:
    ctx = _pve_ctx(_Pve([{"type": "cluster", "quorate": 1}]))
    result = asyncio.run(evaluate_probe(ctx, _check(probe, **args)))
    assert (
        result.state == "ok"
        and result.perf["value"] == pytest.approx(value)
        and words in result.output
    )


def test_a_cluster_without_quorum_and_a_standalone_host() -> None:
    lost = asyncio.run(
        evaluate_probe(
            _pve_ctx(_Pve([{"type": "cluster", "quorate": 0}])),
            _check("proxmox.quorate", "lt", None, 1),
        )
    )
    assert lost.state == "crit" and "NO quorum" in lost.output
    pve.CACHE._items.clear()
    alone = asyncio.run(
        evaluate_probe(_pve_ctx(_Pve([])), _check("proxmox.quorate", "lt", None, 1))
    )
    assert alone.state == "ok"


def test_a_storage_that_does_not_exist_and_a_pve_that_fails() -> None:
    ctx = _pve_ctx(_Pve([]))
    missing = asyncio.run(evaluate_probe(ctx, _check("proxmox.storage_max_pct", storage="nope")))
    assert missing.state == "unknown" and "no available storage nope" in missing.output
    pve.CACHE._items.clear()
    for error, state in (
        (PveError("pve_unreachable"), "crit"),
        (PveError("pve_forbidden", 403), "unknown"),
        (PveError("no_proxmox_capability", 404), "unknown"),
    ):
        pve.CACHE._items.clear()
        result = asyncio.run(
            evaluate_probe(_pve_ctx(_Pve([], error)), _check("proxmox.nodes_offline"))
        )
        assert result.state == state, (error.code, result)
    pve.CACHE._items.clear()
    nothing = asyncio.run(evaluate_probe(CTX, _check("proxmox.nodes_offline")))
    assert nothing.state == "unknown" and "not available" in nothing.output


def test_proxmox_is_read_once_for_all_the_probes_of_a_tick() -> None:
    access = _Pve([{"type": "cluster", "quorate": 1}])
    ctx = _pve_ctx(access)

    async def tick() -> None:
        await asyncio.gather(
            *(
                evaluate_probe(ctx, _check(p))
                for p in (
                    "proxmox.nodes_offline",
                    "proxmox.quorate",
                    "proxmox.node_cpu_max_pct",
                    "proxmox.storage_max_pct",
                )
            )
        )

    asyncio.run(tick())
    assert access.calls == 2  # resources and status, once


# ---------------------------------------------------------------- the cache


def test_a_snapshot_is_shared_for_its_ttl_and_errors_are_shared_too() -> None:
    cache = SnapshotCache(ttl_s=60)
    loads = 0

    async def load() -> dict[str, Any]:
        nonlocal loads
        loads += 1
        await asyncio.sleep(0.01)
        return {"n": loads}

    async def failing() -> dict[str, Any]:
        nonlocal loads
        loads += 1
        raise ProbeError("unreachable")

    async def run() -> None:
        first = await asyncio.gather(*(cache.get("a", load) for _ in range(5)))
        assert {x["n"] for x in first} == {1}
        for _ in range(3):
            with pytest.raises(ProbeError):
                await cache.get("b", failing)

    asyncio.run(run())
    assert loads == 2  # one per key, successes and failures alike


def test_a_snapshot_expires() -> None:
    cache = SnapshotCache(ttl_s=0)
    calls = []

    async def load() -> dict[str, Any]:
        calls.append(1)
        return {}

    async def run() -> None:
        await cache.get("a", load)
        await cache.get("a", load)

    asyncio.run(run())
    assert len(calls) == 2
