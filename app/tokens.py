"""Token estimates for when the upstream doesn't report usage.

Ollama 0.4.1 ignores stream_options.include_usage, so streamed responses come back without
token counts (DESIGN.md "To verify"). These estimates are rough on purpose: they lean high
rather than low, because undercounting would let a team spend past its budget. Rows built
from them are marked usage_estimated = true.
"""

import math

from app.schemas import ChatMessage

# Chat templates wrap every message in role markers, and some models add a default system
# prompt: "Say hi" is 2 words but Qwen counts it as 31 prompt tokens.
_TOKENS_PER_MESSAGE = 10
_TOKENS_PER_REQUEST = 30
# English averages about 4 bytes per token; 3 leans high.
_BYTES_PER_TOKEN = 3


def message_text(message: ChatMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return "".join(part.text for part in message.content)


def estimate_text_tokens(text: str) -> int:
    return math.ceil(len(text.encode()) / _BYTES_PER_TOKEN)


def estimate_prompt_tokens(messages: list[ChatMessage]) -> int:
    content = sum(estimate_text_tokens(message_text(m)) for m in messages)
    return content + _TOKENS_PER_MESSAGE * len(messages) + _TOKENS_PER_REQUEST
