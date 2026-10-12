import math
import uuid
from dataclasses import dataclass
from pathlib import Path

import redis.asyncio as redis
from fastapi import Depends, Request
from redis.exceptions import RedisError

from app.auth import require_api_key
from app.errors import GatewayError, dependency_unavailable
from app.models import ApiKey

_SLIDING_WINDOW_SCRIPT = (Path(__file__).parent / "lua" / "sliding_window.lua").read_text()


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int

    def headers(self) -> dict[str, str]:
        # OpenAI's header names, so existing clients' retry logic works unchanged.
        headers = {
            "x-ratelimit-limit-requests": str(self.limit),
            "x-ratelimit-remaining-requests": str(self.remaining),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.retry_after_seconds)
            headers["x-ratelimit-reset-requests"] = f"{self.retry_after_seconds}s"
        return headers


class RateLimiter:
    def __init__(self, client: redis.Redis, window_seconds: float):
        # register_script sends the script by its hash (EVALSHA) and uploads it only if Redis doesn't have it.
        self._script = client.register_script(_SLIDING_WINDOW_SCRIPT)
        self._window_ms = int(window_seconds * 1000)

    async def hit(self, key_id: uuid.UUID, limit: int, request_id: uuid.UUID) -> RateLimitResult:
        allowed, remaining, retry_after_ms = await self._script(
            keys=[f"ratelimit:{key_id}"], args=[self._window_ms, limit, str(request_id)]
        )
        return RateLimitResult(
            allowed=bool(allowed),
            limit=limit,
            remaining=int(remaining),
            retry_after_seconds=0 if allowed else max(1, math.ceil(int(retry_after_ms) / 1000)),
        )


@dataclass(frozen=True)
class Admission:
    """Everything known about a request once it has passed auth and the rate limit."""

    request_id: uuid.UUID
    api_key: ApiKey
    rate_limit: RateLimitResult


async def enforce_rate_limit(request: Request, api_key: ApiKey = Depends(require_api_key)) -> Admission:
    """Runs right after auth and before the body is validated (DESIGN.md pipeline steps 2-3)."""
    request_id = uuid.uuid4()
    try:
        result = await request.app.state.rate_limiter.hit(api_key.id, api_key.rpm_limit, request_id)
    except RedisError as err:
        # Fail closed: a security gateway must not become unlimited when Redis is down (DESIGN.md §2).
        raise dependency_unavailable() from err

    if not result.allowed:
        raise GatewayError(
            429,
            f"Rate limit reached: {result.limit} requests per minute. Retry after {result.retry_after_seconds}s.",
            "requests",
            "rate_limit_exceeded",
            headers=result.headers(),
        )
    return Admission(request_id=request_id, api_key=api_key, rate_limit=result)
