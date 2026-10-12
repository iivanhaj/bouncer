import asyncio
import time
import uuid
from contextlib import asynccontextmanager

import anyio
import httpx
import pytest
import redis
import redis.asyncio as aredis
import respx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.concurrency import ConcurrencyLimiter, Slot
from app.errors import GatewayError
from app.main import create_app
from tests.helpers import TEST_REDIS_URL, UPSTREAM_URL, create_key, make_settings

COMPLETION = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}}]}
CHAT = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


@asynccontextmanager
async def limiter(stale_seconds: float = 360.0):
    client = aredis.from_url(TEST_REDIS_URL)
    try:
        yield ConcurrencyLimiter(client, stale_seconds)
    finally:
        await client.aclose()


def in_progress(key_id: str) -> int:
    with redis.Redis.from_url(TEST_REDIS_URL) as client:
        return client.zcard(f"inflight:{key_id}")


# --- The limiter itself -------------------------------------------------------


@pytest.mark.anyio
async def test_acquire_up_to_limit_then_429():
    key = uuid.uuid4()
    async with limiter() as cl:
        await cl.acquire(key, 2, uuid.uuid4())
        await cl.acquire(key, 2, uuid.uuid4())
        with pytest.raises(GatewayError) as error:
            await cl.acquire(key, 2, uuid.uuid4())

    assert error.value.status_code == 429
    assert error.value.code == "concurrency_limit_exceeded"


@pytest.mark.anyio
async def test_release_frees_the_slot_and_is_idempotent():
    key = uuid.uuid4()
    async with limiter() as cl:
        slot = await cl.acquire(key, 1, uuid.uuid4())
        await slot.release()
        await slot.release()  # second release must not free someone else's slot
        other = await cl.acquire(key, 1, uuid.uuid4())

        with pytest.raises(GatewayError):
            await cl.acquire(key, 1, uuid.uuid4())
        await other.release()


@pytest.mark.anyio
async def test_concurrent_acquires_cannot_exceed_the_limit():
    key = uuid.uuid4()
    async with limiter() as cl:
        results = await asyncio.gather(*(cl.acquire(key, 4, uuid.uuid4()) for _ in range(50)), return_exceptions=True)

    assert sum(not isinstance(r, Exception) for r in results) == 4


@pytest.mark.anyio
async def test_leaked_slots_heal_after_the_stale_age():
    """A crashed gateway never releases its slots. The stale cleanup must free them."""
    key = uuid.uuid4()
    # Generous margins: Redis ages entries by the Docker VM clock, the test sleeps on the host clock,
    # and a busy machine (e.g. a kind cluster running) can stretch both.
    async with limiter(stale_seconds=1.0) as cl:
        await cl.acquire(key, 1, uuid.uuid4())  # never released, like after a crash
        with pytest.raises(GatewayError):
            await cl.acquire(key, 1, uuid.uuid4())
        await asyncio.sleep(1.2)
        await cl.acquire(key, 1, uuid.uuid4())


@pytest.mark.anyio
async def test_release_still_runs_when_the_request_is_cancelled():
    """In anyio, once a scope is cancelled every await inside it is cancelled too, including
    the cleanup in a finally block. Without the shield in Slot.release, the slot would leak.

    The release uses a client with no open connection, so it must connect before sending ZREM:
    that is the case where cancellation hits before Redis ever receives the command."""
    key, request_id = uuid.uuid4(), uuid.uuid4()
    async with limiter() as cl:
        await cl.acquire(key, 1, request_id)

    fresh_client = aredis.from_url(TEST_REDIS_URL)
    slot = Slot(fresh_client, f"inflight:{key}", str(request_id))
    try:
        with anyio.CancelScope() as scope:
            scope.cancel()
            try:
                await anyio.sleep(1)  # cancelled here, like a request being torn down
            finally:
                await slot.release()
    finally:
        await fresh_client.aclose()

    assert in_progress(str(key)) == 0


def test_settings_reject_stale_age_shorter_than_a_request():
    with pytest.raises(ValidationError, match="concurrency_stale_seconds"):
        make_settings(concurrency_stale_seconds=200, stream_total_timeout=300)


# --- Through the HTTP API: every exit path releases the slot ------------------


@pytest.fixture
def upstream():
    with respx.mock(assert_all_called=False) as mock:
        yield mock.post(UPSTREAM_URL)


@pytest.fixture
def app_client():
    with TestClient(create_app(make_settings())) as test_client:
        created = create_key(test_client, max_concurrent=1)
        test_client.headers["Authorization"] = f"Bearer {created['key']}"
        yield test_client, created["id"]


@pytest.mark.parametrize(
    ("upstream_result", "body", "expected_status"),
    [
        pytest.param(httpx.Response(200, json=COMPLETION), CHAT, 200, id="non-stream success"),
        pytest.param(httpx.Response(500), CHAT, 502, id="non-stream upstream error"),
        pytest.param(httpx.ConnectError("down"), CHAT, 502, id="non-stream unreachable"),
        pytest.param(httpx.Response(200, text="data: [DONE]\n\n"), {**CHAT, "stream": True}, 200, id="stream success"),
        pytest.param(
            httpx.Response(404, json={"error": {"message": "no"}}),
            {**CHAT, "stream": True},
            404,
            id="stream rejected at open",
        ),
        pytest.param(httpx.ConnectError("down"), {**CHAT, "stream": True}, 502, id="stream unreachable"),
    ],
)
def test_slot_is_released_on_every_exit_path(app_client, upstream, upstream_result, body, expected_status):
    client, key_id = app_client
    if isinstance(upstream_result, Exception):
        upstream.mock(side_effect=upstream_result)
    else:
        upstream.mock(return_value=upstream_result)

    response = client.post("/v1/chat/completions", json=body)

    assert response.status_code == expected_status
    assert in_progress(key_id) == 0


def test_busy_key_gets_429_without_reaching_upstream(app_client, upstream):
    client, key_id = app_client
    upstream.mock(return_value=httpx.Response(200, json=COMPLETION))
    # Occupy the key's only slot, as if another request were in progress.
    with redis.Redis.from_url(TEST_REDIS_URL) as r:
        r.zadd(f"inflight:{key_id}", {"other-request": int(time.time() * 1000)})

    response = client.post("/v1/chat/completions", json=CHAT)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "concurrency_limit_exceeded"
    assert response.headers["Retry-After"] == "1"
    assert not upstream.called
