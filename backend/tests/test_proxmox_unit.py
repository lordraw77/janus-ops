"""Proxmox VE client (ticket and token auth, CSRF, retries, errors) and the backup audit rules."""

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest

from janus_ops.proxmox import backups
from janus_ops.proxmox.client import Auth, PveClient, PveError

DAY = 86400


class FakePve:
    """Answers /access/ticket and a few API paths; records what the client sent."""

    def __init__(self, expire_first: bool = False, tfa: bool = False) -> None:
        self.calls: list[httpx2.Request] = []
        self.logins = 0
        self.expire_first, self.tfa = expire_first, tfa

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        path = request.url.path.removeprefix("/api2/json")
        if path == "/access/ticket":
            form = parse_qs(request.content.decode())
            if form.get("password") != ["s3cret"]:
                return httpx2.Response(401)
            self.logins += 1
            data: dict[str, Any] = {"ticket": f"PVE:t{self.logins}", "CSRFPreventionToken": "csrf"}
            if self.tfa:
                data["NeedTFA"] = 1
            return httpx2.Response(200, json={"data": data})
        auth = request.headers.get("authorization", "")
        cookie = request.headers.get("cookie", "")
        if not auth and "PVEAuthCookie=" not in cookie:
            return httpx2.Response(401)
        if self.expire_first and cookie == "PVEAuthCookie=PVE:t1":
            return httpx2.Response(401)
        if path == "/nodes/pve/qemu/101/status/start":
            if request.headers.get("csrfpreventiontoken") != "csrf" and not auth:
                return httpx2.Response(401)
            return httpx2.Response(200, json={"data": "UPID:pve:1:2:3:qmstart:101:root@pam:"})
        if path == "/cluster/nextid":
            return httpx2.Response(200, json={"data": "112"})
        if path == "/nodes/pve/qemu/999/config":
            return httpx2.Response(
                500, json={"data": None, "message": "Configuration file does not exist\n"}
            )
        if path == "/nodes/pve/qemu/101/clone":
            return httpx2.Response(
                400, json={"data": None, "errors": {"newid": "VM 101 already exists"}}
            )
        return httpx2.Response(404)


def client(fake: FakePve, auth: Auth) -> PveClient:
    return PveClient("pve.test", 8006, True, auth, transport=httpx2.MockTransport(fake))


PASSWORD = Auth("proxmox_password", "root@pam", "s3cret")


def test_ticket_login_and_csrf() -> None:
    fake = FakePve()

    async def run() -> None:
        c = client(fake, PASSWORD)
        assert await c.get("/cluster/nextid") == "112"
        assert await c.post("/nodes/pve/qemu/101/status/start") == (
            "UPID:pve:1:2:3:qmstart:101:root@pam:"
        )
        await c.close()

    asyncio.run(run())
    assert fake.logins == 1  # ticket reused
    get, post = fake.calls[1], fake.calls[2]
    assert "csrfpreventiontoken" not in get.headers  # only unsafe requests carry it
    assert post.headers["csrfpreventiontoken"] == "csrf"
    assert fake.calls[0].url.host == "pve.test" and fake.calls[0].url.port == 8006


def test_expired_ticket_is_renewed_once() -> None:
    fake = FakePve(expire_first=True)

    async def run() -> Any:
        c = client(fake, PASSWORD)
        try:
            return await c.get("/cluster/nextid")
        finally:
            await c.close()

    assert asyncio.run(run()) == "112"
    assert fake.logins == 2


def test_token_auth_has_no_login() -> None:
    fake = FakePve()
    token = Auth("proxmox_token", "root@pam!janus", "uuid-secret")

    async def run() -> None:
        c = client(fake, token)
        await c.post("/nodes/pve/qemu/101/status/start")
        await c.close()

    asyncio.run(run())
    assert fake.logins == 0
    assert fake.calls[0].headers["authorization"] == "PVEAPIToken=root@pam!janus=uuid-secret"


def test_errors() -> None:
    async def fails(auth: Auth, path: str, fake: FakePve | None = None) -> PveError:
        c = client(fake or FakePve(), auth)
        try:
            with pytest.raises(PveError) as exc:
                await c.get(path) if "clone" not in path else await c.post(path, {"newid": 101})
            return exc.value
        finally:
            await c.close()

    wrong = Auth("proxmox_password", "root@pam", "nope")
    assert asyncio.run(fails(wrong, "/cluster/nextid")).code == "pve_auth_failed"
    assert asyncio.run(fails(PASSWORD, "/x", FakePve(tfa=True))).code == "pve_tfa_required"
    err = asyncio.run(fails(PASSWORD, "/nodes/pve/qemu/999/config"))
    assert (err.code, err.status, err.detail) == (
        "pve_error",
        500,
        "Configuration file does not exist",
    )
    err = asyncio.run(fails(PASSWORD, "/nodes/pve/qemu/101/clone"))
    assert (err.status, err.detail) == (400, "newid: VM 101 already exists")

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    async def unreachable() -> str:
        c = PveClient("pve.test", 8006, False, PASSWORD, transport=httpx2.MockTransport(down))
        try:
            await c.get("/version")
        except PveError as exc:
            return exc.code
        finally:
            await c.close()
        return "no error"

    assert asyncio.run(unreachable()) == "pve_unreachable"


