"""Backup audit (FR-53): which guests have no backup, an old one, a failed one, or no job.

Inputs are plain PVE API rows so the rules are unit-testable:
- guests: /cluster/resources?type=vm (templates are skipped)
- jobs: /cluster/backup (vzdump schedules)
- archives: storage content with content=backup (vzdump files and PBS snapshots)
- failures: (vmid, time) parsed from the logs of failed vzdump tasks
"""

import re
from typing import Any

FAILED_LINE = re.compile(r"Backup of VM (\d+) failed")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
GRACE_S = 6 * 3600  # a job may run late or take hours
DAY_S = 86400
STATUS_ORDER = {"failed": 0, "missing": 1, "stale": 2, "unscheduled": 3, "ok": 4}


def _ids(value: Any) -> set[int]:
    if value in (None, ""):
        return set()
    return {int(v) for v in str(value).replace(" ", "").split(",") if v.isdigit()}


def _weekdays(schedule: str) -> set[int]:
    """Weekdays named in a systemd-style calendar event ("fri 23:59", "mon..fri", "sat,sun")."""
    days: set[int] = set()
    for token in re.findall(r"[a-z]{3}(?:\.\.[a-z]{3})?", schedule.lower()):
        first, _, last = token.partition("..")
        if first not in DAYS or (last and last not in DAYS):
            continue
        a, b = DAYS.index(first), DAYS.index(last or first)
        days.update(range(a, b + 1) if a <= b else [*range(a, 7), *range(0, b + 1)])
    return days


def schedule_period_s(schedule: str | None) -> int:
    """Longest expected gap between two runs of a vzdump job.

    Daily unless the schedule names weekdays (the largest gap between them) or a day of the
    month ("monthly", "*-*-01"), which allows a month.
    """
    text = (schedule or "").lower().strip()
    if not text:
        return DAY_S
    if "monthly" in text or re.search(r"\*-\*-\d", text) or re.search(r"-\d{2}\b", text):
        return 31 * DAY_S
    if "weekly" in text:
        return 7 * DAY_S
    days = sorted(_weekdays(text))
    if not days:
        return DAY_S
    gaps = [(days[(i + 1) % len(days)] - d) % 7 or 7 for i, d in enumerate(days)]
    return max(gaps) * DAY_S


def covering_jobs(guest: dict[str, Any], jobs: list[dict[str, Any]]) -> list[str]:
    """Ids of the enabled vzdump jobs that include this guest."""
    vmid = int(guest["vmid"])
    out = []
    for job in jobs:
        if job.get("type", "vzdump") != "vzdump" or str(job.get("enabled", 1)) == "0":
            continue
        if job.get("node") and job["node"] != guest.get("node"):
            continue
        if str(job.get("all", 0)) == "1":
            hit = vmid not in _ids(job.get("exclude"))
        elif job.get("pool"):
            hit = job["pool"] == guest.get("pool")
        else:
            hit = vmid in _ids(job.get("vmid"))
        if hit:
            out.append(str(job.get("id") or job.get("comment") or "job"))
    return out


def failures_from_log(lines: list[str]) -> set[int]:
    return {int(m.group(1)) for line in lines if (m := FAILED_LINE.search(line))}


def audit(
    guests: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    archives: list[dict[str, Any]],
    failures: list[tuple[int, int]],
    now: int,
    max_age_s: int | None = None,
) -> list[dict[str, Any]]:
    """One row per guest, worst first: failed > missing > stale > unscheduled > ok.

    A backup is stale when older than `max_age_s`, or, by default, than the period of the most
    frequent job covering the guest plus GRACE_S (a day for guests without a job).
    """
    periods = {
        str(j.get("id") or j.get("comment") or "job"): schedule_period_s(
            j.get("schedule") or j.get("starttime")
        )
        for j in jobs
    }
    latest: dict[int, dict[str, Any]] = {}
    counts: dict[int, int] = {}
    for a in archives:
        if a.get("vmid") is None:
            continue
        vmid = int(a["vmid"])
        counts[vmid] = counts.get(vmid, 0) + 1
        if vmid not in latest or int(a.get("ctime", 0)) > int(latest[vmid].get("ctime", 0)):
            latest[vmid] = a
    failed_at: dict[int, int] = {}
    for vmid, at in failures:
        failed_at[vmid] = max(at, failed_at.get(vmid, 0))

    rows: list[dict[str, Any]] = []
    for g in guests:
        if g.get("template"):
            continue
        vmid = int(g["vmid"])
        jobs_for = covering_jobs(g, jobs)
        limit = max_age_s or min((periods[j] for j in jobs_for), default=DAY_S) + GRACE_S
        last = latest.get(vmid)
        last_time = int(last["ctime"]) if last else None
        failure = failed_at.get(vmid)
        if failure and (last_time is None or failure > last_time):
            status = "failed"
        elif last is None:
            status = "missing"
        elif now - (last_time or 0) > limit:
            status = "stale"
        elif not jobs_for:
            status = "unscheduled"
        else:
            status = "ok"
        row: dict[str, Any] = {
            "vmid": vmid,
            "name": g.get("name"),
            "type": g.get("type"),
            "node": g.get("node"),
            "status": status,
            "jobs": jobs_for,
            "last_backup": last_time,
            "age_s": now - last_time if last_time else None,
            "max_age_s": limit,
            "size": int(last["size"]) if last and last.get("size") is not None else None,
            "storage": str(last.get("volid", "")).split(":", 1)[0] if last else None,
            "verified": (last.get("verification") or {}).get("state") if last else None,
            "archives": counts.get(vmid, 0),
            "failed_at": failure,
        }
        rows.append(row)
    rows.sort(key=lambda r: (STATUS_ORDER[r["status"]], r["vmid"]))
    return rows
