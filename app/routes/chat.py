from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app import tokens, upstream
from app.auth import require_api_key
from app.concurrency import Slot
from app.errors import GatewayError
from app.models import ApiKey
from app.ratelimit import Admission, enforce_rate_limit
from app.schemas import ChatCompletionRequest
from app.usage import UsageRecord, status_for

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


async def finish(request: Request, record: UsageRecord, slot: Slot | None) -> None:
    """The single exit point for every request past the rate limit. Safe to call twice.

    Order matters (DESIGN.md pipeline step 10): the budget counter is updated before the slot
    is released, so the next request's budget check already sees these tokens.
    """
    if record.finished:
        return
    record.finished = True
    # Later step slots in here: add tokens to the budget counter.
    if slot is not None:
        await slot.release()
    request.app.state.usage.record(record)


def _usage_from_completion(record: UsageRecord, body: ChatCompletionRequest, completion: dict[str, Any]) -> None:
    usage = completion.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int):
        record.set_usage(usage["prompt_tokens"], usage.get("completion_tokens") or 0, estimated=False)
        return
    choices = completion.get("choices") or [{}]
    text = (choices[0].get("message") or {}).get("content") or ""
    record.set_usage(
        tokens.estimate_prompt_tokens(body.messages), tokens.estimate_text_tokens(text), estimated=True
    )


def _usage_from_stream(record: UsageRecord, body: ChatCompletionRequest, stats: upstream.StreamStats) -> None:
    if stats.first_token_at is not None:
        record.mark_first_token(stats.first_token_at)
    if stats.usage and isinstance(stats.usage.get("prompt_tokens"), int):
        record.set_usage(stats.usage["prompt_tokens"], stats.usage.get("completion_tokens") or 0, estimated=False)
    else:
        # Ollama sends one token per chunk, so the chunk count is the completion estimate.
        record.set_usage(tokens.estimate_prompt_tokens(body.messages), stats.content_chunks, estimated=True)
    # No outcome means the relay never finished: the client went away mid-stream.
    record.status = {"completed": "ok", None: "client_disconnected"}.get(stats.outcome, stats.outcome)


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    response: Response,
    # FastAPI runs dependencies before validating the body, so the order is:
    # auth -> rate limit -> body validation (DESIGN.md pipeline steps 2-4).
    admission: Admission = Depends(enforce_rate_limit),
) -> dict[str, Any] | StreamingResponse:
    state = request.app.state
    settings = state.settings
    headers = {"x-request-id": str(admission.request_id), **admission.rate_limit.headers()}
    record = UsageRecord(id=admission.request_id, key_id=admission.api_key.id, model=body.model)
    slot: Slot | None = None
    streaming = False

    try:
        # The allowlist: unknown models are rejected before any upstream is contacted.
        route = state.models.resolve(body.model)
        record.upstream, record.route = route.upstream, route
        client = state.upstream_clients[route.upstream]

        # Later steps slot in here: injection check, redaction.
        payload = body.to_upstream_payload(settings.max_tokens_cap)
        payload["model"] = route.upstream_model

        # Taken as late as possible, so slots are only held by requests about to reach the model.
        slot = await state.concurrency.acquire(
            admission.api_key.id, admission.api_key.max_concurrent, admission.request_id
        )
        # Later step slots in here: budget check (must come AFTER the slot, see DESIGN.md pipeline).

        if not body.stream:
            completion = await upstream.create_chat_completion(client, payload)
            _usage_from_completion(record, body, completion)
            response.headers.update(headers)
            return completion

        upstream_response = await upstream.open_chat_stream(client, payload, settings.stream_first_token_timeout)
        streaming = True
    except GatewayError as error:
        record.status = status_for(error)
        raise
    except BaseException:
        record.status = "error"
        raise
    finally:
        # Streams finish when the stream ends, in relay_then_finish below.
        if not streaming:
            await finish(request, record, slot)

    stats = upstream.StreamStats()

    async def relay_then_finish():
        try:
            async for chunk in upstream.relay_stream(
                upstream_response,
                client_wants_usage=body.client_wants_usage,
                first_token_timeout=settings.stream_first_token_timeout,
                chunk_timeout=settings.stream_chunk_timeout,
                total_timeout=settings.stream_total_timeout,
                stats=stats,
            ):
                yield chunk
        finally:
            _usage_from_stream(record, body, stats)
            await finish(request, record, slot)

    async def cleanup_if_never_started() -> None:
        # Safety net: if the client disconnects before the relay even starts, its finally blocks
        # never run. Both cleanups are safe to run twice.
        await upstream_response.aclose()
        _usage_from_stream(record, body, stats)
        await finish(request, record, slot)

    return StreamingResponse(
        relay_then_finish(),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, **headers},
        background=BackgroundTask(cleanup_if_never_started),
    )
