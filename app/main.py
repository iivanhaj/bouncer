import asyncio
import ssl
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

import certifi
import httpx
import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import start_http_server
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import metrics
from app.body_limit import BodySizeLimitMiddleware
from app.budget import BudgetEnforcer
from app.concurrency import ConcurrencyLimiter
from app.config import Settings, get_settings
from app.errors import register_error_handlers
from app.injection import InjectionDetector
from app.injection_model import InjectionClassifier
from app.models_registry import ModelRegistry
from app.ratelimit import RateLimiter
from app.routes import admin, admin_usage, chat
from app.usage import UsageRecorder


@lru_cache
def _ssl_context() -> ssl.SSLContext:
    # Loading the certificate store takes ~0.5s on Windows, so build the context once per process.
    return ssl.create_default_context(cafile=certifi.where())


_READY_CHECK_TIMEOUT = 1.0


async def _check_postgres(app: FastAPI) -> bool:
    try:
        async with asyncio.timeout(_READY_CHECK_TIMEOUT), app.state.engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        metrics.DEPENDENCY_ERRORS.labels("postgres").inc()
        return False


async def _check_redis(app: FastAPI) -> bool:
    try:
        async with asyncio.timeout(_READY_CHECK_TIMEOUT):
            return bool(await app.state.redis.ping())
    except Exception:
        metrics.DEPENDENCY_ERRORS.labels("redis").inc()
        return False


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory. Run with: uvicorn app.main:create_app --factory

    A factory (instead of a module-level `app`) means importing this module never reads
    settings, so the "refuse to start without a strong ADMIN_TOKEN" check only runs on startup.
    """
    settings = settings or get_settings()
    # Loaded before startup so a broken models.yaml stops the gateway immediately.
    registry = ModelRegistry.load(settings.models_file, set(settings.upstreams))
    # Also loaded before startup: an invalid rules file must stop the gateway, not weaken it.
    classifier = InjectionClassifier.load() if settings.injection_classifier else None
    injection = InjectionDetector(threshold=settings.injection_threshold, classifier=classifier)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One shared client per upstream for the whole app, so connections are pooled and reused.
        app.state.upstream_clients = {
            name: httpx.AsyncClient(
                base_url=base_url,
                verify=_ssl_context(),
                timeout=httpx.Timeout(
                    connect=settings.upstream_connect_timeout,
                    read=settings.upstream_read_timeout,
                    write=10.0,
                    pool=5.0,
                ),
            )
            for name, base_url in settings.upstreams.items()
        }
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        redis_timeout = settings.redis_timeout_ms / 1000
        redis_client = redis.from_url(
            settings.redis_url, socket_timeout=redis_timeout, socket_connect_timeout=redis_timeout
        )
        app.state.rate_limiter = RateLimiter(redis_client, settings.rate_limit_window_seconds)
        app.state.concurrency = ConcurrencyLimiter(redis_client, settings.concurrency_stale_seconds)
        app.state.budget = BudgetEnforcer(redis_client, app.state.sessionmaker, settings)
        app.state.usage = UsageRecorder(
            app.state.sessionmaker,
            max_queue=settings.usage_queue_size,
            batch_size=settings.usage_batch_size,
            flush_interval=settings.usage_flush_interval_seconds,
        )
        app.state.usage.start()
        app.state.engine, app.state.redis = engine, redis_client
        # Metrics on their own port, never the API port (DESIGN.md §8).
        metrics_server = start_http_server(settings.metrics_port) if settings.metrics_port else None
        yield
        if metrics_server:
            metrics_server[0].shutdown()
        # Flush queued usage rows before closing the database connections they need.
        await app.state.usage.stop()
        for client in app.state.upstream_clients.values():
            await client.aclose()
        await engine.dispose()
        await redis_client.aclose()

    app = FastAPI(title="Bouncer", lifespan=lifespan)
    app.state.settings = settings
    app.state.models = registry
    app.state.injection = injection

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Liveness only: never checks dependencies (see DESIGN.md §8).
        return {"status": "ok"}

    @app.get("/readyz", response_model=None)
    async def readyz() -> dict[str, Any] | JSONResponse:
        """Readiness: can this instance serve traffic right now?

        Unlike /healthz, this checks Postgres and Redis. Kubernetes stops routing traffic to a pod
        that isn't ready but does NOT restart it, so a Redis outage doesn't turn into every
        gateway pod restarting over and over (DESIGN.md §8).
        """
        checks = {"postgres": await _check_postgres(app), "redis": await _check_redis(app)}
        if all(checks.values()):
            return {"status": "ready", "checks": checks}
        return JSONResponse({"status": "not ready", "checks": checks}, status_code=503)

    register_error_handlers(app)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.include_router(chat.router)
    app.include_router(admin.router)
    app.include_router(admin_usage.router)

    return app
