import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from tests.helpers import UPSTREAM_URL, create_key, make_settings

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
    app = create_app(make_settings(max_tokens_cap=CAP))
    with TestClient(app) as test_client:
        test_client.headers["Authorization"] = f"Bearer {create_key(test_client)['key']}"
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

    # The client fixture already sends a real mgd_ key in Authorization.
    client.post("/v1/chat/completions", json=chat_body(), headers={"X-Custom": "leak"})

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
        pytest.param({"stream_options": {"include_usage": True}}, id="stream_options without stream"),
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
    # e.g. the model is in models.yaml but hasn't been pulled into Ollama.
    upstream_error = {"error": {"message": 'model "qwen2.5:0.5b" not found, try pulling it first'}}
    upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(404, json=upstream_error))

    response = client.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 404
    assert response.json()["error"]["message"] == upstream_error["error"]["message"]


# --- Model registry -----------------------------------------------------------


def test_unknown_model_is_404_and_never_reaches_upstream(client, upstream):
    route = upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    response = client.post("/v1/chat/completions", json=chat_body(model="gpt-unlisted"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert not route.called


def test_public_model_name_is_mapped_to_upstream_name(client, upstream):
    route = upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, json=COMPLETION))

    client.post("/v1/chat/completions", json=chat_body(model="aliased"))

    assert sent_payload(route)["model"] == "real-upstream-name"


def test_list_models_returns_the_allowlist(client):
    response = client.get("/v1/models")

    assert response.status_code == 200
    assert response.json()["object"] == "list"
    assert {m["id"] for m in response.json()["data"]} == {"m", "qwen2.5:0.5b", "aliased"}


def test_list_models_requires_a_key(client):
    response = client.get("/v1/models", headers={"Authorization": ""})

    assert response.status_code == 401


def test_upstream_invalid_json_becomes_502(client, upstream):
    upstream.post(UPSTREAM_URL).mock(return_value=httpx.Response(200, text="not json"))

    response = client.post("/v1/chat/completions", json=chat_body())

    assert response.status_code == 502
