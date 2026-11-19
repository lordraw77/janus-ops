"""Apache httpd probes, from `mod_status` (`/server-status?auto`), fetched by the server.

Apache restricts `server-status` to the local host by default: allow the Janus-Ops server in its
configuration (`Require ip <server address>`). The URL is a parameter of the probe, so it can also
be a proxy in front of it.
"""

import asyncio
import ssl
import urllib.error
import urllib.request
from typing import Any

from janus_ops.monitoring.probes.base import Ctx, Probe, ProbeError, SnapshotCache, Target, fmt

CACHE = SnapshotCache()
MAX_BYTES = 1 << 18


def _fetch(url: str, timeout: float, verify_tls: bool) -> str:
    context = ssl.create_default_context()
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    # http(s) only: the probe argument is checked in ProbeParams
    request = urllib.request.Request(url, headers={"User-Agent": "Janus-Ops probe"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310
            return str(response.read(MAX_BYTES).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # 401/403: the server answers but does not let this client read the status page
        raise ProbeError(
            "denied" if exc.code in (401, 403) else "bad_response", f"HTTP {exc.code}"
        ) from None
    except TimeoutError:
        raise ProbeError("timeout") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProbeError("unreachable", str(getattr(exc, "reason", exc))[:120]) from None


def parse(body: str) -> dict[str, Any]:
    """`Key: value` lines of the machine readable page."""
    found: dict[str, Any] = {}
    for line in body.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        try:
            found[key.strip()] = float(value.strip())
        except ValueError:
            continue
    return found


async def snapshot(ctx: Ctx, target: Target) -> dict[str, Any]:
    url = str(target.args["url"])
    verify = target.args.get("verify_tls", True) is not False

    async def load() -> dict[str, Any]:
        body = await asyncio.to_thread(_fetch, url, target.timeout_s, verify)
        data = parse(body)
        if "BusyWorkers" not in data and "BusyServers" not in data:
            raise ProbeError("bad_response", "not an Apache server-status?auto page")
        return data

    return await CACHE.get(f"httpd:{url}", load)


def _busy(s: dict[str, Any]) -> float:
    return float(s.get("BusyWorkers", s.get("BusyServers", 0)))


def _idle(s: dict[str, Any]) -> float:
    return float(s.get("IdleWorkers", s.get("IdleServers", 0)))


def _busy_workers(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    return _busy(s), f"{fmt(_busy(s))} busy workers"


def _idle_workers(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    return _idle(s), f"{fmt(_idle(s))} idle workers"


def _busy_pct(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
    total = _busy(s) + _idle(s)
    pct = 100.0 * _busy(s) / total if total else 0.0
    return pct, f"{fmt(_busy(s))} of {fmt(total)} workers busy ({pct:.1f}%)"


def _num(key: str, label: str, unit: str) -> Any:
    def extract(s: dict[str, Any], _: dict[str, Any]) -> tuple[float, str]:
        value = float(s.get(key, 0))
        return value, f"{label} {fmt(value)} {unit}".strip()

    return extract


ARGS = {"url": True, "verify_tls": False}


def _p(name: str, unit: str, description: str, extract: Any) -> Probe:
    return Probe(f"httpd.{name}", "httpd", unit, description, None, ARGS, extract)


PROBES = [
    _p("busy_workers", "", "Workers serving a request", _busy_workers),
    _p("idle_workers", "", "Workers waiting for a request", _idle_workers),
    _p("busy_workers_pct", "%", "Busy workers as a share of all workers", _busy_pct),
    _p(
        "requests_per_s",
        "req/s",
        "Requests per second (average since start)",
        _num("ReqPerSec", "requests", "req/s"),
    ),
    _p(
        "bytes_per_s",
        "B/s",
        "Bytes sent per second (average since start)",
        _num("BytesPerSec", "traffic", "B/s"),
    ),
    _p(
        "uptime_s",
        "s",
        "Seconds since the server started",
        _num("ServerUptimeSeconds", "uptime", "s"),
    ),
]
