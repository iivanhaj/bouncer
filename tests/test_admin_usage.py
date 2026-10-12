import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.budget import headroom
from app.main import create_app
from tests.helpers import ADMIN_HEADERS, create_key, make_settings, run_sql

NOW = datetime.now(UTC)


def log_row(
    key_id,
    *,
    status="ok",
    prompt=10,
    completion=5,
    cost="0.01",
    latency=100,
    ttft=None,
    rules=None,
    score=None,
    at=None,
):
    run_sql(
        "INSERT INTO requests (id, key_id, model, upstream, prompt_tokens, completion_tokens, usage_estimated, "
        "cost_usd, latency_ms, ttft_ms, status, blocked_rules, injection_score, created_at) "
        "VALUES ($1, $2, 'm', 'test', $3, $4, false, $5::numeric, $6, $7, $8, $9, $10, $11)",
        uuid.uuid4(),
        uuid.UUID(key_id),
        prompt,
        completion,
        cost,
        latency,
        ttft,
        status,
        rules,
        score,
        at or NOW,
    )


@pytest.fixture
def client():
    with TestClient(create_app(make_settings())) as test_client:
        yield test_client


# --- /admin/usage -----------------------------------------------------------------------


def test_usage_totals_per_key(client):
    key = create_key(client, team_name="search", monthly_token_budget=10 * headroom(make_settings(), 4))
    log_row(key["id"], latency=100, ttft=40)
    log_row(key["id"], latency=300, ttft=60)
    log_row(key["id"], status="upstream_error", prompt=0, completion=0, cost="0", latency=50)
    log_row(key["id"], status="blocked_injection", prompt=0, completion=0, cost="0", latency=1, rules=["x"], score=1.0)

    report = client.get("/admin/usage", headers=ADMIN_HEADERS).json()

    [usage] = report["keys"]
    assert usage["team_name"] == "search"
    assert usage["requests"] == 4
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (20, 10)
    assert float(usage["cost_usd"]) == pytest.approx(0.02)
    assert usage["errors"] == 1
    assert usage["blocked"] == 1
    assert usage["avg_ttft_ms"] == pytest.approx(50)
    assert usage["month_tokens"] == 30
    assert usage["usable_budget"] == usage["monthly_token_budget"] - headroom(make_settings(), 4)


def test_usage_window_excludes_old_rows(client):
    key = create_key(client)
    log_row(key["id"])
    log_row(key["id"], at=NOW - timedelta(days=30))

    report = client.get("/admin/usage?days=7", headers=ADMIN_HEADERS).json()

    assert report["keys"][0]["requests"] == 1


def test_usage_series_is_bucketed(client):
    key = create_key(client)
    log_row(key["id"], at=NOW)
    log_row(key["id"], at=NOW - timedelta(days=2))
    log_row(key["id"], at=NOW - timedelta(days=2, minutes=1))

    series = client.get("/admin/usage?days=7&bucket=day", headers=ADMIN_HEADERS).json()["series"]

    assert sorted(point["requests"] for point in series) == [1, 2]


def test_usage_can_be_filtered_by_key(client):
    a, b = create_key(client, team_name="a"), create_key(client, team_name="b")
    log_row(a["id"])
    log_row(b["id"])

    report = client.get(f"/admin/usage?key_id={a['id']}", headers=ADMIN_HEADERS).json()

    assert [k["team_name"] for k in report["keys"]] == ["a"]


def test_usage_requires_admin(client):
    assert client.get("/admin/usage").status_code == 401


# --- /admin/blocked ---------------------------------------------------------------------


def test_blocked_lists_blocked_and_would_have_blocked(client):
    key = create_key(client, team_name="support")
    log_row(key["id"], status="blocked_injection", rules=["ignore-previous-instructions"], score=1.0)
    log_row(key["id"], status="ok", rules=["special-mode", "no-restrictions"], score=1.2)  # monitor mode
    log_row(key["id"], status="ok", rules=["dan-name"], score=0.3)  # below threshold: not listed
    log_row(key["id"], status="ok")

    blocked = client.get("/admin/blocked", headers=ADMIN_HEADERS).json()

    assert len(blocked) == 2
    assert {(b["blocked"], tuple(b["rules"])) for b in blocked} == {
        (True, ("ignore-previous-instructions",)),
        (False, ("special-mode", "no-restrictions")),
    }
    assert all(b["team_name"] == "support" for b in blocked)


def test_blocked_is_newest_first_and_limited(client):
    key = create_key(client)
    for minutes in range(5):
        log_row(key["id"], status="blocked_injection", rules=["r"], score=1.0, at=NOW - timedelta(minutes=minutes))

    blocked = client.get("/admin/blocked?limit=3", headers=ADMIN_HEADERS).json()

    assert len(blocked) == 3
    times = [b["created_at"] for b in blocked]
    assert times == sorted(times, reverse=True)
