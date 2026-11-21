"""/api/v1/ai: providers, keys, models, routes (fallback chains), health and usage.

Everything here needs `ai.manage`, except the list of choices a user can pick from in Operate
(`/ai/llm/choices`, needs `ai.use` or `ai.operate`). Keys go in and never come out: only the last
four characters are shown.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from janus_ops.admin.common import record
from janus_ops.ai import config_store
from janus_ops.ai.gateway import Gateway
from janus_ops.ai.llm import AIError
from janus_ops.ai.llm_router import LLMRouter
from janus_ops.ai.providers import PRESETS, ProviderError, ProviderService
from janus_ops.auth.deps import current_session, require_permission
from janus_ops.auth.sessions import Session

router = APIRouter(tags=["ai-providers"])
Manager = Annotated[Session, Depends(require_permission("ai.manage"))]


class ProviderIn(BaseModel):
    type: str
    slug: str
    base_url: str | None = None
    config: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None
    connect_timeout: float | None = None
    first_token_timeout: float | None = None
    total_timeout: float | None = None
    verify_tls: bool | str | None = None
    enabled: bool = True
    budget_tokens_daily: int | None = None
    budget_tokens_monthly: int | None = None
    api_key: str | None = Field(default=None, repr=False)
    key_label: str = ""


class ProviderPatch(BaseModel):
    slug: str | None = None
    base_url: str | None = None
    config: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    extra_body: dict[str, Any] | None = None
    connect_timeout: float | None = None
    first_token_timeout: float | None = None
    total_timeout: float | None = None
    verify_tls: bool | str | None = None
    enabled: bool | None = None
    budget_tokens_daily: int | None = None
    budget_tokens_monthly: int | None = None


class KeyIn(BaseModel):
    value: str = Field(repr=False)
    label: str | None = None


class ModelIn(BaseModel):
    model_id: str
    kind: str | None = None
    context_window: int | None = None
    price_in: float | None = None
    price_out: float | None = None
    first_token_timeout: float | None = None


class RouteIn(BaseModel):
    entries: list[dict[str, Any]] = Field(max_length=12)


def _services(request: Request) -> tuple[ProviderService, LLMRouter]:
    app = request.app.state
    providers = ProviderService(app.engine, app.vault)
    return providers, LLMRouter(app.engine, app.vault, providers)


def _bad(exc: ProviderError) -> HTTPException:
    return HTTPException(422, f"invalid_provider:{exc}")


def _fields(body: BaseModel, *names: str) -> dict[str, Any]:
    """The fields the caller actually sent (None values included: they clear a limit)."""
    given = body.model_fields_set
    return {n: getattr(body, n) for n in names if n in given}


PROVIDER_FIELDS = (
    "base_url", "config", "headers", "extra_body", "connect_timeout", "first_token_timeout",
    "total_timeout", "verify_tls", "enabled", "budget_tokens_daily", "budget_tokens_monthly",
)  # fmt: skip


@router.get("/providers/presets")
async def presets(_: Manager) -> list[dict[str, Any]]:
    return [
        {
            "type": p.type, "label": p.label, "base_url": p.base_url, "key": p.key,
            "notes": p.notes, "config_fields": list(p.config_fields), "paid": p.paid,
            "headers": p.headers,
        }
        for p in PRESETS.values()
    ]  # fmt: skip


@router.get("/providers")
async def list_providers(request: Request, _: Manager) -> list[dict[str, Any]]:
    providers, _router = _services(request)
    return [await providers.public(p) for p in await providers.list_all()]


@router.post("/providers", status_code=201, responses={422: {}, 502: {}})
async def create_provider(body: ProviderIn, request: Request, session: Manager) -> dict[str, Any]:
    providers, _router = _services(request)
    data = {"type": body.type, "slug": body.slug, **_fields(body, *PROVIDER_FIELDS)}
    data = {k: v for k, v in data.items() if v is not None or k.startswith("budget")}
    try:
        preview = await providers.preview(data)
        # the connection and the key are tried before anything is saved
        listed: list[dict[str, Any]] = []
        if preview.type != "custom" or body.api_key:
            listed = await providers.fetch_models(preview, body.api_key or None)
        created = await providers.create(data, body.api_key, body.key_label, session.username)
        if listed:
            await providers.upsert_listed(created.id, listed)
    except ProviderError as exc:
        raise _bad(exc) from None
    await record(
        request, session, "ai.provider.created", "ai_provider", created.slug,
        {"type": created.type, "key": bool(body.api_key), "models": len(listed)},
    )  # fmt: skip
    return await providers.public(created)


@router.put("/providers/{provider_id}", responses={404: {}, 422: {}})
async def update_provider(
    provider_id: str, body: ProviderPatch, request: Request, session: Manager
) -> dict[str, Any]:
    providers, _router = _services(request)
    data = _fields(body, "slug", *PROVIDER_FIELDS)
    try:
        updated = await providers.update(provider_id, data)
    except ProviderError as exc:
        if "not found" in str(exc):
            raise HTTPException(404, "not_found") from None
        raise _bad(exc) from None
    await record(
        request, session, "ai.provider.changed", "ai_provider", updated.slug,
        {"fields": sorted(data)},
    )  # fmt: skip
    return await providers.public(updated)


@router.delete("/providers/{provider_id}", status_code=204, responses={404: {}})
async def delete_provider(provider_id: str, request: Request, session: Manager) -> Response:
    providers, _router = _services(request)
    try:
        provider = await providers.get(provider_id)
        await providers.delete(provider_id)
    except ProviderError:
        raise HTTPException(404, "not_found") from None
    await record(request, session, "ai.provider.deleted", "ai_provider", provider.slug, {})
    return Response(status_code=204)


@router.post("/providers/{provider_id}/test", responses={404: {}, 422: {}})
async def test_provider(provider_id: str, request: Request, _: Manager) -> dict[str, Any]:
    providers, _router = _services(request)
    try:
        provider = await providers.get(provider_id)
        listed = await providers.fetch_models(provider)
    except ProviderError as exc:
        if "not found" in str(exc):
            raise HTTPException(404, "not_found") from None
        raise _bad(exc) from None
    return {"ok": True, "models": len(listed)}


# ------------------------------------------------------------------------------------- keys
@router.post("/providers/{provider_id}/keys", status_code=201, responses={404: {}, 422: {}})
async def add_key(
    provider_id: str, body: KeyIn, request: Request, session: Manager
) -> dict[str, str]:
    providers, _router = _services(request)
    try:
        provider = await providers.get(provider_id)
        key_id = await providers.add_key(provider_id, body.value, body.label or "")
    except ProviderError as exc:
        if "not found" in str(exc):
            raise HTTPException(404, "not_found") from None
        raise _bad(exc) from None
    await record(request, session, "ai.provider.key.added", "ai_provider", provider.slug, {})
    return {"id": key_id}


@router.put("/providers/{provider_id}/keys/{key_id}", status_code=204, responses={404: {}, 422: {}})
async def replace_key(
    provider_id: str, key_id: str, body: KeyIn, request: Request, session: Manager
) -> Response:
    providers, _router = _services(request)
    try:
        provider = await providers.get(provider_id)
        await providers.replace_key(key_id, body.value, body.label)
    except ProviderError as exc:
        if "not found" in str(exc):
            raise HTTPException(404, "not_found") from None
        raise _bad(exc) from None
    await record(request, session, "ai.provider.key.replaced", "ai_provider", provider.slug, {})
    return Response(status_code=204)


@router.delete("/providers/{provider_id}/keys/{key_id}", status_code=204, responses={404: {}})
async def delete_key(provider_id: str, key_id: str, request: Request, session: Manager) -> Response:
    providers, _router = _services(request)
    try:
        provider = await providers.get(provider_id)
        await providers.delete_key(provider_id, key_id)
    except ProviderError:
        raise HTTPException(404, "not_found") from None
    await record(request, session, "ai.provider.key.deleted", "ai_provider", provider.slug, {})
    return Response(status_code=204)


# ------------------------------------------------------------------------------------- models
@router.post("/providers/{provider_id}/models/refresh", responses={404: {}, 422: {}})
async def refresh_models(provider_id: str, request: Request, _: Manager) -> list[dict[str, Any]]:
    providers, _router = _services(request)
    try:
        return await providers.refresh_models(provider_id)
    except ProviderError as exc:
        if "not found" in str(exc):
            raise HTTPException(404, "not_found") from None
        raise _bad(exc) from None


@router.put("/providers/{provider_id}/models", responses={404: {}, 422: {}})
async def save_models(
    provider_id: str, body: list[ModelIn], request: Request, _: Manager
) -> list[dict[str, Any]]:
    providers, _router = _services(request)
    try:
        return await providers.save_models(
            provider_id, [m.model_dump(exclude_unset=True) for m in body]
        )
    except ProviderError as exc:
        if "not found" in str(exc):
            raise HTTPException(404, "not_found") from None
        raise _bad(exc) from None


@router.delete("/providers/{provider_id}/models/{model_id:path}", status_code=204)
async def delete_model(provider_id: str, model_id: str, request: Request, _: Manager) -> Response:
    providers, _router = _services(request)
    await providers.delete_model(provider_id, model_id)
    return Response(status_code=204)


@router.post("/providers/{provider_id}/models/{model_id:path}/test", responses={404: {}, 422: {}})
async def test_model(
    provider_id: str, model_id: str, request: Request, session: Manager
) -> dict[str, Any]:
    providers, _router = _services(request)
    try:
        result = await providers.test_model(provider_id, model_id)
    except ProviderError as exc:
        if "not found" in str(exc):
            raise HTTPException(404, "not_found") from None
        raise _bad(exc) from None
    return result


# ------------------------------------------------------------------------------------- routes
@router.get("/routes")
async def list_routes(request: Request, _: Manager) -> list[dict[str, Any]]:
    _providers, llm_router = _services(request)
    return await llm_router.routes()


@router.put("/routes/{name}", status_code=204, responses={422: {}})
async def put_route(name: str, body: RouteIn, request: Request, session: Manager) -> Response:
    _providers, llm_router = _services(request)
    try:
        await llm_router.set_route(name, body.entries)
    except ProviderError as exc:
        raise _bad(exc) from None
    await record(
        request, session, "ai.route.changed", "ai_route", name,
        {"entries": [f"{e.get('provider')}/{e.get('model')}" for e in body.entries]},
    )  # fmt: skip
    return Response(status_code=204)


@router.delete("/routes/{name}", status_code=204, responses={404: {}, 422: {}})
async def delete_route(name: str, request: Request, session: Manager) -> Response:
    _providers, llm_router = _services(request)
    try:
        await llm_router.delete_route(name)
    except ProviderError as exc:
        if "not found" in str(exc):
            raise HTTPException(404, "not_found") from None
        raise _bad(exc) from None
    await record(request, session, "ai.route.deleted", "ai_route", name, {})
    return Response(status_code=204)


@router.post("/routes/{name}/test", responses={404: {}, 422: {}, 502: {}})
async def test_route(name: str, request: Request, _: Manager) -> dict[str, Any]:
    """One tiny request through the route: which model answered, and what it had to skip."""
    app = request.app.state
    settings = await config_store.effective(app.engine, app.settings, app.vault)
    gateway = Gateway(app.engine, app.vault, settings)
    try:
        await gateway.ask(name, [{"role": "user", "content": "Reply with the single word: ok"}],
                          source="route-test")  # fmt: skip
    except AIError as exc:
        raise HTTPException(502, exc.code) from None
    return {"ok": True, "model": gateway.used[-1] if gateway.used else None,
            "notices": gateway.notices}  # fmt: skip


# ------------------------------------------------------------------------------------- health
@router.get("/llm/health")
async def health(request: Request, _: Manager) -> list[dict[str, Any]]:
    _providers, llm_router = _services(request)
    return await llm_router.health()


@router.get("/llm/usage")
async def usage(request: Request, _: Manager, days: int = 7) -> list[dict[str, Any]]:
    _providers, llm_router = _services(request)
    return await llm_router.usage(max(1, min(days, 90)))


@router.get("/llm/choices")
async def choices(request: Request, session: Session = Depends(current_session)) -> dict[str, Any]:  # noqa: B008
    """What a user can pick in Operate: the routes with models, and each tested model."""
    if not ({"ai.use", "ai.operate", "ai.manage"} & session.permissions):
        raise HTTPException(403, "forbidden")
    providers, llm_router = _services(request)
    routes = [r for r in await llm_router.routes() if r["entries"]]
    models: list[dict[str, Any]] = []
    for p in await providers.list_all():
        if not p.enabled:
            continue
        for m in await providers.models(p.id):
            if m["kind"] == "chat" and m["test_ok"] and m["available"]:
                models.append(
                    {"provider": p.slug, "model": m["model_id"], "tools": m["tools"],
                     "spec": f"{p.slug}/{m['model_id']}"}
                )  # fmt: skip
    return {"configured": bool(routes), "routes": routes, "models": models}
