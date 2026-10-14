import time
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app import metrics, tokens, upstream
from app.auth import require_api_key
from app.concurrency import Slot
from app.errors import GatewayError
from app.models import ApiKey
from app.ratelimit import Admission, enforce_rate_limit
from app.redaction import Redactor
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
            {"id": name, "object": "model", "created": 0, "owned_by": "modelguard"}
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
    await request.app.state.budget.settle(record.key_id, record.total_tokens, record.started_at)
    if slot is not None:
        await slot.release()
    request.app.state.usage.record(record)
    _observe(record)


def _observe(record: UsageRecord) -> None:
    """Prometheus metrics for one finished request. Labels only use gateway-defined values."""
    model = metrics.model_label(record.model, known=record.route is not None)
    metrics.IN_FLIGHT.dec()
    metrics.REQUESTS.labels(record.status, model).inc()
    metrics.REQUEST_DURATION.labels(model, str(record.stream).lower()).observe(time.monotonic() - record.started)
    if record.ttft_ms is not None:
        metrics.TIME_TO_FIRST_TOKEN.labels(model).observe(record.ttft_ms / 1000)
    metrics.TOKENS.labels("prompt", model).inc(record.prompt_tokens)
    metrics.TOKENS.labels("completion", model).inc(record.completion_tokens)
    if record.status == "blocked_injection":
        for rule in record.blocked_rules or []:
            metrics.BLOCKED.labels(rule).inc()
    for category, count in (record.redaction_counts or {}).items():
        metrics.REDACTIONS.labels(category).inc(count)


def _usage_from_completion(record: UsageRecord, body: ChatCompletionRequest, completion: dict[str, Any]) -> None:
    usage = completion.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("prompt_tokens"), int):
        record.set_usage(usage["prompt_tokens"], usage.get("completion_tokens") or 0, estimated=False)
        return
    choices = completion.get("choices") or [{}]
    text = (choices[0].get("message") or {}).get("content") or ""
    record.set_usage(tokens.estimate_prompt_tokens(body.messages), tokens.estimate_text_tokens(text), estimated=True)


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
    record = UsageRecord(id=admission.request_id, key_id=admission.api_key.id, model=body.model, stream=body.stream)
    # Decremented in finish(), which every request past this point reaches exactly once.
    metrics.IN_FLIGHT.inc()
    slot: Slot | None = None
    streaming = False

    try:
        # The allowlist: unknown models are rejected before any upstream is contacted.
        route = state.models.resolve(body.model)
        record.upstream, record.route = route.upstream, route
        client = state.upstream_clients[route.upstream]

        # Injection check on the ORIGINAL text, before redaction (DESIGN.md §4): blocked requests
        # skip the redaction work, and the detector sees exactly what the attacker wrote.
        # Matched rules are logged even below the threshold: that's the data for tuning rules.
        verdict = state.injection.scan(body.messages)
        if verdict.rules:
            record.blocked_rules, record.injection_score = verdict.rules, verdict.score
        if verdict.blocked and settings.injection_mode == "block":
            # Generic on purpose: naming the matched rule would help an attacker iterate.
            raise GatewayError(
                403,
                f"This request was blocked by the gateway's security policy. Reference: {admission.request_id}",
                "invalid_request_error",
                "prompt_blocked",
            )

        # Redact personal data and secrets before anything leaves the gateway. Only the counts
        # per category are kept; the values and the placeholder mapping are never stored.
        redactor = Redactor()
        outgoing = body.model_copy(update={"messages": redactor.redact_messages(body.messages)})
        record.redaction_counts = redactor.counts or None

        payload = outgoing.to_upstream_payload(settings.max_tokens_cap)
        payload["model"] = route.upstream_model

        # Taken as late as possible, so slots are only held by requests about to reach the model.
        slot = await state.concurrency.acquire(
            admission.api_key.id, admission.api_key.max_concurrent, admission.request_id
        )
        # AFTER the slot (DESIGN.md pipeline): the headroom only covers requests that hold one.
        await state.budget.check(
            admission.api_key.id,
            admission.api_key.monthly_token_budget,
            admission.api_key.max_concurrent,
            record.started_at,
        )

        if not body.stream:
            completion = await upstream.create_chat_completion(client, payload)
            _usage_from_completion(record, body, completion)
            response.headers.update(headers)
            return completion

        upstream_response = await upstream.open_chat_stream(client, payload, settings.stream_first_token_timeout)
        streaming = True
    except GatewayError as error:
        record.status = status_for(error)
        # Errors carry the request id too, so a failed request can be matched to its log row.
        error.headers = {**headers, **(error.headers or {})}
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
