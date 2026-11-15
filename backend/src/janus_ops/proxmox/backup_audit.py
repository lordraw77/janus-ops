"""Collects the inputs of the backup audit (FR-53) from one Proxmox cluster.

Used by the API (/proxmox/{key}/backups) and by the check engine (kind `backup`, FR-53 + F3).
Pure rules live in `backups.py`; this module only talks to the PVE API.
A PBS datastore appears as a storage with content `backup`, read like any other storage.
"""

import asyncio
import time
from typing import Any
from urllib.parse import quote

from janus_ops.proxmox import backups
from janus_ops.proxmox.access import ProxmoxAccess
from janus_ops.proxmox.client import PveError

FAILED_TASKS = 10


async def collect(access: ProxmoxAccess, key: str, max_age_s: int | None = None) -> dict[str, Any]:
    """Audit of one cluster. Top-level PVE errors raise PveError; per-storage/node ones are
    listed in `errors` so one bad member does not hide the rest."""
    resources, jobs = await asyncio.gather(
        access.call(key, "GET", "/cluster/resources"),
        access.call(key, "GET", "/cluster/backup"),
    )
    guests = [r for r in resources if r.get("type") in ("qemu", "lxc")]
    online = {
        r["node"] for r in resources if r.get("type") == "node" and r.get("status") == "online"
    }
    stores: dict[str, tuple[str, str]] = {}  # shared storages are read once
    for r in resources:
        if r.get("type") != "storage" or r.get("status") != "available":
            continue
        if "backup" not in (r.get("content") or "").split(",") or r["node"] not in online:
            continue
        name = r["storage"] if r.get("shared") else f"{r['node']}/{r['storage']}"
        stores.setdefault(name, (r["node"], r["storage"]))

    errors: list[str] = []

    async def content(node: str, storage: str) -> list[dict[str, Any]]:
        try:
            rows: list[dict[str, Any]] = await access.call(
                key,
                "GET",
                f"/nodes/{node}/storage/{quote(storage, safe='')}/content",
                params={"content": "backup"},
            )
            return rows
        except PveError as exc:
            errors.append(f"{storage}@{node}: {exc.code}")
            return []

    async def failed(node: str) -> list[dict[str, Any]]:
        try:
            rows: list[dict[str, Any]] = await access.call(
                key,
                "GET",
                f"/nodes/{node}/tasks",
                params={"typefilter": "vzdump", "errors": 1, "limit": FAILED_TASKS},
            )
            return rows
        except PveError as exc:
            errors.append(f"tasks@{node}: {exc.code}")
            return []

    archives_lists, failed_lists = await asyncio.gather(
        asyncio.gather(*(content(n, s) for n, s in stores.values())),
        asyncio.gather(*(failed(n) for n in sorted(online))),
    )
    archives = [a for rows in archives_lists for a in rows]
    failed_tasks = [t for rows in failed_lists for t in rows if t.get("status") != "OK"]
    failures: list[tuple[int, int]] = []

    async def parse(t: dict[str, Any]) -> None:
        at = int(t.get("endtime") or t.get("starttime") or 0)
        if str(t.get("id") or "").isdigit():
            failures.append((int(t["id"]), at))
        try:
            lines = await access.call(
                key,
                "GET",
                f"/nodes/{t['node']}/tasks/{quote(t['upid'], safe='')}/log",
                params={"limit": 2000},
            )
        except PveError:
            return
        for vmid in backups.failures_from_log([line.get("t", "") for line in lines]):
            failures.append((vmid, at))

    await asyncio.gather(*(parse(t) for t in failed_tasks))
    rows = backups.audit(guests, jobs, archives, failures, int(time.time()), max_age_s)
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {
        "max_age_h": max_age_s // 3600 if max_age_s else None,
        "summary": summary,
        "guests": rows,
        "jobs": [
            {
                "id": j.get("id"),
                "enabled": str(j.get("enabled", 1)) != "0",
                "schedule": j.get("schedule") or j.get("starttime"),
                "storage": j.get("storage"),
                "node": j.get("node"),
                "selection": "all"
                if str(j.get("all", 0)) == "1"
                else f"pool {j['pool']}"
                if j.get("pool")
                else j.get("vmid"),
                "exclude": j.get("exclude"),
                "mode": j.get("mode"),
                "comment": j.get("comment"),
            }
            for j in jobs
        ],
        "failed_tasks": [
            {k: t.get(k) for k in ("upid", "node", "id", "status", "starttime", "endtime")}
            for t in failed_tasks
        ],
        "storages": sorted(stores),
        "errors": errors,
    }
