"""PostgreSQL probes: one connection from the server, one fixed read-only query.

The node needs the `postgres` capability (port, database, sslmode) with a `postgres_password`
credential. The user needs the `pg_monitor` role to see other sessions (`GRANT pg_monitor TO
janus;`); without it the counts only cover its own sessions. The session is read only and the
statement time is bounded.
"""

from typing import Any

import asyncpg

from janus_ops.monitoring.probes.base import (
    Ctx,
    Probe,
    ProbeError,
    SnapshotCache,
    Target,
    fmt,
    load_secret,
)

CACHE = SnapshotCache()

# every column is a number or a boolean; nothing here takes input
SNAPSHOT_SQL = """
SELECT
  (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS max_connections,
  (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend') AS connections,
  (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'
     AND state = 'active' AND pid <> pg_backend_pid()) AS active,
  (SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction') AS idle_in_tx,
  (SELECT coalesce(extract(epoch FROM max(now() - xact_start)), 0)
     FROM pg_stat_activity WHERE xact_start IS NOT NULL AND pid <> pg_backend_pid()
       AND backend_type = 'client backend') AS longest_tx_s,
  (SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock') AS lock_waits,
  (SELECT CASE WHEN sum(blks_hit + blks_read) = 0 THEN 100
          ELSE 100.0 * sum(blks_hit) / sum(blks_hit + blks_read) END
     FROM pg_stat_database) AS cache_hit_pct,
  pg_database_size(current_database()) AS db_size_bytes,
  pg_is_in_recovery() AS in_recovery,
  CASE WHEN pg_is_in_recovery()
       THEN coalesce(extract(epoch FROM now() - pg_last_xact_replay_timestamp()), 0)
       ELSE 0 END AS replication_lag_s,
  (SELECT max(age(datfrozenxid)) FROM pg_database) AS max_xid_age
"""
XID_LIMIT = 2_147_483_647  # transaction ids wrap at 2^31


async def _snapshot(ctx: Ctx, target: Target) -> dict[str, Any]:
    async with ctx.db.connect() as conn:
        secret = await load_secret(conn, ctx.vault, target.node_id, "postgres", "postgres_password")
    cfg = secret.config
    try:
        pg = await asyncpg.connect(
            host=secret.address,
            port=int(cfg.get("port", 5432)),
            user=secret.username,
            password=secret.password,
            database=str(cfg.get("database", "postgres")),
            ssl=str(cfg.get("sslmode", "prefer")),
            timeout=target.timeout_s,
            command_timeout=target.timeout_s,
            server_settings={
                "default_transaction_read_only": "on",
                "statement_timeout": str(int(target.timeout_s * 1000)),
                "application_name": "janus-ops",
            },
        )
    except (TimeoutError, asyncpg.exceptions.ConnectionDoesNotExistError):
        raise ProbeError("timeout") from None
    except (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError):
        raise ProbeError("auth_failed") from None
    except (OSError, asyncpg.PostgresError) as exc:
        raise ProbeError("unreachable", type(exc).__name__) from None
    try:
        row = await pg.fetchrow(SNAPSHOT_SQL)
    except asyncpg.InsufficientPrivilegeError:
        raise ProbeError("denied") from None
    except (TimeoutError, asyncpg.QueryCanceledError):
        raise ProbeError("timeout") from None
    except asyncpg.PostgresError as exc:
        raise ProbeError("bad_response", type(exc).__name__) from None
    finally:
        await pg.close()
    if row is None:
        raise ProbeError("bad_response")
    return {
        k: (float(v) if not isinstance(v, bool) and v is not None else v) for k, v in row.items()
    }


async def snapshot(ctx: Ctx, target: Target) -> dict[str, Any]:
    return await CACHE.get(f"pg:{target.node_id}", lambda: _snapshot(ctx, target))


def _pct_used(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    used, limit = s["connections"], s["max_connections"]
    pct = 100.0 * used / limit if limit else 0.0
    return pct, f"{fmt(used)} of {fmt(limit)} connections ({pct:.1f}%)"


def _simple(key: str, label: str, unit: str = "") -> Any:
    def extract(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
        value = float(s[key] or 0)
        return value, f"{label} {fmt(value)}{unit}"

    return extract


def _lag(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    if not s["in_recovery"]:
        return 0.0, "not a standby (primary): no replication lag"
    return float(s["replication_lag_s"]), f"replay lag {fmt(float(s['replication_lag_s']))} s"


def _wraparound(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    pct = 100.0 * float(s["max_xid_age"] or 0) / XID_LIMIT
    return pct, f"oldest transaction id at {pct:.1f}% of the wraparound limit"


def _p(name: str, unit: str, description: str, extract: Any) -> Probe:
    return Probe(f"postgres.{name}", "postgres", unit, description, "postgres", {}, extract)


PROBES = [
    _p("connections_used_pct", "%", "Client connections as a share of max_connections", _pct_used),
    _p("connections", "", "Client connections", _simple("connections", "connections")),
    _p("active_queries", "", "Queries running now", _simple("active", "active queries")),
    _p(
        "idle_in_transaction",
        "",
        "Sessions idle in a transaction",
        _simple("idle_in_tx", "idle in transaction"),
    ),
    _p(
        "longest_transaction_s",
        "s",
        "Age of the oldest open transaction",
        _simple("longest_tx_s", "oldest transaction", " s"),
    ),
    _p(
        "lock_waits", "", "Sessions waiting for a lock", _simple("lock_waits", "waiting for a lock")
    ),
    _p(
        "cache_hit_pct",
        "%",
        "Buffer cache hit ratio (all databases)",
        _simple("cache_hit_pct", "cache hit", " %"),
    ),
    _p(
        "database_size_bytes",
        "B",
        "Size of the configured database",
        _simple("db_size_bytes", "database size", " B"),
    ),
    _p("replication_lag_s", "s", "Replay lag of a standby (0 on a primary)", _lag),
    _p(
        "xid_wraparound_pct",
        "%",
        "Oldest transaction id as a share of the wraparound limit",
        _wraparound,
    ),
]
