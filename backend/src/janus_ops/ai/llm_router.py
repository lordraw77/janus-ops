"""Routes with a fallback chain (ported from the AIFRED gateway).

A route is an ordered list of (provider, model). A request tries the first entry that can be used
and moves to the next when it fails. What a failure means is decided by its classification
(`llm_http.Outcome`): a bad key is switched off, a quota or rate limit puts the key or the entry on
hold for a while, a missing model is marked unavailable, an outage backs the entry off, and a
request the provider rejects is tried on the next entry without punishing this one. Health is kept
in the database, so it survives a restart and is shared by every route using the same model.
"""

import asyncio
import calendar
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops.ai.llm_http import LLMError, Outcome, Reply, post_chat
from janus_ops.ai.providers import Provider, ProviderError, ProviderService, endpoint_for
from janus_ops.vault import Vault, VaultUnavailable

log = logging.getLogger(__name__)

PROTECTED_ROUTES = ("default", "review")
MAX_ROUTE_ENTRIES = 12
SHORT_RATE_LIMIT = 5.0
UNAVAILABLE_BASE = 30.0
UNAVAILABLE_MAX = 600.0
TIMEOUT_COOLDOWN = 120.0
AUTH_ENTRY_COOLDOWN = 600.0
QUOTA_COOLDOWN = 3600.0
ROUTE_DEADLINE = 240.0

REASONS = {
    Outcome.QUOTA: "quota exhausted",
    Outcome.RATE_LIMIT: "rate limited",
    Outcome.UNAVAILABLE: "unavailable",
    Outcome.TIMEOUT: "timeout",
    Outcome.AUTH: "invalid key",
    Outcome.MODEL_MISSING: "model missing",
    Outcome.CONTEXT: "context too long",
    Outcome.FATAL: "request rejected",
}


class RouteError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


class NoProvider(RouteError):
    def __init__(self) -> None:
        super().__init__("ai_not_configured", "no route has a model yet")


class RouteExhausted(RouteError):
    def __init__(self, failures: list[tuple[str, str]]) -> None:
        self.failures = failures
        detail = "; ".join(f"{label}: {reason}" for label, reason in failures) or "none available"
        super().__init__("ai_route_exhausted", f"every model in the route failed ({detail})")


@dataclass(frozen=True)
class Entry:
    provider: Provider
    model: str
    key_id: str | None = None
    context_window: int | None = None
    available: bool = True
    first_token: float | None = None

    @property
    def ref(self) -> str:
        return f"{self.provider.id}|{self.model}"

    @property
    def label(self) -> str:
        return f"{self.provider.slug}/{self.model}"


@dataclass
class Completed:
    reply: Reply
    entry: Entry
    notices: list[str] = field(default_factory=list)


@dataclass
class _Status:
    keys: list[str | None]
    blocked_until: datetime | None = None
    reason: str = ""
    never: bool = False  # disabled for good (until someone fixes it)


@dataclass
class _Walk:
    route: str
    source: str
    primary: Entry
    failures: list[tuple[str, str]]
    attempts: int = 0
    min_context: int | None = None
    context_failed: bool = False
    first_fatal: LLMError | None = None


def _utc(value: datetime | None) -> datetime:
    return (value or datetime.fromtimestamp(0, UTC)).astimezone(UTC)


def _day_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    days = calendar.monthrange(now.year, now.month)[1]
    return start, start + timedelta(days=days)


