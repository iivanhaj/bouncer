from typing import Any

from fastapi import APIRouter, Request

from app import upstream
from app.errors import GatewayError
from app.schemas import ChatCompletionRequest

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings

    if body.stream:
        raise GatewayError(501, "Streaming is not supported yet", "invalid_request_error", "stream_not_supported")

    # Later steps slot in here: auth, rate limit, injection check, redaction, concurrency, budget.
    payload = body.to_upstream_payload(settings.max_tokens_cap)
    return await upstream.create_chat_completion(request.app.state.http_client, payload)
