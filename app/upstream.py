import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import anyio
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


@dataclass
class StreamStats:
    """What the relay saw, for usage logging. Filled in as events pass through."""

    first_token_at: float | None = None
    content_chunks: int = 0
    content_bytes: int = 0
    usage: dict[str, Any] | None = None
    # "completed", "upstream_error" or "upstream_timeout". Still None if the client went away.
    outcome: str | None = None


def _inspect_event(event: list[str], stats: StreamStats) -> bool:
    """Update stats from one SSE event. Returns True if it is the usage-only chunk."""
    for line in event:
        if not line.startswith("data:") or line == "data: [DONE]":
            continue
        try:
            chunk = json.loads(line[len("data:"):])
        except ValueError:
            return False
        if not isinstance(chunk, dict):
            return False
        if isinstance(chunk.get("usage"), dict):
            stats.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if choices and isinstance(choices[0], dict):
            content = (choices[0].get("delta") or {}).get("content")
            if content:
                if stats.first_token_at is None:
                    stats.first_token_at = time.monotonic()
                stats.content_chunks += 1
                stats.content_bytes += len(content.encode())
        return chunk.get("choices") == [] and chunk.get("usage") is not None
    return False


async def relay_stream(
    response: httpx.Response,
    *,
    client_wants_usage: bool,
    first_token_timeout: float,
    chunk_timeout: float,
    total_timeout: float,
    stats: StreamStats | None = None,
) -> AsyncIterator[bytes]:
    """Relay upstream SSE events to the client, one complete event at a time.

    Events are passed through as the upstream wrote them; they are only parsed to collect
    stats and to find the usage-only chunk. Any failure after streaming has started is sent as
    a final SSE error event, because the 200 status has already gone out.
    """
    stats = stats if stats is not None else StreamStats()
    deadline = time.monotonic() + total_timeout
    wait = first_token_timeout
    lines = response.aiter_lines()
    event: list[str] = []

    def forward(event: list[str]) -> bytes | None:
        is_usage_only = _inspect_event(event, stats)
        if client_wants_usage or not is_usage_only:
            return ("\n".join(event) + "\n\n").encode()
        return None

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stats.outcome = "upstream_timeout"
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
                stats.outcome = "upstream_timeout"
                yield _sse_error("The model server stopped responding", "upstream_timeout")
                return
            wait = chunk_timeout

            if line:
                event.append(line)
                continue
            # A blank line ends an SSE event.
            if event and (data := forward(event)):
                yield data
            event = []

        if event and (data := forward(event)):
            yield data
        stats.outcome = "completed"
    except httpx.RequestError:
        stats.outcome = "upstream_error"
        yield _sse_error("Lost connection to the model server", "upstream_error")
    finally:
        # Runs on success, error AND client disconnect (the server cancels this generator).
        # Closing the upstream response tells the model server to stop generating.
        # Shielded, because after a cancellation every unshielded await is cancelled too (see Slot.release).
        with anyio.CancelScope(shield=True):
            await response.aclose()
