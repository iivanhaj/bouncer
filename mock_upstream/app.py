"""A fake OpenAI-compatible model server with predictable timing.

Load tests run against this instead of a real model: a CPU model is always the bottleneck,
so only a fast, deterministic upstream shows the gateway's own overhead.
Timing is configured with environment variables:

    MOCK_TTFT_MS          delay before the first token        (default 100)
    MOCK_TOKEN_DELAY_MS   delay between tokens                (default 10)
    MOCK_COMPLETION_TOKENS  tokens per answer, unless max_tokens is lower (default 20)

Run with: uvicorn mock_upstream.app:app --port 8001
"""

import asyncio
import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

TTFT = int(os.environ.get("MOCK_TTFT_MS", "100")) / 1000
TOKEN_DELAY = int(os.environ.get("MOCK_TOKEN_DELAY_MS", "10")) / 1000
COMPLETION_TOKENS = int(os.environ.get("MOCK_COMPLETION_TOKENS", "20"))

app = FastAPI(title="Mock upstream")


def _prompt_tokens(messages: list[dict[str, Any]]) -> int:
    # Rough and deterministic: about 4 characters per token.
    text = "".join(
        m["content"] if isinstance(m.get("content"), str) else json.dumps(m.get("content")) for m in messages
    )
    return max(1, len(text) // 4)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "mock")
    count = min(COMPLETION_TOKENS, body.get("max_tokens") or COMPLETION_TOKENS)
    usage = {
        "prompt_tokens": _prompt_tokens(body.get("messages", [])),
        "completion_tokens": count,
        "total_tokens": _prompt_tokens(body.get("messages", [])) + count,
    }
    completion_id = f"chatcmpl-mock-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if not body.get("stream"):
        await asyncio.sleep(TTFT + TOKEN_DELAY * (count - 1))
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": " ".join(f"tok{i}" for i in range(count))},
                    "finish_reason": "length" if count < COMPLETION_TOKENS else "stop",
                }
            ],
            "usage": usage,
        }

    include_usage = (body.get("stream_options") or {}).get("include_usage", False)

    async def events():
        def chunk(delta: dict[str, Any], finish_reason: str | None = None) -> str:
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            return f"data: {json.dumps(payload)}\n\n"

        await asyncio.sleep(TTFT)
        for i in range(count):
            if i:
                await asyncio.sleep(TOKEN_DELAY)
            yield chunk({"content": f"tok{i} "})
        yield chunk({}, "length" if count < COMPLETION_TOKENS else "stop")
        if include_usage:
            usage_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": usage,
            }
            yield f"data: {json.dumps(usage_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