class LLMRouter:
    def __init__(
        self,
        engine: AsyncEngine,
        vault: Vault,
        providers: ProviderService,
        transport: Any = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._engine = engine
        self._vault = vault
        self._providers = providers
        self._transport = transport
        self._sleep = sleep

    # ------------------------------------------------------------------ resolving
    async def configured(self) -> bool:
        async with self._engine.connect() as conn:
            return bool(await conn.scalar(text("SELECT 1 FROM ai_route_entry LIMIT 1")))

    async def route_entries(self, name: str) -> list[Entry]:
        async with self._engine.connect() as conn:
            if not await conn.scalar(text("SELECT 1 FROM ai_route WHERE name = :n"), {"n": name}):
                raise RouteError("route_not_found", name)
            rows = (
                await conn.execute(
                    text(
                        "SELECT e.provider_id, e.model_id, e.key_id, m.context_window, m.available,"
                        " m.first_token_timeout FROM ai_route_entry e JOIN ai_provider_model m"
                        " ON m.provider_id = e.provider_id AND m.model_id = e.model_id"
                        " WHERE e.route = :n ORDER BY e.position"
                    ),
                    {"n": name},
                )
            ).all()
        providers = {p.id: p for p in await self._providers.list_all()}
        return [
            Entry(
                providers[str(r.provider_id)], r.model_id, str(r.key_id) if r.key_id else None,
                r.context_window, r.available, r.first_token_timeout,
            )
            for r in rows
            if str(r.provider_id) in providers
        ]  # fmt: skip

    async def resolve(self, spec: str | None) -> list[Entry]:
        """`spec`: a route name, `provider/model` (pinned) or empty (the default route)."""
        spec = (spec or "").strip() or "default"
        if "/" in spec:
            slug, _, model = spec.partition("/")
            provider = await self._providers.by_slug(slug)
            if provider is None or not model:
                raise RouteError("route_not_found", spec)
            async with self._engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT context_window, available, first_token_timeout"
                            " FROM ai_provider_model WHERE provider_id = CAST(:p AS uuid)"
                            " AND model_id = :m"
                        ),
                        {"p": provider.id, "m": model},
                    )
                ).first()
            return [
                Entry(
                    provider, model, None, row.context_window if row else None,
                    row.available if row else True, row.first_token_timeout if row else None,
                )
            ]  # fmt: skip
        entries = await self.route_entries(spec)
        if not entries and spec != "default":
            entries = await self.route_entries("default")
        if not entries:
            raise NoProvider
        return entries

    # ------------------------------------------------------------------ status
    async def _plan(
        self, entries: list[Entry]
    ) -> tuple[list[tuple[Entry, _Status]], list[tuple[str, str]]]:
        now = datetime.now(UTC)
        provider_ids = sorted({e.provider.id for e in entries})
        async with self._engine.connect() as conn:
            key_rows = (
                await conn.execute(
                    text(
                        "SELECT id, provider_id FROM ai_provider_key"
                        " WHERE provider_id = ANY(CAST(:p AS uuid[])) ORDER BY created_at"
                    ),
                    {"p": provider_ids},
                )
            ).all()
            health = {
                (r.scope, r.ref): r
                for r in await conn.execute(
                    text("SELECT scope, ref, state, until, reason FROM ai_health")
                )
            }
            used: dict[str, int] = {}
            if any(e.provider.budget_daily or e.provider.budget_monthly for e in entries):
                month_start, _ = _month_bounds(now)
                for r in await conn.execute(
                    text(
                        "SELECT provider_id, sum(CASE WHEN ts >= :d THEN tokens_in + tokens_out"
                        " ELSE 0 END) AS day, sum(tokens_in + tokens_out) AS month FROM ai_usage"
                        " WHERE ts >= :m AND provider_id = ANY(CAST(:p AS uuid[]))"
                        " GROUP BY provider_id"
                    ),
                    {"d": _day_start(now), "m": month_start, "p": provider_ids},
                ):
                    used[f"{r.provider_id}|day"] = int(r.day or 0)
                    used[f"{r.provider_id}|month"] = int(r.month or 0)
        keys: dict[str, list[str]] = {}
        for r in key_rows:
            keys.setdefault(str(r.provider_id), []).append(str(r.id))

        def status(entry: Entry, ignore_cooldown: bool = False) -> _Status:
            p = entry.provider
            if not p.enabled:
                return _Status([], None, "provider disabled", never=True)
            if not entry.available:
                return _Status([], None, "model missing", never=True)
            h = health.get(("entry", entry.ref))
            if h and h.state == "disabled":
                return _Status([], None, h.reason or "disabled", never=True)
            if not ignore_cooldown:
                if h and h.until and _utc(h.until) > now:
                    return _Status([], _utc(h.until), h.reason or "cooling down")
                if p.budget_daily and used.get(f"{p.id}|day", 0) >= p.budget_daily:
                    return _Status(
                        [], _day_start(now) + timedelta(days=1), "daily token budget reached"
                    )
                if p.budget_monthly and used.get(f"{p.id}|month", 0) >= p.budget_monthly:
                    return _Status([], _month_bounds(now)[1], "monthly token budget reached")
            ids = keys.get(p.id, [])
            if entry.key_id is not None:
                ids = [i for i in ids if i == entry.key_id]
                if not ids:
                    return _Status([], None, "pinned key missing", never=True)
            if not ids:
                return _Status([None])  # a provider that needs no key
            usable: list[str | None] = []
            soonest: datetime | None = None
            reason = "all keys unavailable"
            for key_id in ids:
                kh = health.get(("key", key_id))
                if kh and kh.state == "disabled":
                    reason = "invalid key"
                    continue
                if not ignore_cooldown and kh and kh.until and _utc(kh.until) > now:
                    until = _utc(kh.until)
                    if soonest is None or (until is not None and until < soonest):
                        soonest, reason = until, kh.reason or reason
                    continue
                usable.append(key_id)
            if usable:
                return _Status(usable)
            return _Status([], soonest, reason, never=soonest is None)

        statuses = [(e, status(e)) for e in entries]
        usable_entries = [(e, s) for e, s in statuses if s.keys]
        skipped = [(e.provider.slug, s.reason) for e, s in statuses if not s.keys]
        if usable_entries:
            return usable_entries, skipped
        recoverable = [(e, s) for e, s in statuses if s.blocked_until is not None]
        if recoverable:
            entry, _ = min(recoverable, key=lambda p: p[1].blocked_until or now)
            forced = status(entry, ignore_cooldown=True)
            if forced.keys:  # everything is cooling down: try the one closest to recovery
                return [(entry, forced)], [
                    (e.provider.slug, s.reason) for e, s in statuses if e is not entry
                ]
        return [], skipped

    # ------------------------------------------------------------------ health and usage
    @staticmethod
    def _cooldown(outcome: Outcome, exc: LLMError, failures: int) -> float:
        if outcome is Outcome.QUOTA:
            return float(exc.retry_after or QUOTA_COOLDOWN)
        if outcome is Outcome.RATE_LIMIT:
            return float(exc.retry_after or UNAVAILABLE_BASE)
        if outcome is Outcome.TIMEOUT:
            return TIMEOUT_COOLDOWN
        if outcome is Outcome.AUTH:
            return AUTH_ENTRY_COOLDOWN
        return float(min(UNAVAILABLE_BASE * 2 ** max(0, failures - 1), UNAVAILABLE_MAX))

    async def _health_set(
        self, scope: str, ref: str, state: str, seconds: float | None, reason: str, detail: str
    ) -> None:
        until = datetime.now(UTC) + timedelta(seconds=seconds) if seconds is not None else None
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ai_health (scope, ref, state, until, failures, reason, detail)"
                    " VALUES (:s, :r, :st, :u, 1, :re, :d) ON CONFLICT (scope, ref) DO UPDATE SET"
                    " state = :st, until = :u, failures = ai_health.failures + 1, reason = :re,"
                    " detail = :d, at = now()"
                ),
                {"s": scope, "r": ref, "st": state, "u": until, "re": reason, "d": detail[:300]},
            )

    async def _failures(self, scope: str, ref: str) -> int:
        async with self._engine.connect() as conn:
            return int(
                await conn.scalar(
                    text("SELECT failures FROM ai_health WHERE scope = :s AND ref = :r"),
                    {"s": scope, "r": ref},
                )
                or 0
            )

    async def _mark_entry(self, entry: Entry, exc: LLMError) -> None:
        failures = await self._failures("entry", entry.ref) + 1
        seconds = self._cooldown(exc.outcome, exc, failures)
        await self._health_set(
            "entry", entry.ref, "cooldown", seconds, REASONS[exc.outcome], str(exc)
        )
        log.warning("%s: %s (on hold for %d s)", entry.label, REASONS[exc.outcome], seconds)

    async def _mark_key(self, key_id: str, exc: LLMError) -> None:
        if exc.outcome is Outcome.AUTH:
            await self._health_set("key", key_id, "disabled", None, "invalid key", str(exc))
            log.error("an AI provider key was rejected and is now switched off")
            return
        failures = await self._failures("key", key_id) + 1
        seconds = self._cooldown(exc.outcome, exc, failures)
        await self._health_set("key", key_id, "cooldown", seconds, REASONS[exc.outcome], str(exc))

    async def _mark_success(self, entry: Entry, key_id: str | None) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM ai_health WHERE scope = 'entry' AND ref = :r"), {"r": entry.ref}
            )
            if key_id:
                await conn.execute(
                    text(
                        "DELETE FROM ai_health WHERE scope = 'key' AND ref = :r"
                        " AND state = 'cooldown'"
                    ),
                    {"r": key_id},
                )

    async def _record(
        self,
        walk: "_Walk",
        entry: Entry,
        outcome: str,
        latency_ms: int,
        reply: Reply | None,
        error: str = "",
    ) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ai_usage (provider_id, provider, model, route, source, tokens_in,"
                    " tokens_out, latency_ms, outcome, error, fallback) VALUES"
                    " (CAST(:pid AS uuid), :p, :m, :r, :s, :ti, :to, :l, :o, :e, :f)"
                ),
                {
                    "pid": entry.provider.id,
                    "p": entry.provider.slug,
                    "m": entry.model,
                    "r": walk.route,
                    "s": walk.source,
                    "ti": reply.tokens_in if reply else 0,
                    "to": reply.tokens_out if reply else 0,
                    "l": latency_ms,
                    "o": outcome,
                    "e": error[:300],
                    "f": entry.ref != walk.primary.ref,
                },
            )

    async def _model_unavailable(self, entry: Entry) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE ai_provider_model SET available = false WHERE provider_id ="
                    " CAST(:p AS uuid) AND model_id = :m"
                ),
                {"p": entry.provider.id, "m": entry.model},
            )

    # ------------------------------------------------------------------ the walk
    async def _try_entry(
        self,
        entry: Entry,
        status: _Status,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        walk: "_Walk",
    ) -> Completed | None:
        """Try one entry with each of its usable keys. None = give up on this entry."""
        for key_id in status.keys:
            retried = False
            while True:
                walk.attempts += 1
                began = time.monotonic()
                try:
                    key = await self._providers.key_value(key_id) if key_id else None
                    reply = await post_chat(
                        endpoint_for(entry.provider, entry.model, key, entry.first_token),
                        messages,
                        tools,
                        transport=self._transport,
                    )
                except VaultUnavailable:
                    # a sealed vault says nothing about the key: no punishment, stop here
                    raise RouteError("vault_unavailable") from None
                except LLMError as exc:
                    latency = int((time.monotonic() - began) * 1000)
                    await self._record(walk, entry, exc.outcome.value, latency, None, str(exc))
                    reason = REASONS[exc.outcome]
                    wait = exc.retry_after
                    short = wait is not None and wait <= SHORT_RATE_LIMIT
                    if exc.outcome is Outcome.RATE_LIMIT and not retried and short:
                        retried = True
                        await self._sleep(wait or 0.0)
                        continue
                    walk.failures.append((entry.provider.slug, reason))
                    if exc.outcome is Outcome.FATAL:
                        # may be this provider's alone (a format it does not take): next entry,
                        # no punishment; the first rejection is raised if every entry says so
                        walk.first_fatal = walk.first_fatal or exc
                        return None
                    if exc.outcome is Outcome.CONTEXT:
                        walk.context_failed = True
                        walk.min_context = entry.context_window or 0
                        return None
                    if exc.outcome is Outcome.MODEL_MISSING:
                        await self._model_unavailable(entry)
                        return None
                    if exc.outcome in (Outcome.QUOTA, Outcome.AUTH) and key_id is not None:
                        await self._mark_key(key_id, exc)
                        break  # the next key of this entry
                    await self._mark_entry(entry, exc)
                    return None
                latency = int((time.monotonic() - began) * 1000)
                await self._record(walk, entry, "success", latency, reply)
                await self._mark_success(entry, key_id)
                done = Completed(reply, entry)
                if entry.ref != walk.primary.ref:
                    why = "; ".join(dict.fromkeys(f"{s}: {r}" for s, r in walk.failures))
                    done.notices.append(f"Switched to {entry.label}" + (f" ({why})" if why else ""))
                return done
        return None

    async def complete(
        self,
        entries: list[Entry],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        route: str = "default",
        source: str = "",
    ) -> Completed:
        """Walk the route. Raises `NoProvider`, `RouteExhausted`, or `LLMError` for a request that
        every entry rejected the same way (or a conversation too long for all of them)."""
        if not entries:
            raise NoProvider
        plan, skipped = await self._plan(entries)
        walk = _Walk(route, source, entries[0], list(skipped))
        deadline = time.monotonic() + ROUTE_DEADLINE
        for entry, status in plan:
            if walk.attempts and time.monotonic() >= deadline:
                walk.failures.append(("route", "time budget exhausted"))
                break
            # an entry known to hold no more than the one that was too small is not worth a try
            if (
                walk.min_context
                and entry.context_window
                and entry.context_window <= walk.min_context
            ):
                continue
            done = await self._try_entry(entry, status, messages, tools, walk)
            if done is not None:
                return done
        real = walk.failures[len(skipped) :]
        if walk.context_failed and real and all(r == "context too long" for _, r in real):
            raise LLMError(
                "the conversation is too long for every model in the route", Outcome.CONTEXT
            )
        if walk.first_fatal and real and all(r == "request rejected" for _, r in real):
            raise walk.first_fatal
        raise RouteExhausted(walk.failures)

    # ------------------------------------------------------------------ management
    async def set_route(self, name: str, entries: list[dict[str, Any]]) -> None:
        import re

        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
            raise ProviderError(
                "route names use lowercase letters, digits, - and _ (1-32 characters)"
            )
        if len(entries) > MAX_ROUTE_ENTRIES:
            raise ProviderError(f"a route has at most {MAX_ROUTE_ENTRIES} entries")
        rows: list[tuple[str, str, str | None]] = []
        for i, e in enumerate(entries, start=1):
            provider = await self._providers.by_slug(str(e.get("provider", "")))
            if provider is None:
                raise ProviderError(f"entry {i}: unknown provider {e.get('provider')!r}")
            model_id = str(e.get("model", "")).strip()
            models = {m["model_id"]: m for m in await self._providers.models(provider.id)}
            model = models.get(model_id)
            if model is None:
                raise ProviderError(
                    f"entry {i}: {provider.slug}/{model_id} is not in the provider's model list"
                )
            if model["kind"] != "chat":
                raise ProviderError(f"entry {i}: {provider.slug}/{model_id} is not a chat model")
            if not model["test_ok"]:
                raise ProviderError(
                    f"entry {i}: {provider.slug}/{model_id} has not passed the test;"
                    " press Test on the model first"
                )
            key_id = e.get("key_id")
            pinned = [k["id"] for k in await self._providers.keys(provider.id)]
            if key_id and key_id not in pinned:
                raise ProviderError(f"entry {i}: the pinned key does not belong to {provider.slug}")
            rows.append((provider.id, model_id, key_id or None))
        async with self._engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO ai_route (name) VALUES (:n) ON CONFLICT DO NOTHING"), {"n": name}
            )
            await conn.execute(text("DELETE FROM ai_route_entry WHERE route = :n"), {"n": name})
            for position, (provider_id, model_id, key_id) in enumerate(rows):
                await conn.execute(
                    text(
                        "INSERT INTO ai_route_entry (route, position, provider_id, model_id,"
                        " key_id) VALUES (:n, :pos, CAST(:p AS uuid), :m, CAST(:k AS uuid))"
                    ),
                    {"n": name, "pos": position, "p": provider_id, "m": model_id, "k": key_id},
                )

    async def delete_route(self, name: str) -> None:
        if name in PROTECTED_ROUTES:
            raise ProviderError(f"the {name!r} route cannot be deleted")
        async with self._engine.begin() as conn:
            gone = await conn.scalar(
                text("DELETE FROM ai_route WHERE name = :n RETURNING 1"), {"n": name}
            )
        if not gone:
            raise ProviderError("route not found")

    async def routes(self) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            names = [
                r.name for r in await conn.execute(text("SELECT name FROM ai_route ORDER BY name"))
            ]
            rows = (
                await conn.execute(
                    text(
                        "SELECT e.route, e.position, p.slug, e.model_id, e.key_id"
                        " FROM ai_route_entry e JOIN ai_provider p ON p.id = e.provider_id"
                        " ORDER BY e.route, e.position"
                    )
                )
            ).all()
        out = {n: {"name": n, "protected": n in PROTECTED_ROUTES, "entries": []} for n in names}
        for r in rows:
            out[r.route]["entries"].append(
                {
                    "provider": r.slug,
                    "model": r.model_id,
                    "key_id": str(r.key_id) if r.key_id else None,
                }
            )
        return list(out.values())

    async def health(self) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT p.slug, p.enabled, e.route, e.model_id, h.state, h.until, h.reason"
                        " FROM ai_provider p LEFT JOIN ai_route_entry e ON e.provider_id = p.id"
                        " LEFT JOIN ai_health h ON h.scope = 'entry' AND h.ref = p.id::text || '|'"
                        " || e.model_id ORDER BY p.slug, e.route, e.position"
                    )
                )
            ).all()
        now = datetime.now(UTC)
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            item = out.setdefault(r.slug, {"provider": r.slug, "enabled": r.enabled, "entries": []})
            if r.route:
                state = "ok"
                if r.state == "disabled":
                    state = "disabled"
                elif r.state == "cooldown" and r.until and _utc(r.until) > now:
                    state = "cooling_down"
                item["entries"].append(
                    {
                        "route": r.route,
                        "model": r.model_id,
                        "state": state,
                        "reason": r.reason or "",
                        "until": r.until.isoformat()
                        if state == "cooling_down" and r.until
                        else None,
                    }
                )
        return list(out.values())

    async def usage(self, days: int = 7) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT provider, model, count(*) AS calls,"
                    " count(*) FILTER (WHERE outcome = 'success') AS ok,"
                    " count(*) FILTER (WHERE fallback) AS fallbacks,"
                    " coalesce(sum(tokens_in), 0) AS tokens_in,"
                    " coalesce(sum(tokens_out), 0) AS tokens_out,"
                    " coalesce(avg(latency_ms) FILTER (WHERE outcome = 'success'), 0)::int"
                    " AS latency_ms"
                    " FROM ai_usage WHERE ts > now() - make_interval(days => :d)"
                    " GROUP BY provider, model ORDER BY calls DESC LIMIT 200"
                ),
                {"d": days},
            )
            return [dict(r._mapping) for r in rows]
