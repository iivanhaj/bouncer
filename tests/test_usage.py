"""Usage logging. Rows are read AFTER the app shuts down: shutdown flushes the write queue."""

import asyncio
import json
import time
import uuid
from decimal import Decimal

import httpx
import pytest
import redis
import respx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import create_app
from app.usage import UsageRecord, UsageRecorder
from tests.helpers import TEST_DATABASE_URL, TEST_REDIS_URL, UPSTREAM_URL, create_key, fetch_rows, make_settings

# Any honest prompt; the tests check its text is never stored. (A terse "my secret prompt text"
# looked like a prompt-extraction request to the classifier, which is fair.)
PROMPT = "Draft a short reply to Priya confirming the Thursday budget review"
CHAT = {"model": "m", "messages": [{"role": "user", "content": PROMPT}]}
COMPLETION = {
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "the secret answer"}}],
    "usage": {"prompt_tokens": 500, "completion_tokens": 250, "total_tokens": 750},
}


def sse(*chunks: dict | str) -> str:
    return "".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks)


def content_chunk(text: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


@pytest.fixture
def upstream():
    with respx.mock(assert_all_called=False) as mock:
        yield mock.post(UPSTREAM_URL)


def run_requests(*requests, key_fields=None) -> tuple[list[httpx.Response], dict]:
    """Run requests through a fresh app, shut it down (flushing the log), return responses and the key."""
    with TestClient(create_app(make_settings())) as client:
        key = create_key(client, **(key_fields or {}))
        client.headers["Authorization"] = f"Bearer {key['key']}"
        responses = [client.post("/v1/chat/completions", json=body) for body in requests]
    return responses, key


def rows() -> list[dict]:
    return fetch_rows("SELECT * FROM requests ORDER BY created_at")


# --- What gets logged ---------------------------------------------------------


def test_successful_request_is_logged_with_upstream_usage_and_cost(upstream):
    upstream.mock(return_value=httpx.Response(200, json=COMPLETION))

    (response,), key = run_requests(CHAT)

    [row] = rows()
    assert row["id"] == uuid.UUID(response.headers["x-request-id"])
    assert str(row["key_id"]) == key["id"]
    assert row["model"] == "m"
    assert row["upstream"] == "test"
    assert row["status"] == "ok"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (500, 250)
    assert row["usage_estimated"] is False
    # models.test.yaml prices "m" at $1 in / $2 out per 1k tokens: 0.5 + 0.5
    assert row["cost_usd"] == Decimal("1.0")
    assert row["latency_ms"] >= 0
    assert row["redaction_counts"] is None  # SQL NULL, not JSON null


def test_prompt_and_answer_text_are_never_stored(upstream):
    upstream.mock(return_value=httpx.Response(200, json=COMPLETION))

    run_requests(CHAT)

    stored = str(rows())
    assert PROMPT not in stored
    assert "the secret answer" not in stored


def test_missing_usage_is_estimated_and_flagged(upstream):
    upstream.mock(return_value=httpx.Response(200, json={"choices": COMPLETION["choices"]}))

    run_requests(CHAT)

    [row] = rows()
    assert row["usage_estimated"] is True
    assert row["prompt_tokens"] > 0
    assert row["completion_tokens"] > 0


def test_stream_with_usage_chunk_uses_exact_numbers(upstream):
    usage_chunk = {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 2}}
    upstream.mock(
        return_value=httpx.Response(200, text=sse(content_chunk("Hi"), content_chunk("!"), usage_chunk, "[DONE]"))
    )

    run_requests({**CHAT, "stream": True})

    [row] = rows()
    assert (row["prompt_tokens"], row["completion_tokens"], row["usage_estimated"]) == (40, 2, False)
    assert row["status"] == "ok"
    assert row["ttft_ms"] is not None


def test_stream_without_usage_counts_chunks(upstream):
    upstream.mock(return_value=httpx.Response(200, text=sse(*(content_chunk(t) for t in "abc"), "[DONE]")))

    run_requests({**CHAT, "stream": True})

    [row] = rows()
    assert row["completion_tokens"] == 3
    assert row["usage_estimated"] is True


