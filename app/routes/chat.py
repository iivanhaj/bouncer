from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app import upstream
from app.ratelimit import Admission, enforce_rate_limit
from app.schemas import ChatCompletionRequest

router = APIRouter()

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Stops nginx (e.g. the Kubernetes ingress) from buffering the stream, which would
    # silently turn streaming back into "wait for the whole answer".
    "X-Accel-Buffering": "no",
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
    client = request.app.state.http_client
    headers = {"x-request-id": str(admission.request_id), **admission.rate_limit.headers()}

    # Later steps slot in here: injection check, redaction, concurrency, budget.
    payload = body.to_upstream_payload(settings.max_tokens_cap)

    if not body.stream:
        response.headers.update(headers)
        return await upstream.create_chat_completion(client, payload)

    upstream_response = await upstream.open_chat_stream(client, payload, settings.stream_first_token_timeout)
    return StreamingResponse(
        upstream.relay_stream(
            upstream_response,
            client_wants_usage=body.client_wants_usage,
            first_token_timeout=settings.stream_first_token_timeout,
            chunk_timeout=settings.stream_chunk_timeout,
            total_timeout=settings.stream_total_timeout,
        ),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, **headers},
        # Safety net: if the client disconnects before the relay even starts, its finally block
        # never runs, so close the upstream response here too. Closing twice is harmless.
        background=BackgroundTask(upstream_response.aclose),
    )
