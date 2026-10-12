import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from tests.helpers import UPSTREAM_URL, create_key, make_settings

LIMIT = 2_000
COMPLETION = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}}]}


def body_of_size(size: int) -> bytes:
    """A valid chat request whose JSON encoding is exactly `size` bytes."""
    base = {"model": "m", "messages": [{"role": "user", "content": ""}]}
    padding = size - len(json.dumps(base).encode())
    base["messages"][0]["content"] = "x" * padding
    encoded = json.dumps(base).encode()
    assert len(encoded) == size
    return encoded


@pytest.fixture
def client():
    with respx.mock(assert_all_called=False) as mock:
        mock.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))
        with TestClient(create_app(make_settings(max_body_bytes=LIMIT))) as test_client:
            test_client.headers["Authorization"] = f"Bearer {create_key(test_client)['key']}"
            test_client.headers["Content-Type"] = "application/json"
            yield test_client


def test_body_at_the_limit_is_accepted(client):
    response = client.post("/v1/chat/completions", content=body_of_size(LIMIT))

    assert response.status_code == 200


def test_declared_size_over_the_limit_is_413(client):
    response = client.post("/v1/chat/completions", content=body_of_size(LIMIT + 1))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_chunked_body_over_the_limit_is_413(client):
    # A generator body has no Content-Length, so the bytes must be counted as they arrive.
    data = body_of_size(LIMIT + 500)

    def chunks():
        for i in range(0, len(data), 100):
            yield data[i : i + 100]

    response = client.post("/v1/chat/completions", content=chunks())

    assert response.status_code == 413


def test_limit_runs_before_auth(client):
    # Unauthenticated oversized requests are rejected without touching the database.
    response = client.post("/v1/chat/completions", content=body_of_size(LIMIT + 1), headers={"Authorization": ""})

    assert response.status_code == 413


def test_limit_applies_to_admin_api_too(client):
    response = client.post("/admin/keys", content=b"{" + b" " * LIMIT + b"}")

    assert response.status_code == 413
