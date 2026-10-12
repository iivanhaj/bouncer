import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.errors import GatewayError

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

# Headers are never copied from the incoming request: httpx builds every upstream request from
# scratch, so the client's Authorization header (their bnc_ key) can never leak upstream.


def _transport_error(exc: httpx.RequestError) -> GatewayError:
    """Map a network-level failure to a gateway error. Order matters: ConnectTimeout is a TimeoutException."""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return GatewayError(502, "The model server is unreachable", "upstream_error", "upstream_unavailable")
    if isinstance(exc, httpx.TimeoutException):
        return GatewayError(504, "The model server timed out", "upstream_error", "upstream_timeout")
    return GatewayError(502, "Error talking to the model server", "upstream_error", "upstream_error")


def _status_error(status_code: int, content: bytes) -> GatewayError:
    """Map an upstream error status. 5xx is our problem (502); 4xx is passed through with its message."""
    if status_code >= 500:
        return GatewayError(502, "The model server returned an error", "upstream_error", "upstream_error")
    try:
        error = json.loads(content).get("error")
    except (ValueError, AttributeError):
        error = None
    message = error.get("message") if isinstance(error, dict) else str(error or "Upstream rejected the request")
    return GatewayError(status_code, message, "invalid_request_error", "upstream_rejected")


async def create_chat_completion(client: httpx.AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    """Send a non-streaming chat completion upstream and return its JSON body."""
    try:
        response = await client.post(CHAT_COMPLETIONS_PATH, json=payload)
    except httpx.RequestError as exc:
        raise _transport_error(exc)

    if response.status_code >= 400:
        raise _status_error(response.status_code, response.content)

    try:
        return response.json()
    except ValueError:
        raise GatewayError(502, "The model server returned an invalid response", "upstream_error", "upstream_error")


async def open_chat_stream(
    client: httpx.AsyncClient, payload: dict[str, Any], first_token_timeout: float
) -> httpx.Response:
    """Start a streaming request and check its status BEFORE the gateway sends anything to the client.

    Once a 200 has been sent to the client the status can't change, so upstream errors that
    happen at the start must be turned into real HTTP errors here.
    """
    request = client.build_request(
        "POST",
        CHAT_COMPLETIONS_PATH,
        json=payload,
        timeout=httpx.Timeout(client.timeout.connect, read=first_token_timeout, write=10.0, pool=5.0),
    )
    try:
        response = await client.send(request, stream=True)
    except httpx.RequestError as exc:
        raise _transport_error(exc)

    if response.status_code >= 400:
        content = await response.aread()
        await response.aclose()
        raise _status_error(response.status_code, content)

    return response


def _sse_error(message: str, code: str) -> bytes:
    body = {"error": {"message": message, "type": "upstream_error", "code": code}}
    return f"data: {json.dumps(body)}\n\n".encode()


def _is_usage_only_event(event: list[str]) -> bool:
    """True for the extra chunk carrying only token usage (sent when include_usage is on)."""
    for line in event:
        if line.startswith("data:") and '"usage"' in line:
            try:
                chunk = json.loads(line[len("data:"):])
            except ValueError:
                return False
            return chunk.get("choices") == [] and chunk.get("usage") is not None
    return False


async def relay_stream(
    response: httpx.Response,
    *,
    client_wants_usage: bool,
    first_token_timeout: float,
    chunk_timeout: float,
    total_timeout: float,
) -> AsyncIterator[bytes]:
    """Relay upstream SSE events to the client, one complete event at a time.

    Events are passed through as the upstream wrote them; lines are only inspected to find
    the usage-only chunk. Any failure after streaming has started is sent as a final SSE error
    event, because the 200 status has already gone out.
    """
    deadline = time.monotonic() + total_timeout
    wait = first_token_timeout
    lines = response.aiter_lines()
    event: list[str] = []
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                yield _sse_error("The response took too long", "upstream_timeout")
                return
            # Only the read is wrapped in a timeout, never the yield: a timeout around a yield
            # would cancel whatever the caller is doing at that moment.
            try:
                async with asyncio.timeout(min(wait, remaining)):
                    line = await anext(lines)
            except StopAsyncIteration:
                break
            except TimeoutError:
                yield _sse_error("The model server stopped responding", "upstream_timeout")
                return
            wait = chunk_timeout

            if line:
                event.append(line)
                continue
            # A blank line ends an SSE event.
            if event and (client_wants_usage or not _is_usage_only_event(event)):
                yield ("\n".join(event) + "\n\n").encode()
            event = []

        if event and (client_wants_usage or not _is_usage_only_event(event)):
            yield ("\n".join(event) + "\n\n").encode()
    except httpx.RequestError:
        yield _sse_error("Lost connection to the model server", "upstream_error")
    finally:
        # Runs on success, error AND client disconnect (the server cancels this generator).
        # Closing the upstream response tells the model server to stop generating.
        await response.aclose()
