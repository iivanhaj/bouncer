import logging
import uuid
from pathlib import Path

import anyio
import redis.asyncio as redis
from redis.exceptions import RedisError

from app import metrics
from app.errors import GatewayError, dependency_unavailable

logger = logging.getLogger(__name__)

_ACQUIRE_SCRIPT = (Path(__file__).parent / "lua" / "acquire_slot.lua").read_text()


class Slot:
    """One in-progress request's claim on its key's concurrency limit. Release exactly once, in a finally."""

    def __init__(self, client: redis.Redis, set_key: str, request_id: str):
        self._client = client
        self._set_key = set_key
        self._request_id = request_id
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        # Shielded: if the request is cancelled (e.g. server shutdown), anyio cancels every await
        # inside the cancelled scope, including this one, and the slot would leak until the stale
        # cleanup. (A normal client disconnect currently surfaces as an error, not a cancellation,
        # but cleanup must not depend on which.) Proven by test_release_still_runs_when_the_request_is_cancelled.
        with anyio.CancelScope(shield=True):
            try:
                await self._client.zrem(self._set_key, self._request_id)
            except RedisError:
                metrics.DEPENDENCY_ERRORS.labels("redis").inc()
                # Not fatal: the stale-entry cleanup in acquire_slot.lua frees it later.
                logger.warning("could not release concurrency slot %s", self._request_id)


class ConcurrencyLimiter:
    def __init__(self, client: redis.Redis, stale_seconds: float):
        self._client = client
        self._script = client.register_script(_ACQUIRE_SCRIPT)
        self._stale_ms = int(stale_seconds * 1000)

    async def acquire(self, key_id: uuid.UUID, limit: int, request_id: uuid.UUID) -> Slot:
        set_key = f"inflight:{key_id}"
        try:
            acquired, in_progress = await self._script(keys=[set_key], args=[self._stale_ms, limit, str(request_id)])
        except RedisError as err:
            metrics.DEPENDENCY_ERRORS.labels("redis").inc()
            raise dependency_unavailable() from err

        if not acquired:
            raise GatewayError(
                429,
                f"Too many requests in progress: at most {limit} at a time for this key.",
                "requests",
                "concurrency_limit_exceeded",
                headers={"Retry-After": "1"},
            )
        return Slot(self._client, set_key, str(request_id))