@pytest.mark.parametrize(
    ("upstream_result", "body", "status"),
    [
        pytest.param(httpx.Response(500), CHAT, "upstream_error", id="upstream 5xx"),
        pytest.param(httpx.ReadTimeout("slow"), CHAT, "upstream_timeout", id="upstream timeout"),
        pytest.param(httpx.ConnectError("down"), {**CHAT, "stream": True}, "upstream_error", id="stream unreachable"),
        pytest.param(
            httpx.Response(200, json=COMPLETION), {**CHAT, "model": "unlisted"}, "bad_request", id="unknown model"
        ),
    ],
)
def test_failures_are_logged_with_a_status(upstream, upstream_result, body, status):
    if isinstance(upstream_result, Exception):
        upstream.mock(side_effect=upstream_result)
    else:
        upstream.mock(return_value=upstream_result)

    run_requests(body)

    [row] = rows()
    assert row["status"] == status


def test_concurrency_limited_request_is_logged(upstream):
    upstream.mock(return_value=httpx.Response(200, json=COMPLETION))
    with TestClient(create_app(make_settings())) as client:
        key = create_key(client, max_concurrent=1)
        with redis.Redis.from_url(TEST_REDIS_URL) as r:
            r.zadd(f"inflight:{key['id']}", {"other": int(time.time() * 1000)})
        client.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key['key']}"})

    assert [row["status"] for row in rows()] == ["concurrency_limited"]


def test_401_and_rate_limited_429_are_not_logged(upstream):
    """Rejected traffic must not write to the database, or a flood could fill it (DESIGN.md §6)."""
    upstream.mock(return_value=httpx.Response(200, json=COMPLETION))
    with TestClient(create_app(make_settings())) as client:
        key = create_key(client, rpm_limit=1)
        client.post("/v1/chat/completions", json=CHAT, headers={"Authorization": "Bearer mgd_nope"})
        auth = {"Authorization": f"Bearer {key['key']}"}
        assert client.post("/v1/chat/completions", json=CHAT, headers=auth).status_code == 200
        assert client.post("/v1/chat/completions", json=CHAT, headers=auth).status_code == 429

    assert [row["status"] for row in rows()] == ["ok"]


# --- The recorder -------------------------------------------------------------


def make_record(key_id: uuid.UUID) -> UsageRecord:
    return UsageRecord(id=uuid.uuid4(), key_id=key_id, model="m")


async def count_rows(engine) -> int:
    async with engine.connect() as connection:
        return (await connection.exec_driver_sql("SELECT count(*) FROM requests")).scalar_one()


def existing_key_id() -> uuid.UUID:
    with TestClient(create_app(make_settings())) as client:
        return uuid.UUID(create_key(client)["id"])


@pytest.mark.anyio
async def test_recorder_writes_everything_in_batches_and_flushes_on_stop():
    key_id = existing_key_id()
    engine = create_async_engine(TEST_DATABASE_URL)
    recorder = UsageRecorder(async_sessionmaker(engine), batch_size=100, flush_interval=10)
    recorder.start()
    for _ in range(250):
        recorder.record(make_record(key_id))
    started = time.monotonic()
    await recorder.stop()

    assert time.monotonic() - started < 2, "stop() must not wait out the 10s flush interval"
    assert await count_rows(engine) == 250
    assert recorder.dropped == 0
    await engine.dispose()


@pytest.mark.anyio
async def test_full_queue_drops_rows_instead_of_blocking():
    engine = create_async_engine(TEST_DATABASE_URL)
    recorder = UsageRecorder(async_sessionmaker(engine), max_queue=5)  # writer not started
    for _ in range(8):
        recorder.record(make_record(uuid.uuid4()))  # returns immediately, never waits
    await engine.dispose()

    assert recorder.dropped == 3


@pytest.mark.anyio
async def test_database_errors_do_not_kill_the_writer():
    key_id = existing_key_id()
    engine = create_async_engine(TEST_DATABASE_URL)
    recorder = UsageRecorder(async_sessionmaker(engine), flush_interval=0.05)
    recorder.start()
    recorder.record(make_record(uuid.uuid4()))  # unknown key: foreign key violation
    await asyncio.sleep(0.2)
    recorder.record(make_record(key_id))  # must still be written
    await recorder.stop()

    assert recorder.dropped == 1
    assert await count_rows(engine) == 1
    await engine.dispose()


def test_error_responses_carry_the_request_id(upstream):
    upstream.mock(return_value=httpx.Response(500))

    (response,), _ = run_requests(CHAT)

    assert response.status_code == 502
    [row] = rows()
    assert response.headers["x-request-id"] == str(row["id"])
