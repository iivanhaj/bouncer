from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Only what database migrations need, so Alembic can run without the gateway's secrets."""

    # hide_input_in_errors: a rejected setting (e.g. an admin token one character too short)
    # must never be printed in the startup error, where it would end up in logs.
    model_config = SettingsConfigDict(
        env_prefix="BOUNCER_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = "postgresql+asyncpg://bouncer:bouncer@localhost:5432/bouncer"


class Settings(DatabaseSettings):
    """Gateway settings, read from environment variables prefixed BOUNCER_ (or a .env file)."""

    # Required, at least 32 characters: the gateway refuses to start without a strong admin token.
    admin_token: SecretStr = Field(min_length=32)

    redis_url: str = "redis://localhost:6379/0"
    # Short on purpose: a hanging Redis must not hang every request (DESIGN.md §2).
    redis_timeout_ms: int = 100
    rate_limit_window_seconds: float = 60.0

    upstream_base_url: str = "http://localhost:11434"
    upstream_connect_timeout: float = 5.0
    # Non-streaming: nothing arrives until generation is done, so this covers the whole generation.
    upstream_read_timeout: float = 300.0

    # Streaming timeouts (DESIGN.md §5). A cold model has to load into RAM before its first token.
    stream_first_token_timeout: float = 60.0
    stream_chunk_timeout: float = 30.0
    # Must stay below the concurrency cleanup age (DESIGN.md §2).
    stream_total_timeout: float = 300.0

    max_tokens_cap: int = 2048
    # Also the upper bound on prompt tokens: a token is always at least one byte.
    max_body_bytes: int = 32_768


@lru_cache
def get_settings() -> Settings:
    return Settings()
