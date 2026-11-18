"""Charts from the continuous aggregates (migration 0025, NFR-05): same numbers as the raw points.

Real TimescaleDB (scripts/integration-test.sh). The aggregates are refreshed by hand here; in the
stack a policy does it every minute / 15 minutes.
"""

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops.agents.telemetry import RANGES, _chart_sql
from janus_ops.db import alembic_config

from ._it import CONFIGURED, DB_URL, run_sql, signed_in
from .test_agents_integration import make_node

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")  # also proves that 0025 comes down cleanly
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def refresh(view: str) -> None:
    """A refresh cannot run inside a transaction."""

    async def _go() -> None:
        engine = create_async_engine(DB_URL, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.execute(
                    text(
                        f"CALL refresh_continuous_aggregate('{view}', NULL,"
                        " now() - interval '1 minute')"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_go())


def seed(name: str) -> tuple[str, int]:
    """One series with a point a minute for 3 days: 10 until 20 minutes ago, then 20."""
    with signed_in("bob") as op:
        node = make_node(op, name, agent=False)
    sid = run_sql(
        "INSERT INTO metric_series (node_id, name, labels, unit)"
        " VALUES (:n, 'cpu.usage_pct', '{}', '%') RETURNING id",
        {"n": node},
    )[0][0]
    run_sql(
        "INSERT INTO metric_point (time, series_id, value)"
        " SELECT t, :s, CASE WHEN t < now() - interval '20 minutes' THEN 10 ELSE 20 END"
        " FROM generate_series(now() - interval '3 days', now() - interval '5 seconds',"
        " interval '1 minute') t",
        {"s": sid},
    )
    return node, sid


def chart(node: str, rng: str) -> dict[str, Any]:
    with signed_in("bob") as op:
        r = op.get(
            f"/api/v1/telemetry/nodes/{node}", params={"name": "cpu.usage_pct", "range": rng}
        )
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()
        return body


def raw_points(sid: int, rng: str) -> dict[int, tuple[float, float]]:
    span, bucket, _ = RANGES[rng]
    rows = run_sql(_chart_sql(None), {"ids": [sid], "span": span, "bucket": bucket})
    return {int(r.bucket.timestamp() * 1000): (r.v, r.mx) for r in rows}


def test_ranges_beyond_a_week_exist() -> None:
    assert RANGES["30d"][1] == timedelta(hours=6)
    assert RANGES["90d"][1] == timedelta(hours=12)


def test_before_the_first_refresh_the_chart_comes_from_the_raw_points() -> None:
    node, sid = seed("agg-before-refresh")
    got = chart(node, "24h")
    pts = got["series"][0]["points"]
    assert pts and pts[-1]["v"] == 20 and pts[0]["v"] == 10
    assert {p["t"]: (p["v"], p["max"]) for p in pts}.keys() <= raw_points(sid, "24h").keys()


@pytest.mark.parametrize("rng", ["6h", "24h", "7d"])
def test_the_aggregates_give_the_numbers_of_the_raw_points(rng: str) -> None:
    node, sid = seed(f"agg-same-{rng}")
    refresh("metric_1m")
    refresh("metric_1h")
    assert run_sql("SELECT count(*) FROM metric_1m WHERE series_id = :s", {"s": sid})[0][0] > 0
    got = chart(node, rng)
    assert got["bucket_s"] == int(RANGES[rng][1].total_seconds())
    api = {p["t"]: (p["v"], p["max"]) for p in got["series"][0]["points"]}
    raw = raw_points(sid, rng)
    assert api.keys() <= raw.keys()
    # the oldest raw bucket is only partly inside the range: the aggregate leaves it out
    assert len(raw) - len(api) <= 1
    for t, (v, mx) in api.items():
        assert v == pytest.approx(raw[t][0], abs=1e-3)
        assert mx == pytest.approx(raw[t][1], abs=1e-3)


def test_points_newer_than_the_last_refresh_are_in_the_chart() -> None:
    node, sid = seed("agg-tail")
    refresh("metric_1m")
    refresh("metric_1h")
    # arrives after the refresh: only the raw points have it
    run_sql(
        "INSERT INTO metric_point (time, series_id, value)"
        " VALUES (now() - interval '1 second', :s, 99)",
        {"s": sid},
    )
    for rng in ("24h", "7d"):
        pts = chart(node, rng)["series"][0]["points"]
        assert pts[-1]["max"] == 99, rng


def test_a_week_old_series_can_be_charted_for_90_days() -> None:
    node, _ = seed("agg-90d")
    refresh("metric_1h")
    got = chart(node, "90d")
    assert got["bucket_s"] == 43200
    assert got["series"][0]["points"]
