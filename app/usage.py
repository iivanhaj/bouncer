import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.errors import GatewayError
from app.models import RequestLog
from app.models_registry import ModelRoute

logger = logging.getLogger(__name__)

# GatewayError code -> requests.status. Anything not listed is an upstream failure.
_STATUS_BY_CODE = {
    "model_not_found": "bad_request",
    "max_tokens_too_large": "bad_request",
    "invalid_request": "bad_request",
    "concurrency_limit_exceeded": "concurrency_limited",
    "insufficient_quota": "budget_exceeded",
    "prompt_blocked": "blocked_injection",
    "dependency_unavailable": "unavailable",
    "upstream_timeout": "upstream_timeout",
}


def status_for(error: GatewayError) -> str:
    return _STATUS_BY_CODE.get(error.code or "", "upstream_error")


@dataclass
class UsageRecord:
    """Everything logged about one request, filled in as the request goes through the pipeline."""

    id: uuid.UUID
    key_id: uuid.UUID
    model: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started: float = field(default_factory=time.monotonic)

    upstream: str | None = None
    route: ModelRoute | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_estimated: bool = False
    ttft_ms: int | None = None
    status: str = "ok"
    blocked_rules: list[str] | None = None
    injection_score: float | None = None
    redaction_counts: dict[str, int] | None = None
    finished: bool = False

    def set_usage(self, prompt_tokens: int, completion_tokens: int, estimated: bool) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.usage_estimated = estimated

    def mark_first_token(self, at: float) -> None:
        self.ttft_ms = round((at - self.started) * 1000)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_row(self) -> dict[str, Any]:
        cost = self.route.cost(self.prompt_tokens, self.completion_tokens) if self.route else Decimal(0)
        return {
            "id": self.id,
            "key_id": self.key_id,
            "model": self.model,
            "upstream": self.upstream,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "usage_estimated": self.usage_estimated,
            "cost_usd": cost,
            "latency_ms": round((time.monotonic() - self.started) * 1000),
            "ttft_ms": self.ttft_ms,
            "status": self.status,
            "blocked_rules": self.blocked_rules,
            "injection_score": self.injection_score,
            "redaction_counts": self.redaction_counts,
            "created_at": self.started_at,
        }


class UsageRecorder:
    """Writes usage rows to Postgres in batches, in the background (DESIGN.md §6).

    Requests only put a row on an in-memory queue, so logging never adds latency. If the queue
    is full (Postgres slow or down), the row is dropped and counted: losing a log row is better
    than making users wait. Rows still in memory are lost if the process crashes; budget
    enforcement doesn't depend on this log, so that's accepted.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        *,
        max_queue: int = 10_000,
        batch_size: int = 100,
        flush_interval: float = 1.0,
    ):
        self._sessionmaker = sessionmaker
        # None is a wake-up signal from stop(), never a row.
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=max_queue)
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._closing = False
        self._task: asyncio.Task | None = None
        self.dropped = 0

    def record(self, record: UsageRecord) -> None:
        try:
            self._queue.put_nowait(record.to_row())
        except asyncio.QueueFull:
            self.dropped += 1
            logger.warning("usage log queue full, dropped row for request %s", record.id)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Flush everything still queued, then stop. Called on shutdown."""
        self._closing = True
        # Wake the writer if it's waiting for rows, instead of letting shutdown wait out the
        # flush interval. If the queue is full, the writer isn't waiting anyway.
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._task:
            await self._task

    async def _run(self) -> None:
        while True:
            batch = await self._next_batch()
            if batch:
                await self._write(batch)
            if self._closing and self._queue.empty():
                return

    async def _next_batch(self) -> list[dict[str, Any]]:
        """Wait up to flush_interval, returning early once batch_size rows are ready."""
        batch: list[dict[str, Any]] = []
        deadline = time.monotonic() + self._flush_interval
        while len(batch) < self._batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                row = await asyncio.wait_for(self._queue.get(), remaining)
            except TimeoutError:
                break
            if row is None:  # the wake-up from stop()
                break
            batch.append(row)
        return batch

    async def _write(self, batch: list[dict[str, Any]]) -> None:
        try:
            async with self._sessionmaker() as session:
                await session.execute(insert(RequestLog), batch)
                await session.commit()
        except Exception:
            # Never let a database problem kill the writer loop.
            self.dropped += len(batch)
            logger.exception("failed to write %d usage rows", len(batch))
