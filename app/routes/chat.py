from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask, BackgroundTasks

from app import upstream
from app.auth import require_api_key
from app.models import ApiKey
from app.ratelimit import Admission, enforce_rate_limit
from app.schemas import ChatCompletionRequest

router = APIRouter()

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Stops nginx (e.g. the Kubernetes ingress) from buffering the stream, which would
    # silently turn streaming back into "wait for the whole answer".
    "X-Accel-Buffering": "no",
}


@router.get("/v1/models")
async def list_models(request: Request, api_key: ApiKey = Depends(require_api_key)) -> dict[str, Any]:
    """The models this gateway routes to, in OpenAI's format."""
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": "bouncer"}
            for name in request.app.state.models.models
        ],
    }


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    response: Response,
    # FastAPI runs dependencies before validating the body, so the order is:
    # auth -> rate limit -> body validation (DESIGN.md pipeline steps 2-4).
    admission: Admission = Depends(enforce_rate_limit),
) -> dict[str, Any] | StreamingResponse:
    settings = request.app.state.settings
    headers = {"x-request-id": str(admission.request_id), **admission.rate_limit.headers()}

    # The allowlist: unknown models are rejected before any upstream is contacted.
    route = request.app.state.models.resolve(body.model)
    client = request.app.state.upstream_clients[route.upstream]

    # Later steps slot in here: injection check, redaction.
    payload = body.to_upstream_payload(settings.max_tokens_cap)
    payload["model"] = route.upstream_model

    # Taken as late as possible, so slots are only held by requests about to reach the model.
    # From here on, every exit path must release it.
    slot = await request.app.state.concurrency.acquire(
        admission.api_key.id, admission.api_key.max_concurrent, admission.request_id
    )
    # Later step slots in here: budget check (must come AFTER the slot, see DESIGN.md pipeline).

    if not body.stream:
        try:
            response.headers.update(headers)
            return await upstream.create_chat_completion(client, payload)
        finally:
            await slot.release()

    try:
        upstream_response = await upstream.open_chat_stream(client, payload, settings.stream_first_token_timeout)
    except BaseException:
        await slot.release()
        raise

    async def relay_then_release():
        try:
            async for chunk in upstream.relay_stream(
                upstream_response,
                client_wants_usage=body.client_wants_usage,
                first_token_timeout=settings.stream_first_token_timeout,
                chunk_timeout=settings.stream_chunk_timeout,
                total_timeout=settings.stream_total_timeout,
            ):
                yield chunk
        finally:
            await slot.release()

    return StreamingResponse(
        relay_then_release(),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, **headers},
        # Safety net: if the client disconnects before the relay even starts, its finally blocks
        # never run. Both cleanups are safe to run twice.
        background=BackgroundTasks([BackgroundTask(upstream_response.aclose), BackgroundTask(slot.release)]),
    )
