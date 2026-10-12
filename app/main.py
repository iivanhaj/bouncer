import ssl
from contextlib import asynccontextmanager
from functools import lru_cache

import certifi
import httpx
import redis.asyncio as redis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.errors import register_error_handlers
from app.ratelimit import RateLimiter
from app.routes import admin, chat


@lru_cache
def _ssl_context() -> ssl.SSLContext:
    # Loading the certificate store takes ~0.5s on Windows, so build the context once per process.
    return ssl.create_default_context(cafile=certifi.where())


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory. Run with: uvicorn app.main:create_app --factory

    A factory (instead of a module-level `app`) means importing this module never reads
    settings, so the "refuse to start without a strong ADMIN_TOKEN" check only runs on startup.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One shared client for the whole app, so connections to the upstream are pooled and reused.
        app.state.http_client = httpx.AsyncClient(
            base_url=settings.upstream_base_url,
            verify=_ssl_context(),
            timeout=httpx.Timeout(
                connect=settings.upstream_connect_timeout,
                read=settings.upstream_read_timeout,
                write=10.0,
                pool=5.0,
            ),
        )
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        redis_timeout = settings.redis_timeout_ms / 1000
        redis_client = redis.from_url(
            settings.redis_url, socket_timeout=redis_timeout, socket_connect_timeout=redis_timeout
        )
        app.state.rate_limiter = RateLimiter(redis_client, settings.rate_limit_window_seconds)
        yield
        await app.state.http_client.aclose()
        await engine.dispose()
        await redis_client.aclose()

    app = FastAPI(title="Bouncer", lifespan=lifespan)
    app.state.settings = settings

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Liveness only: never checks dependencies (see DESIGN.md §8).
        return {"status": "ok"}

    register_error_handlers(app)
    app.include_router(chat.router)
    app.include_router(admin.router)

    return app
