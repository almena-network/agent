"""FastAPI application: the home page, the health probe and the A2A interface.

Its lifespan opens the metrics listener when ``AGENT_METRICS_ADDR`` is set:
the metrics are never among this app's routes.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from scalar_fastapi import add_scalar_reference

from almena_agent import __version__
from almena_agent.a2a.routes import add_a2a_routes
from almena_agent.accounts import Accounts, ApiAccounts
from almena_agent.api.routes import health, home, well_known
from almena_agent.config import get_settings
from almena_agent.llm import Model, get_model
from almena_agent.metrics import serve
from almena_agent.registry import McpRegistry, Registry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if not settings.metrics_addr:
        yield
        return
    _, stop = serve(settings.metrics_addr)
    try:
        yield
    finally:
        stop()


def create_app(
    model: Model | None = None,
    registry: Registry | None = None,
    accounts: Accounts | None = None,
) -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.name,
        lifespan=lifespan,
        version=__version__,
        # The reference is Scalar's (below), not Swagger UI or ReDoc.
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        servers=[
            {"url": settings.public_url, "description": "Public"},
            {"url": "/", "description": "This server"},
        ],
    )
    if settings.docs_enabled:
        add_scalar_reference(app, route="/docs")
    app.include_router(home.router)
    app.include_router(health.router)
    app.include_router(well_known.router)
    if registry is None and settings.registry_mcp_url:
        registry = McpRegistry(settings.registry_mcp_url)
    accounts = accounts or ApiAccounts(settings.registry_account_url)
    add_a2a_routes(app, settings, model or get_model(), registry, accounts)
    return app


app = create_app()
