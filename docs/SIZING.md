# Sizing and load test (NFR-05)

NFR-05: sustained ingest for **500 nodes at 10 s, about 10k points/s**. This page says what was
measured, on what, and what to size.

## What was measured

A synthetic load test on a throwaway TimescaleDB 2.32 (PostgreSQL 16) container, not on the live
stack: 500 nodes, each with an agent row and its series, 500 concurrent "agents" each sending one
batch every 10 s through the same code the gateway runs (`parse_samples` + `store` and the
`last_seen_at` update, one transaction per batch), with the dashboard queries running next to it.
The web socket layer is not part of it: the numbers are those of validation and the database.

Host: 4 CPUs, 7.5 GB RAM, one rotational disk, shared with the live stack (so a worst case).

| Scenario | Points/s | Batch latency p50 / p95 / p99 / max | Errors | Dashboard `latest` / sparklines |
|---|---|---|---|---|
| 500 nodes x 35 series (what the agent sends today), 3 min | 1.7k | 8 / 22 / 47 / 312 ms | 0 | 144 / 134 ms |
| 500 nodes x 200 series (the NFR-05 estimate), 3 min, pool 20+20 | 10k | 18 / 44 / 131 / 390 ms | 0 | 203 / 29 ms |

Things the test found, now fixed:

- **The connection pool.** With SQLAlchemy's default of 5+10 connections, 10k points/s starved the
  pool: batches waited up to 37 s and some failed. `JANUS_DB_POOL_SIZE` and `JANUS_DB_MAX_OVERFLOW`
  (default 20 and 20) are now settings.
- **The dashboard `latest` query** sorted every point of the last five minutes of every series:
  650 ms at 100k series. It now takes the newest point of each series through its index: 200 ms.
- **The sparklines and the charts** read raw points. They now read the continuous aggregates of
  migration 0025 (see below).

## Continuous aggregates

`metric_1m` (kept 14 days) and `metric_1h` (kept 400 days) hold avg, max and the number of points
per series and bucket. A chart of 6 h or more reads them; 7, 30 and 90 day charts read the hourly
one, so they reach past the 30 days of raw points. Per chart of 4 series on one node (14 days of
raw history of that node): 24 h 25 -> 9 ms, 7 d 131 -> 15 ms, 30 d 249 -> 9 ms; 90 d was not
possible before. The numbers equal those of the raw points (difference below 1e-12), which an
integration test checks.

They are **not** real-time views. A real-time continuous aggregate aggregates the raw points of
every series newer than the last refresh before any filter by node applies: with 17,500 series a
chart took 1 s instead of 2 ms. The chart code reads the materialised buckets and adds the newer
ones from the raw points of the series it shows, so it is exact even if a refresh is late; before
the first refresh it falls back to the raw points.

The refresh costs CPU in the database: at 10k points/s the one-minute refresh takes 8-15 s of
work every minute on the test host.

## Sizing

- **Storage:** about 100 bytes per point on disk before compression (measured: 1.7 GB for 16.3M
  points, index included). At 35 series per node and 10 s that is 500 x 35 x 8640 = 151M points a day,
  about 15 GB a day for the first 7 days, until the compression policy takes over. The compression
  ratio was **not measured** (the synthetic values are random, which compresses badly): measure it
  on your own data before sizing the disk for the 30 days of raw points.
- **Memory and disk:** keep the working set (the last day or two of points and the indexes) in
  memory. In the test the same load that ran with p99 131 ms on a small database ran with p95 11-14 s
  stalls on a 3.3 GB one on this host, which was swapping on a rotational disk. Use SSD and give
  the database enough RAM.
- **Connections:** the default pool (20 + 20 overflow) is enough for 500 agents at 10 s; keep the
  total below the database's `max_connections` (100 by default).
- **One process:** the server is a single Python process; at 10k points/s one core is not the
  limit (the generator used about a fifth of a core), but it is the next thing to watch beyond 2,000 nodes.
