"""FastAPI application factory."""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from janus_ops import __version__, health
from janus_ops.admin import audit_log, roles, users
from janus_ops.agents import gateway, telemetry
from janus_ops.agents import router as agents
from janus_ops.agents.hub import AgentHub
from janus_ops.ai.router import router as ai_copilot
from janus_ops.ansible.router import router as ansible
from janus_ops.auth.ldap import DirectoryBackend, LdapDirectory
from janus_ops.auth.router import router as auth_router
from janus_ops.auth.service import AuthService
from janus_ops.config import Settings, get_settings
from janus_ops.containers import router as containers
from janus_ops.containers.access import DockerAccess
from janus_ops.db import create_engine, schema_head
from janus_ops.directory import LldapAdmin
from janus_ops.gitops.router import router as gitops
from janus_ops.inventory import credentials, environments, groups, nodes
from janus_ops.monitoring import engine as check_engine
from janus_ops.monitoring import fritz_router, notify, snmp_oids, snmp_templates
from janus_ops.monitoring import router as monitoring
from janus_ops.proxmox import router as proxmox
from janus_ops.proxmox.access import ProxmoxAccess
from janus_ops.terminal import router as terminal
from janus_ops.terminal import sftp, snippets
from janus_ops.vault import Vault
from janus_ops.workflows import engine as workflow_engine
from janus_ops.workflows.router import router as workflows

API_PREFIX = "/api/v1"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class _Default:
    """Sentinel: build the directory from settings."""


DEFAULT_DIRECTORY = _Default()


def _same_origin(request: Request) -> bool:
    """CSRF guard for cookie-authenticated writes (complements SameSite=Strict).

    Browsers send Origin on cross-site POSTs; when present (or Referer as fallback) its host
    must equal the Host header. Requests without either come from non-browser clients.
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return source is None
    return urlsplit(source).netloc.lower() == request.headers.get("host", "").lower()


def create_app(
    settings: Settings | None = None,
    *,
    directory: DirectoryBackend | _Default | None = DEFAULT_DIRECTORY,
    extra_routers: Sequence[APIRouter] = (),
) -> FastAPI:
    settings = settings or get_settings()
    if isinstance(directory, _Default):
        directory = LdapDirectory(settings) if settings.ldap_url else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.settings = settings
        app.state.schema_head = schema_head()
        app.state.auth = AuthService(engine, settings, directory)
        app.state.directory_admin = LldapAdmin(settings) if settings.directory_managed else None
        app.state.vault = Vault(app.state.auth.box)
        app.state.terminals = terminal.TerminalCounter()
        app.state.sftp = sftp.SftpPool(engine, app.state.vault, settings)
        app.state.agents = AgentHub()
        app.state.agent_jobs = set()  # nodes with a bootstrap/uninstall in progress
        app.state.gitops_jobs = set()  # GitOps deploys running in the background
        app.state.workflow_jobs = set()  # workflow runs advancing in the background
        # a run interrupted by a restart is marked failed: a person decides whether to rerun it
        # (skipped when the schema is not migrated yet: /ready reports that)
        with contextlib.suppress(Exception):
            await workflow_engine.recover(engine)
        app.state.docker = DockerAccess(engine, app.state.vault, settings, app.state.agents)
        app.state.proxmox = ProxmoxAccess(engine, app.state.vault)
        app.state.check_engine = check_engine.Engine(
            engine, app.state.agents, app.state.proxmox, app.state.vault
        )
        app.state.dispatcher = notify.Dispatcher(engine, app.state.vault)
        tasks = [asyncio.create_task(sftp.reaper(app.state.sftp, engine))]
        if settings.monitoring_enabled:
            tasks.append(asyncio.create_task(app.state.check_engine.run()))
            tasks.append(asyncio.create_task(app.state.dispatcher.run()))
            tasks.append(
                asyncio.create_task(
                    workflow_engine.scheduler(
                        engine, app.state.agents, app.state.workflow_jobs, app.state.vault
                    )
                )
            )
        if settings.recordings_dir is not None:
            settings.recordings_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # a deploy cancelled here records its run as failed (see gitops.deploy.execute)
            for job in list(app.state.gitops_jobs):
                job.cancel()
            if app.state.gitops_jobs:
                await asyncio.gather(*app.state.gitops_jobs, return_exceptions=True)
            app.state.sftp.close_all()
            app.state.docker.close_all()
            await app.state.proxmox.close_all()
            await app.state.agents.close_all()
            await engine.dispose()

    app = FastAPI(
        title="Janus-Ops",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )

    @app.middleware("http")
    async def guards(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if (
            request.method in UNSAFE_METHODS
            and request.url.path.startswith("/api/")
            and not _same_origin(request)
        ):
            response: Response = JSONResponse({"detail": "cross_origin"}, status_code=403)
        else:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(auth_router, prefix=API_PREFIX)
    for module_router in (
        users.router,
        roles.router,
        audit_log.router,
        environments.router,
        nodes.router,
        groups.router,
        credentials.router,
        terminal.router,
        sftp.router,
        snippets.router,
        gateway.router,
        agents.router,
        telemetry.router,
        monitoring.router,
        snmp_oids.router,
        fritz_router.router,
        snmp_templates.router,
        workflows,
        containers.router,
        proxmox.router,
        ansible,
        ai_copilot,
        gitops,
    ):
        app.include_router(module_router, prefix=API_PREFIX)
    for router in extra_routers:
        app.include_router(router, prefix=API_PREFIX)

    # Unknown /api paths must answer JSON 404, never the UI's 404 page
    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def api_not_found(path: str) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    if settings.static_dir is not None:
        _mount_ui(app, settings.static_dir)
    return app


def _mount_ui(app: FastAPI, static_dir: Path) -> None:
    # Next.js static export (trailingSlash: true): /page/ -> /page/index.html, misses -> 404.html
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
