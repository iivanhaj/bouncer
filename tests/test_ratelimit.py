import asyncio
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
import redis.asyncio as redis
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.ratelimit import RateLimiter
from tests.helpers import TEST_REDIS_URL, UPSTREAM_URL, create_key, make_settings

COMPLETION = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}}]}
CHAT = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


@asynccontextmanager
async def limiter(window_seconds: float = 60.0):
    client = redis.from_url(TEST_REDIS_URL)
    try:
        yield RateLimiter(client, window_seconds)
    finally:
        await client.aclose()


# --- The limiter itself -------------------------------------------------------


@pytest.mark.anyio
async def test_allows_up_to_the_limit_then_rejects():
    key = uuid.uuid4()
    async with limiter() as rl:
        results = [await rl.hit(key, 3, uuid.uuid4()) for _ in range(4)]

    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]
    assert results[-1].retry_after_seconds >= 1


@pytest.mark.anyio
async def test_concurrent_requests_cannot_exceed_the_limit():
    """The atomicity test: 50 requests at once against a limit of 10.
    If "count" and "add" were separate steps, several requests would see the same count and slip through."""
    key = uuid.uuid4()
    async with limiter() as rl:
        results = await asyncio.gather(*(rl.hit(key, 10, uuid.uuid4()) for _ in range(50)))

    assert sum(r.allowed for r in results) == 10


@pytest.mark.anyio
async def test_window_slides():
    key = uuid.uuid4()
    async with limiter(window_seconds=0.3) as rl:
        assert (await rl.hit(key, 2, uuid.uuid4())).allowed
        assert (await rl.hit(key, 2, uuid.uuid4())).allowed
        assert not (await rl.hit(key, 2, uuid.uuid4())).allowed
        await asyncio.sleep(0.35)
        assert (await rl.hit(key, 2, uuid.uuid4())).allowed


@pytest.mark.anyio
async def test_rejected_requests_do_not_extend_the_lockout():
    key = uuid.uuid4()
    async with limiter(window_seconds=0.4) as rl:
        assert (await rl.hit(key, 1, uuid.uuid4())).allowed  # t=0
        await asyncio.sleep(0.25)
        for _ in range(5):  # t=0.25: hammering while limited
            assert not (await rl.hit(key, 1, uuid.uuid4())).allowed
        await asyncio.sleep(0.2)  # t=0.45: the first request has slid out
        # If the rejected attempts at t=0.25 had been recorded, this would be blocked until t=0.65.
        assert (await rl.hit(key, 1, uuid.uuid4())).allowed


@pytest.mark.anyio
async def test_keys_are_limited_independently():
    key_a, key_b = uuid.uuid4(), uuid.uuid4()
    async with limiter() as rl:
        assert (await rl.hit(key_a, 1, uuid.uuid4())).allowed
        assert not (await rl.hit(key_a, 1, uuid.uuid4())).allowed
        assert (await rl.hit(key_b, 1, uuid.uuid4())).allowed


# --- Through the HTTP API -----------------------------------------------------


@pytest.fixture
def upstream():
    with respx.mock(assert_all_called=False) as mock:
        yield mock.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))


@pytest.fixture
def client():
    with TestClient(create_app(make_settings())) as test_client:
        test_client.headers["Authorization"] = f"Bearer {create_key(test_client, rpm_limit=2)['key']}"
        yield test_client


def test_successful_response_carries_rate_limit_and_request_id_headers(client, upstream):
    response = client.post("/v1/chat/completions", json=CHAT)

    assert response.status_code == 200
    assert response.headers["x-ratelimit-limit-requests"] == "2"
    assert response.headers["x-ratelimit-remaining-requests"] == "1"
    assert uuid.UUID(response.headers["x-request-id"])


def test_streaming_response_carries_rate_limit_headers(client, upstream):
    upstream.mock(return_value=httpx.Response(200, text="data: [DONE]\n\n"))

    response = client.post("/v1/chat/completions", json={**CHAT, "stream": True})

    assert response.headers["x-ratelimit-remaining-requests"] == "1"
    assert "x-request-id" in response.headers


def test_over_the_limit_returns_openai_style_429(client, upstream):
    client.post("/v1/chat/completions", json=CHAT)
    client.post("/v1/chat/completions", json=CHAT)
    upstream.reset()

    response = client.post("/v1/chat/completions", json=CHAT)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(response.headers["Retry-After"]) >= 1
    assert response.headers["x-ratelimit-remaining-requests"] == "0"
    assert not upstream.called


def test_rate_limit_runs_before_body_validation(client, upstream):
    client.post("/v1/chat/completions", json=CHAT)
    client.post("/v1/chat/completions", json=CHAT)

    # An invalid body from a rate-limited key gets 429, not 400: the cheap check comes first.
    response = client.post("/v1/chat/completions", json={"model": "m", "messages": []})

    assert response.status_code == 429


def test_redis_down_fails_closed_and_fast(upstream):
    # Nothing listens on this port.
    settings = make_settings(redis_url="redis://localhost:6390/0")
    with TestClient(create_app(settings)) as client:
        key = create_key(client)["key"]
        start = time.monotonic()
        response = client.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key}"})
        elapsed = time.monotonic() - start

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dependency_unavailable"
    assert not upstream.called
    assert elapsed < 1.0, f"took {elapsed:.2f}s: a down Redis must not hang requests"
