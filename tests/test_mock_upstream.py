import json

from fastapi.testclient import TestClient

from mock_upstream.app import app

client = TestClient(app)
CHAT = {"model": "mock", "messages": [{"role": "user", "content": "Say hi to me"}]}


def data_events(text: str) -> list[str]:
    return [line[len("data: ") :] for line in text.splitlines() if line.startswith("data: ")]


def test_non_streaming_returns_completion_with_usage():
    body = client.post("/v1/chat/completions", json={**CHAT, "max_tokens": 3}).json()

    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "tok0 tok1 tok2"
    assert body["usage"]["completion_tokens"] == 3
    assert body["usage"]["total_tokens"] == body["usage"]["prompt_tokens"] + 3


def test_streaming_without_usage():
    events = data_events(client.post("/v1/chat/completions", json={**CHAT, "stream": True, "max_tokens": 2}).text)

    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert [c["choices"][0]["delta"].get("content") for c in chunks] == ["tok0 ", "tok1 ", None]
    assert all("usage" not in c for c in chunks)


def test_streaming_with_usage_sends_usage_chunk_before_done():
    events = data_events(
        client.post(
            "/v1/chat/completions",
            json={**CHAT, "stream": True, "max_tokens": 2, "stream_options": {"include_usage": True}},
        ).text
    )

    usage_chunk = json.loads(events[-2])
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"]["completion_tokens"] == 2
