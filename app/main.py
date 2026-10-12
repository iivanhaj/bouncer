from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # One shared client for the whole app, so connections to the upstream are pooled and reused.
        app.state.http_client = httpx.AsyncClient(
            base_url=settings.upstream_base_url,
            timeout=httpx.Timeout(
                connect=settings.upstream_connect_timeout,
                read=settings.upstream_read_timeout,
                write=10.0,
                pool=5.0,
            ),
        )
        yield
        await app.state.http_client.aclose()

    app = FastAPI(title="Bouncer", lifespan=lifespan)
    app.state.settings = settings

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Liveness only: never checks dependencies (see DESIGN.md §8).
        return {"status": "ok"}

    # TODO(step 1): include your chat router here, e.g.
    #   from app.routes import chat
    #   app.include_router(chat.router)

    return app


app = create_app()
