"""Basic probes of any node service (a product of `service_catalog.py`), read from the server.

The node's service of the given kind (`kind` argument, `service` to pick by name when there are
several) holds the port and, for products that have one, the health URL. Nothing needs a
credential or the agent: the server opens a TCP connection, or fetches the health URL, and reports
how long it took or the status it got. The connection is opened and closed at once; no data is
sent over the TCP one.
"""

import asyncio
import contextlib
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from janus_ops.monitoring.probes.base import (
    Ctx,
    Probe,
    ProbeError,
    SnapshotCache,
    Target,
    fmt,
    load_service,
)

TCP_CACHE = SnapshotCache()
HTTP_CACHE = SnapshotCache()


def _key(prefix: str, target: Target) -> str:
    return f"{prefix}:{target.node_id}:{target.args['kind']}:{target.args.get('service') or ''}"


async def _config(ctx: Ctx, target: Target) -> tuple[str, dict[str, Any]]:
    async with ctx.db.connect() as conn:
        return await load_service(
            conn,
            target.node_id,
            str(target.args["kind"]),
            str(target.args["service"]) if target.args.get("service") else None,
        )


async def tcp_snapshot(ctx: Ctx, target: Target) -> dict[str, Any]:
    async def load() -> dict[str, Any]:
        address, config = await _config(ctx, target)
        port = config.get("port")
        if not isinstance(port, int):
            raise ProbeError("no_port")
        start = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), target.timeout_s
            )
        except TimeoutError:
            raise ProbeError("timeout") from None
        except OSError as exc:
            raise ProbeError("unreachable", exc.strerror or type(exc).__name__) from None
        ms = (time.monotonic() - start) * 1000
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return {"tcp_ms": ms, "port": port}

    return await TCP_CACHE.get(_key("tcp", target), load)


def _get(url: str, timeout: float, verify_tls: bool) -> tuple[int, float]:
    context = ssl.create_default_context()
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={"User-Agent": "Janus-Ops probe"})  # noqa: S310
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
            response.read(1 << 14)
            return int(response.status), (time.monotonic() - start) * 1000
    except urllib.error.HTTPError as exc:
        # an error status is still an answer: the probe reports the number
        return int(exc.code), (time.monotonic() - start) * 1000
    except TimeoutError:
        raise ProbeError("timeout") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProbeError("unreachable", str(getattr(exc, "reason", exc))[:120]) from None


async def http_snapshot(ctx: Ctx, target: Target) -> dict[str, Any]:
    async def load() -> dict[str, Any]:
        address, config = await _config(ctx, target)
        if "http_port" not in config:
            raise ProbeError("no_http")
        host = f"[{address}]" if ":" in address else address
        scheme, path = config.get("scheme", "http"), config.get("http_path", "/")
        url = f"{scheme}://{host}:{config['http_port']}{path}"
        status, ms = await asyncio.to_thread(
            _get, url, target.timeout_s, config.get("verify_tls", False) is True
        )
        return {"http_status": status, "http_ms": ms}

    return await HTTP_CACHE.get(_key("http", target), load)


ARGS = {"kind": True, "service": False}


def _tcp_latency(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    return float(s["tcp_ms"]), f"tcp/{s['port']} connected in {fmt(float(s['tcp_ms']))} ms"


def _http_latency(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    return float(s["http_ms"]), f"HTTP {s['http_status']} in {fmt(float(s['http_ms']))} ms"


def _http_status(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    return float(s["http_status"]), f"HTTP status {s['http_status']}"


TCP_PROBES = [
    Probe(
        "service_tcp.latency_ms",
        "service_tcp",
        "ms",
        "Time to open a TCP connection to the service's port",
        None,
        ARGS,
        _tcp_latency,
    )
]
HTTP_PROBES = [
    Probe(
        "service_http.latency_ms",
        "service_http",
        "ms",
        "Time to answer the service's health URL",
        None,
        ARGS,
        _http_latency,
    ),
    Probe(
        "service_http.status",
        "service_http",
        "",
        "HTTP status of the service's health URL",
        None,
        ARGS,
        _http_status,
    ),
]
