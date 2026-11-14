"""Telemetry from agents into TimescaleDB (ADR-005): validation, series lookup, batch insert.

Bad points are dropped, never the whole batch: a batch refused forever would block the agent's
buffer behind it. Inserts are idempotent (unique series_id + time), so a batch resent after a
lost acknowledgement adds nothing.
"""

import json
import math
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

NAME = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
LABEL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_LABELS = 4
MAX_LABEL_VALUE = 128
MAX_SAMPLES = 120
MAX_POINTS = 5000  # per sample
MAX_SERIES_PER_NODE = 2000
PAST = timedelta(hours=48)  # agents buffer 24 h; leave room for clock skew
FUTURE = timedelta(minutes=5)

UNITS: dict[str, str] = {
    "cpu.usage_pct": "%",
    "cpu.iowait_pct": "%",
    "cpu.steal_pct": "%",
    "mem.used_pct": "%",
    "swap.used_pct": "%",
    "mem.total_bytes": "B",
    "mem.available_bytes": "B",
    "swap.used_bytes": "B",
    "disk.read_bytes_ps": "B/s",
    "disk.write_bytes_ps": "B/s",
    "disk.util_pct": "%",
    "fs.used_pct": "%",
    "fs.inodes_used_pct": "%",
    "fs.free_bytes": "B",
    "fs.size_bytes": "B",
    "net.rx_bytes_ps": "B/s",
    "net.tx_bytes_ps": "B/s",
    "system.uptime_s": "s",
}


class BadMessage(Exception):
    """The message breaks the protocol (not just a bad point): the connection is closed."""


SeriesKey = tuple[str, str]  # (name, canonical labels JSON)


@dataclass
class Batch:
    times: list[datetime] = field(default_factory=list)
    keys: list[SeriesKey] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    dropped: int = 0


def _labels(raw: Any) -> str | None:
    if raw is None:
        return "{}"
    if not isinstance(raw, dict) or len(raw) > MAX_LABELS:
        return None
    for k, v in raw.items():
        if not LABEL_KEY.match(k) or not isinstance(v, str) or len(v) > MAX_LABEL_VALUE:
            return None
    return json.dumps(raw, sort_keys=True, separators=(",", ":"))


def parse_samples(samples: Any, now: datetime) -> Batch:
    """Validate a metrics message's samples; raises BadMessage on a malformed structure."""
    if not isinstance(samples, list) or len(samples) > MAX_SAMPLES:
        raise BadMessage("samples")
    batch = Batch()
    oldest, newest = now - PAST, now + FUTURE
    for sample in samples:
        if not isinstance(sample, dict):
            raise BadMessage("sample")
        ts, points = sample.get("ts"), sample.get("points")
        if not isinstance(ts, int) or isinstance(ts, bool) or not isinstance(points, list):
            raise BadMessage("sample")
        if len(points) > MAX_POINTS:
            raise BadMessage("points")
        try:
            when = datetime.fromtimestamp(ts / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            batch.dropped += len(points)
            continue
        if not oldest <= when <= newest:
            batch.dropped += len(points)
            continue
        seen: set[SeriesKey] = set()
        for p in points:
            if not isinstance(p, dict):
                batch.dropped += 1
                continue
            name, value, labels = p.get("n"), p.get("v"), _labels(p.get("l"))
            if (
                not isinstance(name, str)
                or not NAME.match(name)
                or labels is None
                or not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                batch.dropped += 1
                continue
            key = (name, labels)
            if key in seen:  # one value per series and timestamp
                batch.dropped += 1
                continue
            seen.add(key)
            batch.times.append(when)
            batch.keys.append(key)
            batch.values.append(float(value))
    return batch


class SeriesCache:
    """series id per (name, labels) of one node; lives as long as the agent connection."""

    def __init__(self, node_id: uuid.UUID) -> None:
        self.node_id = node_id
        self.ids: dict[SeriesKey, int] = {}

    async def resolve(self, conn: AsyncConnection, keys: Iterable[SeriesKey]) -> None:
        missing = sorted({k for k in keys if k not in self.ids})
        if not missing:
            return
        names = [k[0] for k in missing]
        labels = [k[1] for k in missing]
        rows = await conn.execute(
            text(
                "SELECT s.id, s.name, s.labels::text AS labels FROM metric_series s"
                " JOIN unnest(CAST(:names AS text[]), CAST(:labels AS jsonb[])) AS w(name, labels)"
                " ON s.name = w.name AND s.labels = w.labels WHERE s.node_id = :node"
            ),
            {"node": self.node_id, "names": names, "labels": labels},
        )
        for r in rows:
            self.ids[(r.name, _canonical(r.labels))] = r.id
        new = [k for k in missing if k not in self.ids]
        if not new:
            return
        count = await conn.scalar(
            text("SELECT count(*) FROM metric_series WHERE node_id = :node"),
            {"node": self.node_id},
        )
        room = max(0, MAX_SERIES_PER_NODE - int(count or 0))
        new = new[:room]  # a runaway label (e.g. one per process) must not flood the catalogue
        if not new:
            return
        rows = await conn.execute(
            text(
                "INSERT INTO metric_series (node_id, name, labels, unit)"
                " SELECT :node, w.name, w.labels, w.unit FROM unnest("
                " CAST(:names AS text[]), CAST(:labels AS jsonb[]), CAST(:units AS text[]))"
                " AS w(name, labels, unit)"
                " ON CONFLICT (node_id, name, labels) DO UPDATE SET unit = EXCLUDED.unit"
                " RETURNING id, name, labels::text AS labels"
            ),
            {
                "node": self.node_id,
                "names": [k[0] for k in new],
                "labels": [k[1] for k in new],
                "units": [UNITS.get(k[0]) for k in new],
            },
        )
        for r in rows:
            self.ids[(r.name, _canonical(r.labels))] = r.id


def _canonical(labels_json: str) -> str:
    return json.dumps(json.loads(labels_json), sort_keys=True, separators=(",", ":"))


async def store(conn: AsyncConnection, cache: SeriesCache, batch: Batch) -> int:
    """Insert the batch; returns the number of new points (duplicates are ignored)."""
    if not batch.keys:
        return 0
    await cache.resolve(conn, batch.keys)
    times, series, values = [], [], []
    for when, key, value in zip(batch.times, batch.keys, batch.values, strict=True):
        sid = cache.ids.get(key)
        if sid is None:  # over the per-node series limit
            batch.dropped += 1
            continue
        times.append(when)
        series.append(sid)
        values.append(value)
    if not series:
        return 0
    result = await conn.execute(
        text(
            "INSERT INTO metric_point (time, series_id, value)"
            " SELECT * FROM unnest(CAST(:t AS timestamptz[]), CAST(:s AS bigint[]),"
            " CAST(:v AS double precision[]))"
            " ON CONFLICT DO NOTHING"
        ),
        {"t": times, "s": series, "v": values},
    )
    return int(getattr(result, "rowcount", 0) or 0)
