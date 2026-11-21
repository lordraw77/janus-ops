"""Continuous aggregates of the metrics: 1 minute and 1 hour (NFR-05).

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-18

A chart of 24 hours or more used to read the raw points of every series it shows. With hundreds of
nodes that is millions of rows per chart. Two continuous aggregates keep avg, max and the number of
points per series and bucket:

- `metric_1m`: one minute, kept 14 days (charts of 6 and 24 hours, the dashboard sparklines);
- `metric_1h`: one hour, kept 400 days (7, 30 and 90 day charts, beyond the 30 days of raw points).

They are NOT real-time (`materialized_only = true`): a real-time view aggregates the raw points
of EVERY series newer than the last refresh before any filter by node applies (measured with
17,500 series: 1 s for a chart that takes 2 ms from the materialised part). The chart code reads
the materialised buckets and adds the newer ones from the raw points of the series it shows (see
`agents/telemetry.py`), which stays correct if a refresh is late. The refresh policy has no start
offset, so the first run also materialises the history that is already there; later runs only redo
what changed (a point that arrives late, e.g. after the agent buffered it during an outage,
invalidates its own bucket).
The views are created `WITH NO DATA`, because a continuous aggregate with data cannot be created in
a transaction. The raw points keep their 30 days.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEWS = (
    # name, bucket, refresh every, newest bucket left to the real-time part, kept
    ("metric_1m", "1 minute", "1 minute", "1 minute", "14 days"),
    ("metric_1h", "1 hour", "15 minutes", "1 hour", "400 days"),
)


def upgrade() -> None:
    # the dashboard reads the latest value of a handful of series names across all nodes
    op.execute("CREATE INDEX metric_series_name_idx ON metric_series (name)")
    for name, bucket, every, lag, keep in VIEWS:
        op.execute(
            f"CREATE MATERIALIZED VIEW {name} WITH (timescaledb.continuous) AS"  # noqa: S608
            f" SELECT series_id, time_bucket(INTERVAL '{bucket}', time) AS bucket,"
            " avg(value) AS avg, max(value) AS max, count(*) AS n"
            " FROM metric_point GROUP BY series_id, bucket WITH NO DATA"
        )
        op.execute(f"ALTER MATERIALIZED VIEW {name} SET (timescaledb.materialized_only = true)")
        # one series over a time range: the same access path as the raw points
        op.execute(f"CREATE INDEX {name}_series_bucket_idx ON {name} (series_id, bucket DESC)")
        op.execute(
            f"SELECT add_continuous_aggregate_policy('{name}', start_offset => NULL,"
            f" end_offset => INTERVAL '{lag}', schedule_interval => INTERVAL '{every}')"
        )
        op.execute(f"SELECT add_retention_policy('{name}', INTERVAL '{keep}')")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS metric_series_name_idx")
    for name, *_ in reversed(VIEWS):
        # TimescaleDB's own policy job can touch the aggregate while it is dropped and fail the
        # drop with "tuple concurrently deleted" (internal_error): try again, a few times.
        op.execute(
            "DO $$ BEGIN FOR i IN 1..10 LOOP BEGIN"
            f" DROP MATERIALIZED VIEW IF EXISTS {name}; EXIT;"
            " EXCEPTION WHEN internal_error THEN PERFORM pg_sleep(0.5); END; END LOOP; END $$"
        )
