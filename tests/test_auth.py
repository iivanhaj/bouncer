import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import keys
from app.config import Settings
from app.main import create_app
from tests.helpers import ADMIN_HEADERS, ADMIN_TOKEN, UPSTREAM_URL, create_key, make_settings, run_sql

CHAT = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
COMPLETION = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}}]}


@pytest.fixture
def client():
    with TestClient(create_app(make_settings())) as test_client:
        yield test_client


@pytest.fixture
def upstream():
    with respx.mock(assert_all_called=False) as mock:
        yield mock.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))


def chat(client, key: str | None, body=CHAT):
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    return client.post("/v1/chat/completions", json=body, headers=headers)


# --- Team keys on /v1 -------------------------------------------------------


def test_valid_key_reaches_upstream(client, upstream):
    response = chat(client, create_key(client)["key"])

    assert response.status_code == 200
    assert upstream.called


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(None, id="missing header"),
        pytest.param("not-a-key", id="malformed"),
        pytest.param(keys.generate_key(), id="well-formed but unknown"),
        pytest.param(ADMIN_TOKEN, id="admin token"),
    ],
)
def test_bad_keys_get_the_same_401(client, upstream, key):
    response = chat(client, key)

    assert response.status_code == 401
    assert response.json()["error"] == {
        "message": "Invalid API key",
        "type": "invalid_request_error",
        "code": "invalid_api_key",
    }
    assert not upstream.called


def test_revoked_key_gets_the_same_401(client, upstream):
    created = create_key(client)
    assert client.delete(f"/admin/keys/{created['id']}", headers=ADMIN_HEADERS).status_code == 204

    response = chat(client, created["key"])

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid API key"


def test_expired_key_gets_the_same_401(client, upstream):
    created = create_key(client, expires_in_days=1)
    assert chat(client, created["key"]).status_code == 200

    run_sql("UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = $1", uuid.UUID(created["id"]))

    response = chat(client, created["key"])
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid API key"


def test_auth_runs_before_body_validation(client, upstream):
    # Unauthenticated callers must not be able to probe the request schema.
    response = chat(client, None, body={"model": "m", "unknown": 1, "messages": []})

    assert response.status_code == 401


# --- Admin API ----------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing"),
        pytest.param({"Authorization": "Bearer wrong-token-wrong-token-wrong-token"}, id="wrong"),
    ],
)
def test_admin_requires_admin_token(client, headers):
    assert client.get("/admin/keys", headers=headers).status_code == 401
    assert client.post("/admin/keys", json={"team_name": "x"}, headers=headers).status_code == 401


def test_team_key_cannot_use_admin_api(client):
    team_key = create_key(client)["key"]

    response = client.get("/admin/keys", headers={"Authorization": f"Bearer {team_key}"})

    assert response.status_code == 401


def test_create_key_returns_full_key_once_with_defaults(client):
    created = create_key(client)

    assert keys.is_well_formed(created["key"])
    assert created["key_prefix"] == created["key"][:12]
    assert created["rpm_limit"] == 60
    assert created["max_concurrent"] == 4
    assert created["monthly_token_budget"] is None
    assert created["expires_at"] is None
    assert created["revoked_at"] is None


def test_list_keys_never_exposes_key_or_hash(client):
    created = create_key(client)

    listed = client.get("/admin/keys", headers=ADMIN_HEADERS).json()

    assert [item["id"] for item in listed] == [created["id"]]
    assert "key" not in listed[0]
    assert "key_hash" not in listed[0]
    assert created["key"] not in str(listed)
    assert keys.hash_key(created["key"]) not in str(listed)


def test_only_the_hash_is_stored(client):
    created = create_key(client)

    # If the full key were stored anywhere in the row, this would find it.
    run_sql(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM api_keys WHERE api_keys::text LIKE '%' || $k$"
        + created["key"]
        + "$k$ || '%') THEN RAISE EXCEPTION 'full key stored'; END IF; END $$"
    )


def test_revoke_unknown_key_is_404(client):
    response = client.delete(f"/admin/keys/{uuid.uuid4()}", headers=ADMIN_HEADERS)

    assert response.status_code == 404


def test_revoking_twice_keeps_first_revocation_time(client):
    created = create_key(client)
    client.delete(f"/admin/keys/{created['id']}", headers=ADMIN_HEADERS)
    first = client.get("/admin/keys", headers=ADMIN_HEADERS).json()[0]["revoked_at"]

    assert client.delete(f"/admin/keys/{created['id']}", headers=ADMIN_HEADERS).status_code == 204
    assert client.get("/admin/keys", headers=ADMIN_HEADERS).json()[0]["revoked_at"] == first


def test_budget_below_ten_times_headroom_is_rejected(client):
    settings = make_settings()
    headroom = 4 * (settings.max_body_bytes + settings.max_tokens_cap)

    too_small = client.post(
        "/admin/keys",
        json={"team_name": "t", "monthly_token_budget": 10 * headroom - 1},
        headers=ADMIN_HEADERS,
    )
    big_enough = client.post(
        "/admin/keys",
        json={"team_name": "t", "monthly_token_budget": 10 * headroom},
        headers=ADMIN_HEADERS,
    )

    assert too_small.status_code == 400
    assert too_small.json()["error"]["code"] == "budget_too_small"
    assert big_enough.status_code == 201


def test_create_key_rejects_unknown_fields(client):
    response = client.post("/admin/keys", json={"team_name": "t", "is_admin": True}, headers=ADMIN_HEADERS)

    assert response.status_code == 400


# --- Startup check ------------------------------------------------------------


def test_settings_refuse_missing_admin_token(monkeypatch):
    monkeypatch.delenv("BOUNCER_ADMIN_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_refuse_short_admin_token_without_printing_it():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, admin_token="too-short-secret")

    assert "too-short-secret" not in str(error.value)
