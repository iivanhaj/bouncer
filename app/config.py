from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Gateway settings, read from environment variables prefixed BOUNCER_ (or a .env file)."""

    model_config = SettingsConfigDict(env_prefix="BOUNCER_", env_file=".env", extra="ignore")

    upstream_base_url: str = "http://localhost:11434"
    upstream_connect_timeout: float = 5.0
    # Non-streaming: nothing arrives until generation is done, so this covers the whole generation.
    upstream_read_timeout: float = 300.0

    max_tokens_cap: int = 2048


@lru_cache
def get_settings() -> Settings:
    return Settings()
