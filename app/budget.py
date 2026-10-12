"""Monthly token budgets with headroom: a hard limit that is never exceeded (DESIGN.md §2).

The gateway has to decide before a request runs, but only learns its cost afterwards. So it
stops accepting new requests `headroom` tokens before the real budget: every request that
passed the check but hasn't been counted yet holds a concurrency slot, so at most
max_concurrent of them exist, and each one is bounded in size. Their tokens always fit
inside the headroom.
"""

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import anyio
import redis.asyncio as redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import metrics
from app.config import Settings
from app.errors import GatewayError, dependency_unavailable
from app.models import RequestLog

logger = logging.getLogger(__name__)

_SETTLE_SCRIPT = (Path(__file__).parent / "lua" / "settle_budget.lua").read_text()
# Counters expire on their own once their month is long over.
_COUNTER_TTL_SECONDS = 40 * 24 * 3600


def headroom(settings: Settings, max_concurrent: int) -> int:
    """Tokens reserved so requests already in progress can finish inside the budget.

    Per request: the prompt is bounded by the body size limit (a token is at least one byte, and
    JSON framing outweighs the chat template's extra tokens), the completion by MAX_TOKENS_CAP,
    plus a margin because upstreams can report a token or so past the cap (Ollama 0.4.1 reported
    16 completion tokens for max_tokens=15).
    """
    per_request = settings.max_body_bytes + settings.max_tokens_cap + settings.budget_token_margin
    return max_concurrent * per_request


def month_of(moment: datetime) -> tuple[datetime, datetime]:
    """Start and end of the UTC calendar month containing `moment`."""
    start = moment.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def counter_key(key_id: uuid.UUID, moment: datetime) -> str:
    # The month is part of the name, so a new month starts a new counter: no reset job needed.
    return f"budget:{key_id}:{month_of(moment)[0]:%Y-%m}"


class BudgetEnforcer:
    def __init__(self, client: redis.Redis, sessionmaker: async_sessionmaker, settings: Settings):
        self._client = client
        self._settle = client.register_script(_SETTLE_SCRIPT)
        self._sessionmaker = sessionmaker
        self._settings = settings

    async def spent(self, key_id: uuid.UUID, moment: datetime) -> int:
        """Tokens used this month. Rebuilt from the usage log if Redis has lost the counter."""
        key = counter_key(key_id, moment)
        value = await self._client.get(key)
        if value is None:
            # Postgres is the record of truth; Redis is a fast copy. NX: if several requests
            # rebuild at once, the first write wins and the others don't overwrite it.
            await self._client.set(key, await self._spent_from_log(key_id, moment), nx=True, ex=_COUNTER_TTL_SECONDS)
            value = await self._client.get(key)
        return int(value)

    async def _spent_from_log(self, key_id: uuid.UUID, moment: datetime) -> int:
        start, end = month_of(moment)
        query = select(func.coalesce(func.sum(RequestLog.prompt_tokens + RequestLog.completion_tokens), 0)).where(
            RequestLog.key_id == key_id, RequestLog.created_at >= start, RequestLog.created_at < end
        )
        async with self._sessionmaker() as session:
            return int(await session.scalar(query))

    async def check(self, key_id: uuid.UUID, budget: int | None, max_concurrent: int, moment: datetime) -> None:
        """Raise 429 insufficient_quota if the key has used its usable budget. None means unlimited."""
        if budget is None:
            return
        try:
            spent = await self.spent(key_id, moment)
        except RedisError as err:
            metrics.DEPENDENCY_ERRORS.labels("redis").inc()
            # Fail closed, like the rate limiter.
            raise dependency_unavailable() from err

        usable = budget - headroom(self._settings, max_concurrent)
        if spent >= usable:
            raise GatewayError(
                429,
                f"Monthly token budget reached: {spent} of {usable} usable tokens used "
                f"(budget {budget}, of which {budget - usable} is reserved for requests in progress).",
                "insufficient_quota",
                "insufficient_quota",
            )

    async def settle(self, key_id: uuid.UUID, tokens: int, moment: datetime) -> None:
        """Add a finished request's tokens to the counter. Never raises."""
        if tokens <= 0:
            return
        # Shielded for the same reason as Slot.release: a cancelled request must still be counted.
        with anyio.CancelScope(shield=True):
            try:
                await self._settle(keys=[counter_key(key_id, moment)], args=[tokens])
            except RedisError:
                metrics.DEPENDENCY_ERRORS.labels("redis").inc()
                # The usage log still has the row, so the next rebuild will include it.
                logger.warning("could not add %d tokens to the budget counter of key %s", tokens, key_id)
