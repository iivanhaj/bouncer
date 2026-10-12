import uuid
from datetime import UTC, datetime

import httpx
import pytest
import redis
import redis.asyncio as aredis
import respx
from fastapi.testclient import TestClient

from app import concurrency
from app.budget import BudgetEnforcer, counter_key, headroom, month_of
from app.main import create_app
from tests.helpers import TEST_REDIS_URL, UPSTREAM_URL, create_key, fetch_rows, make_settings, run_sql

# Small limits so budgets are small numbers: headroom = 1 x (1000 + 100 + 16) = 1116 tokens.
SETTINGS = {"max_body_bytes": 1000, "max_tokens_cap": 100}
HEADROOM = 1116
BUDGET = 10 * HEADROOM  # the minimum allowed
USABLE = BUDGET - HEADROOM

CHAT = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
COMPLETION = {
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}}],
    "usage": {"prompt_tokens": 30, "completion_tokens": 12},
}


def test_headroom_formula():
    assert headroom(make_settings(**SETTINGS), max_concurrent=1) == HEADROOM
    assert headroom(make_settings(**SETTINGS), max_concurrent=3) == 3 * HEADROOM


@pytest.mark.parametrize(
    ("moment", "start", "end"),
    [
        (datetime(2026, 3, 15, 12, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)),
        (
            datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
            datetime(2026, 12, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
        ),
    ],
)
def test_month_boundaries_are_utc_calendar_months(moment, start, end):
    assert month_of(moment) == (start, end)


def counter(key_id: str) -> int | None:
    with redis.Redis.from_url(TEST_REDIS_URL) as r:
        value = r.get(counter_key(uuid.UUID(key_id), datetime.now(UTC)))
    return None if value is None else int(value)


def set_counter(key_id: str, value: int) -> None:
    with redis.Redis.from_url(TEST_REDIS_URL) as r:
        r.set(counter_key(uuid.UUID(key_id), datetime.now(UTC)), value)


@pytest.fixture
def upstream():
    with respx.mock(assert_all_called=False) as mock:
        yield mock.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))


@pytest.fixture
def app_client(upstream):
    with TestClient(create_app(make_settings(**SETTINGS))) as client:
        key = create_key(client, max_concurrent=1, monthly_token_budget=BUDGET)
        client.headers["Authorization"] = f"Bearer {key['key']}"
        yield client, key["id"]


def test_tokens_are_added_to_the_counter(app_client):
    client, key_id = app_client

    client.post("/v1/chat/completions", json=CHAT)
    client.post("/v1/chat/completions", json=CHAT)

    assert counter(key_id) == 2 * 42


def test_requests_stop_at_the_usable_budget_not_the_full_budget(app_client, upstream):
    client, key_id = app_client
    set_counter(key_id, USABLE - 1)
    assert client.post("/v1/chat/completions", json=CHAT).status_code == 200

    # Now at USABLE + 41: still 1000+ tokens under the real budget, but the rest is headroom.
    upstream.reset()
    response = client.post("/v1/chat/completions", json=CHAT)

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "insufficient_quota"
    assert str(USABLE) in error["message"] and str(BUDGET) in error["message"]
    assert not upstream.called


def test_over_budget_request_is_logged_and_releases_its_slot(app_client):
    client, key_id = app_client
    set_counter(key_id, USABLE)

    assert client.post("/v1/chat/completions", json=CHAT).status_code == 429
    # max_concurrent is 1: if the slot had leaked, this would be a concurrency 429, not a budget one.
    assert client.post("/v1/chat/completions", json=CHAT).json()["error"]["code"] == "insufficient_quota"


def test_unlimited_key_is_never_checked(upstream):
    with TestClient(create_app(make_settings(**SETTINGS))) as client:
        key = create_key(client)  # no budget
        client.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key['key']}"})

    # No check means no counter is ever created, and settle() only adds to existing counters.
    assert counter(key["id"]) is None


def test_lost_counter_is_rebuilt_from_the_usage_log(upstream):
    with TestClient(create_app(make_settings(**SETTINGS))) as client:
        key = create_key(client, max_concurrent=1, monthly_token_budget=BUDGET)
    key_id = uuid.UUID(key["id"])
    # This month's usage: enough to be over the usable budget. Last month's must not count.
    insert = (
        "INSERT INTO requests (id, key_id, model, prompt_tokens, completion_tokens, usage_estimated, "
        "cost_usd, latency_ms, status, created_at) VALUES ($1, $2, 'm', $3, 0, false, 0, 1, 'ok', $4)"
    )
    run_sql(insert, uuid.uuid4(), key_id, USABLE, datetime.now(UTC))
    run_sql(insert, uuid.uuid4(), key_id, 999_999, datetime(2020, 1, 15, tzinfo=UTC))
    assert counter(key["id"]) is None  # Redis has "lost" it

    with TestClient(create_app(make_settings(**SETTINGS))) as client:
        response = client.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key['key']}"})

    assert response.status_code == 429
    assert counter(key["id"]) == USABLE


@pytest.mark.anyio
async def test_settle_skips_a_missing_counter_instead_of_restarting_it_at_zero():
    """If Redis lost the counter between check and settle, INCRBY would recreate it near zero and
    hide this month's spending. settle() must leave it missing so the next check rebuilds it."""
    key_id = uuid.uuid4()
    client = aredis.from_url(TEST_REDIS_URL)
    try:
        await BudgetEnforcer(client, None, make_settings()).settle(key_id, 42, datetime.now(UTC))
    finally:
        await client.aclose()

    assert counter(str(key_id)) is None


def test_tokens_are_counted_before_the_slot_is_released(upstream, monkeypatch):
    """DESIGN.md pipeline rule: if the slot were released first, a new request could pass the
    budget check before these tokens were counted."""
    seen_at_release = []
    original_release = concurrency.Slot.release

    async def spying_release(self):
        seen_at_release.append(counter(key["id"]))
        await original_release(self)

    with TestClient(create_app(make_settings(**SETTINGS))) as client:
        key = create_key(client, max_concurrent=1, monthly_token_budget=BUDGET)
        monkeypatch.setattr(concurrency.Slot, "release", spying_release)
        client.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key['key']}"})

    assert seen_at_release == [42]


def test_budget_exceeded_status_in_usage_log(upstream):
    with TestClient(create_app(make_settings(**SETTINGS))) as client:
        key = create_key(client, max_concurrent=1, monthly_token_budget=BUDGET)
        set_counter(key["id"], USABLE)
        client.post("/v1/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key['key']}"})

    assert [r["status"] for r in fetch_rows("SELECT status FROM requests")] == ["budget_exceeded"]
