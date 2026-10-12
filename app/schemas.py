from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.errors import GatewayError

# extra="forbid" everywhere: unknown fields are rejected, never forwarded (DESIGN.md §5).
# A field we don't know about could change cost or behaviour in ways the gateway can't control.


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    # Content may be a plain string or a list of text parts. Image parts fail validation,
    # because images can't be redacted (DESIGN.md §3).
    content: str | list[TextPart]
    name: str | None = None


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_usage: bool = False


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text", "json_object", "json_schema"]
    json_schema: dict[str, Any] | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None

    # Two names for the same limit: max_tokens (older) and max_completion_tokens (newer).
    # Both are checked against the cap, so neither can be used to get around it.
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)

    # n > 1 would multiply the tokens one request uses and break the budget headroom maths.
    n: int | None = Field(default=None, ge=1, le=1)

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    stop: str | list[str] | None = None
    seed: int | None = None
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    response_format: ResponseFormat | None = None

    @model_validator(mode="after")
    def stream_options_need_stream(self) -> "ChatCompletionRequest":
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options is only allowed when stream is true")
        return self

    @property
    def client_wants_usage(self) -> bool:
        return self.stream_options is not None and self.stream_options.include_usage

    def effective_max_tokens(self, cap: int) -> int:
        """The completion-token limit to send upstream: the client's value, or the cap if none given."""
        requested = [v for v in (self.max_tokens, self.max_completion_tokens) if v is not None]
        if not requested:
            return cap
        if len(set(requested)) > 1:
            raise GatewayError(
                400,
                "max_tokens and max_completion_tokens were both set to different values",
                "invalid_request_error",
                "invalid_request",
            )
        if requested[0] > cap:
            raise GatewayError(
                400,
                f"max_tokens must be at most {cap}",
                "invalid_request_error",
                "max_tokens_too_large",
            )
        return requested[0]

    def to_upstream_payload(self, max_tokens_cap: int) -> dict[str, Any]:
        """Build the JSON body for the upstream from validated fields only."""
        payload = self.model_dump(exclude_none=True, exclude={"max_tokens", "max_completion_tokens", "n"})
        # Always send exactly one, older-style limit: every OpenAI-compatible server understands max_tokens.
        payload["max_tokens"] = self.effective_max_tokens(max_tokens_cap)
        if self.stream:
            # Always ask for usage so the gateway can count tokens; the relay strips the usage
            # chunk again if the client didn't ask for it.
            payload["stream_options"] = {"include_usage": True}
        return payload
