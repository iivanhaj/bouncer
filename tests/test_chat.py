import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

UPSTREAM = "http://upstream.test"
UPSTREAM_URL = f"{UPSTREAM}/v1/chat/completions"
CAP = 100

COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "qwen2.5:0.5b",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi!"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


def chat_body(**overrides):
    body = {"model": "qwen2.5:0.5b", "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


@pytest.fixture
def client():
    app = create_app(Settings(upstream_base_url=UPSTREAM, max_tokens_cap=CAP))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def upstream():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def sent_payload(route) -> dict:
    return json.loads(route.calls.last.request.content)


# --- Happy path -------------------------------------------------------------


def test_forwards_request_and_returns_upstream_json(client, upstream):
    route = upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    response = client.post("/v1/chat/completions", json=chat_body(temperature=0.2))

    assert response.status_code == 200
    assert response.json() == COMPLETION
    payload = sent_payload(route)
    assert payload["model"] == "qwen2.5:0.5b"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["temperature"] == 0.2


def test_sets_max_tokens_to_cap_when_missing(client, upstream):
    route = upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    client.post("/v1/chat/completions", json=chat_body())

    assert sent_payload(route)["max_tokens"] == CAP


def test_max_completion_tokens_is_sent_as_max_tokens(client, upstream):
    route = upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    client.post("/v1/chat/completions", json=chat_body(max_completion_tokens=50))

    payload = sent_payload(route)
    assert payload["max_tokens"] == 50
    assert "max_completion_tokens" not in payload


def test_client_headers_are_not_forwarded(client, upstream):
    route = upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    client.post(
        "/v1/chat/completions",
        json=chat_body(),
        headers={"Authorization": "Bearer bnc_secret", "X-Custom": "leak"},
    )

    sent_headers = route.calls.last.request.headers
    assert "authorization" not in sent_headers
    assert "x-custom" not in sent_headers


def test_text_parts_are_accepted(client, upstream):
    upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    response = client.post(
        "/v1/chat/completions",
        json=chat_body(messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]),
    )

    assert response.status_code == 200


# --- Validation: rejected before reaching the upstream ----------------------


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"unknown_field": 1}, id="unknown field"),
        pytest.param({"n": 2}, id="n greater than 1"),
        pytest.param({"max_tokens": CAP + 1}, id="max_tokens over cap"),
        pytest.param({"max_completion_tokens": CAP + 1}, id="max_completion_tokens over cap"),
        pytest.param({"max_tokens": 10, "max_completion_tokens": 20}, id="conflicting token limits"),
        pytest.param({"messages": []}, id="empty messages"),
        pytest.param({"messages": [{"role": "hacker", "content": "hi"}]}, id="invalid role"),
        pytest.param(
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]},
            id="image part",
        ),
    ],
)
def test_invalid_requests_return_400_and_never_reach_upstream(client, upstream, overrides):
    route = upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    response = client.post("/v1/chat/completions", json=chat_body(**overrides))

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert not route.called


def test_streaming_is_rejected_for_now(client, upstream):
    route = upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    response = client.post("/v1/chat/completions", json=chat_body(stream=True))

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "stream_not_supported"
    assert not route.called


# --- Upstream failures ------------------------------------------------------


@pytest.mark.parametrize(
    ("side_effect", "status", "code"),
    [
        pytest.param(httpx.ConnectError("refused"), 502, "upstream_unavailable", id="connection refused"),
        pytest.param(httpx.ConnectTimeout("slow connect"), 502, "upstream_unavailable", id="connect timeout"),
        pytest.param(httpx.ReadTimeout("slow answer"), 504, "upstream_timeout", id="read timeout"),
    ],
)
def test_upstream_network_failures(client, upstream, side_effect, status, code):
    upstream.post(UPSTREAM_URL).mock(side_effect=side_effect)

    response = client.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_upstream_5xx_becomes_502(client, upstream):
    upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))

    response = client.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


def test_upstream_4xx_is_passed_through(client, upstream):
    upstream_error = {"error": {"message": 'model "nope" not found', "type": "api_error"}}
    upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(404, json=upstream_error))

    response = client.post("/v1/chat/completions", json=chat_body(model="nope"))

    assert response.status_code == 404
    assert response.json()["error"]["message"] == 'model "nope" not found'


def test_upstream_invalid_json_becomes_502(client, upstream):
    upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, text="not json"))

    response = client.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 502
