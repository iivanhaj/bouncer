from typing import Any

import httpx

from app.errors import GatewayError

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"


async def create_chat_completion(client: httpx.AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    """Send a non-streaming chat completion upstream and return its JSON body.

    Headers are never copied from the incoming request: httpx builds this request from scratch,
    so the client's Authorization header (their bnc_ key) can never leak upstream.
    """
    try:
        response = await client.post(CHAT_COMPLETIONS_PATH, json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # Couldn't reach the upstream at all.
        raise GatewayError(502, "The model server is unreachable", "upstream_error", "upstream_unavailable")
    except httpx.TimeoutException:
        # Connected, but the upstream took too long to answer.
        raise GatewayError(504, "The model server timed out", "upstream_error", "upstream_timeout")
    except httpx.RequestError:
        raise GatewayError(502, "Error talking to the model server", "upstream_error", "upstream_error")

    if response.status_code >= 500:
        raise GatewayError(502, "The model server returned an error", "upstream_error", "upstream_error")

    try:
        body = response.json()
    except ValueError:
        raise GatewayError(502, "The model server returned an invalid response", "upstream_error", "upstream_error")

    if response.status_code >= 400:
        # Client-side problems (e.g. unknown model) are passed through so the client sees the real reason.
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else str(error or "Upstream rejected the request")
        raise GatewayError(response.status_code, message, "invalid_request_error", "upstream_rejected")

    return body
