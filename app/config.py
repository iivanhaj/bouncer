from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Only what database migrations need, so Alembic can run without the gateway's secrets."""

    # hide_input_in_errors: a rejected setting (e.g. an admin token one character too short)
    # must never be printed in the startup error, where it would end up in logs.
    model_config = SettingsConfigDict(
        env_prefix="MODELGUARD_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = "postgresql+asyncpg://modelguard:modelguard@localhost:5432/modelguard"


class Settings(DatabaseSettings):
    """Gateway settings, read from environment variables prefixed MODELGUARD_ (or a .env file)."""

    # Required, at least 32 characters: the gateway refuses to start without a strong admin token.
    admin_token: SecretStr = Field(min_length=32)

    redis_url: str = "redis://localhost:6379/0"
    # Short on purpose: a hanging Redis must not hang every request (DESIGN.md §2).
    redis_timeout_ms: int = 100
    rate_limit_window_seconds: float = 60.0

    # Upstream name -> base URL. Set as JSON in the environment, e.g.
    # MODELGUARD_UPSTREAMS='{"ollama": "http://host.docker.internal:11434", "mock": "http://mock-upstream:8001"}'
    upstreams: dict[str, str] = {"ollama": "http://localhost:11434", "mock": "http://localhost:8001"}
    # Which models exist and which upstream serves each one (the model allowlist).
    models_file: Path = Path("models.yaml")
    upstream_connect_timeout: float = 5.0
    # Non-streaming: nothing arrives until generation is done, so this covers the whole generation.
    upstream_read_timeout: float = 300.0

    # Streaming timeouts (DESIGN.md §5). A cold model has to load into RAM before its first token.
    stream_first_token_timeout: float = 60.0
    stream_chunk_timeout: float = 30.0
    # Must stay below the concurrency cleanup age (DESIGN.md §2).
    stream_total_timeout: float = 300.0

    # Prometheus metrics are served on this port, separate from the API. None disables it
    # (tests create many apps in one process and can't all bind the same port).
    metrics_port: int | None = 9100

    # In-progress entries older than this are treated as leaked slots and removed (DESIGN.md §2).
    concurrency_stale_seconds: float = 360.0

    # Usage log writer (DESIGN.md §6): rows are queued in memory and written in batches.
    usage_queue_size: int = 10_000
    usage_batch_size: int = 100
    usage_flush_interval_seconds: float = 1.0

    # Prompt-injection detection (DESIGN.md §4). "monitor" logs what would have been blocked
    # without blocking, for trying new rules on real traffic safely.
    injection_mode: Literal["block", "monitor"] = "block"
    injection_threshold: float = Field(default=1.0, gt=0)
    # Second layer behind the rules (app/injection_model.py). Block if either fires.
    injection_classifier: bool = True
    # "monitor": log what the classifier alone would block, without blocking. Its false positives
    # are measured on public data; on your own traffic, check them in monitor mode first.
    injection_classifier_mode: Literal["block", "monitor"] = "block"

    # Extra tokens per request in the budget headroom: upstreams can report a token or so past
    # max_tokens (DESIGN.md §2).
    budget_token_margin: int = 16

    max_tokens_cap: int = 2048
    # Also the upper bound on prompt tokens: a token is always at least one byte.
    max_body_bytes: int = 32_768

    @model_validator(mode="after")
    def stale_age_outlives_every_request(self) -> "Settings":
        # If a request could outlive the stale age, cleanup would free a slot that's still in use.
        longest_request = max(self.stream_total_timeout, self.upstream_connect_timeout + self.upstream_read_timeout)
        if self.concurrency_stale_seconds <= longest_request:
            raise ValueError(
                f"concurrency_stale_seconds ({self.concurrency_stale_seconds}) must be longer than "
                f"the longest possible request ({longest_request}s)"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
