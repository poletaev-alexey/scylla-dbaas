import importlib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from dbaas.api.routes import router
from dbaas.config import Settings, get_settings
from dbaas.db import Database
from dbaas.operations.handlers import HANDLERS
from dbaas.operations.health import HealthChecker
from dbaas.operations.runner import OperationRunner
from dbaas.providers.base import Provider

REQUESTS = Counter("dbaas_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("dbaas_http_request_seconds", "HTTP latency", ["method", "route"])


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    )


def build_provider(settings: Settings) -> Provider:
    if settings.provider == "fake":
        from dbaas.providers.fake import FakeWorld, fake_provider

        return fake_provider(FakeWorld(step_delay=1.0))[0]
    # Real drivers are imported lazily so the fake path needs no cloud/docker deps.
    module = importlib.import_module(f"dbaas.providers.{settings.provider}")
    p: Provider = module.build(settings)
    return p


def create_app(
    settings: Settings | None = None,
    provider: Provider | None = None,
    start_health_checker: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings.database_url)
        await db.create_all()
        prov = provider or build_provider(settings)
        runner = OperationRunner(db.sessions, prov, HANDLERS)
        await runner.recover_after_restart()
        health = HealthChecker(
            db.sessions, runner, settings.health_interval_seconds, settings.recovery_grace_seconds
        )
        app.state.settings, app.state.db, app.state.runner = settings, db, runner
        app.state.health = health
        if start_health_checker:
            health.start()
        yield
        await health.stop()
        await runner.shutdown()
        await db.dispose()

    app = FastAPI(
        title="Scylla DBaaS",
        version="0.1.0",
        lifespan=lifespan,
        description="Control Plane API for managed ScyllaDB Clusters on AWS.",
    )
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        structlog.contextvars.bind_contextvars(request_id=rid)
        route = request.scope.get("route")
        with LATENCY.labels(request.method, getattr(route, "path", "other")).time():
            response = await call_next(request)
        route = request.scope.get("route")
        REQUESTS.labels(request.method, getattr(route, "path", "other"), response.status_code).inc()
        response.headers["x-request-id"] = rid
        structlog.contextvars.clear_contextvars()
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(router)
    return app


app = create_app()
