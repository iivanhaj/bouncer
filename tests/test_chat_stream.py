import asyncio
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import upstream as upstream_module
from app.main import create_app
from tests.helpers import UPSTREAM_URL, create_key, make_settings


def chunk(content: str) -> str:
    body = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": content}}]}
    return f"data: {json.dumps(body)}\n\n"


USAGE_CHUNK = (
    'data: {"object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
)
DONE = "data: [DONE]\n\n"


class FakeStream(httpx.AsyncByteStream):
    """An upstream body that yields pieces with optional delays and remembers whether it was closed."""

    def __init__(self, *pieces: str | float | Exception):
        self.pieces = pieces
        self.closed = False

    async def __aiter__(self):
        for piece in self.pieces:
            if isinstance(piece, float):
                await asyncio.sleep(piece)
            elif isinstance(piece, Exception):
                raise piece
            else:
                yield piece.encode()

    async def aclose(self):
        self.closed = True


def sse_response(stream: FakeStream) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)


def stream_body(**overrides):
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


def events(text: str) -> list[str]:
    return [block for block in text.split("\n\n") if block]


@pytest.fixture
def settings():
    return make_settings(stream_first_token_timeout=1.0, stream_chunk_timeout=0.2, stream_total_timeout=2.0)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        test_client.headers["Authorization"] = f"Bearer {create_key(test_client)['key']}"
        yield test_client


@pytest.fixture
def upstream():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


# --- Happy path -------------------------------------------------------------


def test_relays_events_in_order(client, upstream):
    upstream.post(UPSTREAM_URL).mock(return_value=sse_response(FakeStream(chunk("Hel"), chunk("lo"), DONE)))

    response = client.post("/v1/chat/completions", json=stream_body())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"
    assert events(response.text) == [chunk("Hel").strip(), chunk("lo").strip(), "data: [DONE]"]


def test_event_split_across_network_reads_is_reassembled(client, upstream):
    whole = chunk("Hello")
    upstream.post(UPSTREAM_URL).mock(return_value=sse_response(FakeStream(whole[:10], whole[10:], DONE)))

    response = client.post("/v1/chat/completions", json=stream_body())

    assert events(response.text) == [whole.strip(), "data: [DONE]"]


def test_gateway_always_asks_upstream_for_usage(client, upstream):
    route = upstream.post(UPSTREAM_URL).mock(return_value=sse_response(FakeStream(DONE)))

    client.post("/v1/chat/completions", json=stream_body())

    assert json.loads(route.calls.last.request.content)["stream_options"] == {"include_usage": True}


def test_usage_chunk_is_stripped_when_client_did_not_ask(client, upstream):
    upstream.post(UPSTREAM_URL).mock(return_value=sse_response(FakeStream(chunk("Hi"), USAGE_CHUNK, DONE)))

    response = client.post("/v1/chat/completions", json=stream_body())

    assert '"usage"' not in response.text
    assert events(response.text) == [chunk("Hi").strip(), "data: [DONE]"]


def test_usage_chunk_is_kept_when_client_asked(client, upstream):
    upstream.post(UPSTREAM_URL).mock(return_value=sse_response(FakeStream(chunk("Hi"), USAGE_CHUNK, DONE)))

    response = client.post("/v1/chat/completions", json=stream_body(stream_options={"include_usage": True}))

    assert USAGE_CHUNK.strip() in events(response.text)


# --- Errors before streaming starts: real HTTP status codes -----------------


def test_upstream_4xx_before_stream_is_a_real_http_error(client, upstream):
    upstream.post(UPSTREAM_URL).mock(
        return_value=httpx.Response(404, json={"error": {"message": 'model "m" not found'}})
    )

    response = client.post("/v1/chat/completions", json=stream_body())

    assert response.status_code == 404
    assert response.json()["error"]["message"] == 'model "m" not found'


def test_upstream_unreachable_before_stream_is_502(client, upstream):
    upstream.post(UPSTREAM_URL).mock(side_effect=httpx.ConnectError("refused"))

    response = client.post("/v1/chat/completions", json=stream_body())

    assert response.status_code == 502


# --- Errors after streaming starts: final SSE error event -------------------


def last_event_error(response) -> dict:
    last = events(response.text)[-1]
    return json.loads(last[len("data:") :])["error"]


def test_connection_lost_mid_stream_sends_error_event(client, upstream):
    stream = FakeStream(chunk("Hi"), httpx.ReadError("connection reset"))
    upstream.post(UPSTREAM_URL).mock(return_value=sse_response(stream))

    response = client.post("/v1/chat/completions", json=stream_body())

    assert response.status_code == 200  # already sent before the failure
    assert events(response.text)[0] == chunk("Hi").strip()
    assert last_event_error(response)["code"] == "upstream_error"
    assert stream.closed


def test_stalled_upstream_hits_chunk_timeout(client, upstream):
    stream = FakeStream(chunk("Hi"), 1.0, chunk("never sent"))  # 1s gap > 0.2s chunk timeout
    upstream.post(UPSTREAM_URL).mock(return_value=sse_response(stream))

    response = client.post("/v1/chat/completions", json=stream_body())

    assert "never sent" not in response.text
    assert last_event_error(response)["code"] == "upstream_timeout"
    assert stream.closed


def test_first_token_gets_the_longer_timeout(client, upstream):
    # 0.5s before the first token: over the 0.2s chunk timeout but under the 1s first-token timeout.
    upstream.post(UPSTREAM_URL).mock(return_value=sse_response(FakeStream(0.5, chunk("Hi"), DONE)))

    response = client.post("/v1/chat/completions", json=stream_body())

    assert events(response.text) == [chunk("Hi").strip(), "data: [DONE]"]


def test_total_timeout_stops_a_slow_trickle(settings, upstream):
    settings.stream_total_timeout = 0.5
    # Each gap (0.15s) is under the chunk timeout, but together they exceed the total.
    pieces = [p for i in range(10) for p in (0.15, chunk(str(i)))]
    upstream.post(UPSTREAM_URL).mock(return_value=sse_response(FakeStream(*pieces)))

    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = f"Bearer {create_key(client)['key']}"
        response = client.post("/v1/chat/completions", json=stream_body())

    assert last_event_error(response)["code"] == "upstream_timeout"
    assert len(events(response.text)) < 10


# --- Client disconnect ------------------------------------------------------


@pytest.mark.anyio
async def test_closing_the_relay_closes_the_upstream_response():
    """When a client disconnects, the server stops the relay generator. That must close the
    upstream response, which is what tells the model server to stop generating."""
    stream = FakeStream(chunk("one"), chunk("two"), chunk("three"))
    response = sse_response(stream)
    relay = upstream_module.relay_stream(
        response, client_wants_usage=False, first_token_timeout=1, chunk_timeout=1, total_timeout=5
    )

    first = await anext(relay)
    await relay.aclose()  # what the server does on disconnect

    assert first == chunk("one").encode()
    assert stream.closed