def test_booleans_and_none_are_cleaned() -> None:
    sent: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.content)
        return httpx2.Response(200, json={"data": "UPID:x"})

    async def run() -> None:
        c = PveClient(
            "h",
            8006,
            True,
            Auth("proxmox_token", "a@pve!t", "s"),
            transport=httpx2.MockTransport(handler),
        )
        await c.post("/x", {"full": True, "online": False, "name": None, "newid": 120})
        await c.close()

    asyncio.run(run())
    assert parse_qs(sent[0].decode()) == {"full": ["1"], "online": ["0"], "newid": ["120"]}


def test_schedule_periods() -> None:
    p = backups.schedule_period_s
    assert p("21:00") == DAY and p("mon..sun 00:00") == DAY and p(None) == DAY
    assert p("fri 23:59") == 7 * DAY
    assert p("mon,thu 02:00") == 4 * DAY  # thu -> mon
    assert p("sat..mon 01:00") == 5 * DAY  # mon -> sat
    assert p("mon..fri") == 3 * DAY  # fri -> mon
    assert p("weekly") == 7 * DAY and p("monthly") == 31 * DAY and p("*-*-01 03:00") == 31 * DAY


def test_covering_jobs() -> None:
    guest = {"vmid": 105, "node": "pve02", "pool": "prod"}
    jobs = [
        {"id": "all", "all": 1, "exclude": "105,106"},
        {"id": "list", "vmid": "101, 105"},
        {"id": "pool", "pool": "prod"},
        {"id": "other-node", "all": 1, "node": "pve"},
        {"id": "disabled", "all": 1, "enabled": 0},
    ]
    assert backups.covering_jobs(guest, jobs) == ["list", "pool"]


def test_failures_from_log() -> None:
    log = [
        "INFO: Starting Backup of VM 101 (qemu)",
        "ERROR: Backup of VM 105 failed - unable to open file",
        "INFO: Finished Backup of VM 101 (00:01:02)",
        "ERROR: Backup of VM 107 failed - job aborted",
    ]
    assert backups.failures_from_log(log) == {105, 107}


def test_audit_statuses() -> None:
    now = 100 * DAY
    guests = [
        {"vmid": vmid, "name": f"g{vmid}", "type": "qemu", "node": "pve"}
        for vmid in (101, 102, 103, 104, 105, 106)
    ] + [{"vmid": 199, "name": "tpl", "type": "qemu", "node": "pve", "template": 1}]
    jobs = [
        {"id": "daily", "vmid": "101,102,104,105", "schedule": "02:00"},
        {"id": "weekly", "vmid": "106", "schedule": "sun 03:00"},
    ]

    def archive(vmid: int, age: int) -> dict[str, Any]:
        return {"vmid": vmid, "ctime": now - age, "size": 1000, "volid": f"nas:backup/{vmid}"}

    archives = [
        archive(101, DAY // 2),
        archive(101, 3 * DAY),
        archive(102, 3 * DAY),  # daily job: too old
        archive(103, DAY // 2),  # no job covers it
        archive(105, 2 * DAY),
        archive(106, 6 * DAY),  # weekly job: still fine
    ]
    failures = [(105, now - DAY), (101, now - 2 * DAY)]  # 101 succeeded after its failure
    rows = backups.audit(guests, jobs, archives, failures, now)
    status = {r["vmid"]: r["status"] for r in rows}
    assert status == {
        101: "ok",
        102: "stale",
        103: "unscheduled",
        104: "missing",
        105: "failed",
        106: "ok",
    }
    assert [r["vmid"] for r in rows] == [105, 104, 102, 103, 101, 106]  # worst first
    first = next(r for r in rows if r["vmid"] == 101)
    assert (first["archives"], first["storage"], first["age_s"]) == (2, "nas", DAY // 2)
    assert next(r for r in rows if r["vmid"] == 106)["max_age_s"] == 7 * DAY + backups.GRACE_S
    # an explicit maximum age overrides the schedules
    strict = backups.audit(guests, jobs, archives, failures, now, max_age_s=DAY // 4)
    assert {r["vmid"]: r["status"] for r in strict}[106] == "stale"
    assert json.dumps(rows)  # plain JSON for the API
