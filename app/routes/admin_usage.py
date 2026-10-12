"""Read-only admin views over the usage log: what the dashboard shows."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import Float, and_, cast, func, or_, select, true

from app.auth import require_admin
from app.budget import headroom, month_of
from app.models import ApiKey, RequestLog

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class KeyUsage(BaseModel):
    key_id: uuid.UUID
    team_name: str
    key_prefix: str
    revoked: bool
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    avg_latency_ms: float | None
    p95_latency_ms: float | None
    avg_ttft_ms: float | None
    errors: int
    blocked: int
    # This calendar month, for the budget bar.
    month_tokens: int
    monthly_token_budget: int | None
    usable_budget: int | None


class UsagePoint(BaseModel):
    key_id: uuid.UUID
    bucket: datetime
    requests: int
    tokens: int
    cost_usd: Decimal
    p95_latency_ms: float | None


class UsageReport(BaseModel):
    since: datetime
    bucket: Literal["hour", "day"]
    keys: list[KeyUsage]
    series: list[UsagePoint]


@router.get("/usage")
async def usage(
    request: Request,
    days: int = Query(default=7, ge=1, le=365),
    bucket: Literal["hour", "day"] = "day",
    key_id: uuid.UUID | None = None,
) -> UsageReport:
    """Tokens, cost, latency and outcomes per key over the last `days`, plus a time series."""
    settings = request.app.state.settings
    since = datetime.now(timezone.utc) - timedelta(days=days)
    month_start, _ = month_of(datetime.now(timezone.utc))
    in_window = and_(RequestLog.created_at >= since, RequestLog.key_id == key_id if key_id else true())
    tokens = RequestLog.prompt_tokens + RequestLog.completion_tokens
    p95 = func.percentile_cont(0.95).within_group(RequestLog.latency_ms)

    totals = (
        select(
            RequestLog.key_id,
            func.count().label("requests"),
            func.coalesce(func.sum(RequestLog.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(RequestLog.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(RequestLog.cost_usd), 0).label("cost_usd"),
            cast(func.avg(RequestLog.latency_ms), Float).label("avg_latency_ms"),
            cast(p95, Float).label("p95_latency_ms"),
            cast(func.avg(RequestLog.ttft_ms), Float).label("avg_ttft_ms"),
            func.count().filter(RequestLog.status.notin_(["ok", "blocked_injection"])).label("errors"),
            func.count().filter(RequestLog.status == "blocked_injection").label("blocked"),
        )
        .where(in_window)
        .group_by(RequestLog.key_id)
        .subquery()
    )
    month = (
        select(RequestLog.key_id, func.sum(tokens).label("month_tokens"))
        .where(RequestLog.created_at >= month_start)
        .group_by(RequestLog.key_id)
        .subquery()
    )
    keys_query = (
        select(ApiKey, totals, month.c.month_tokens)
        .join(totals, totals.c.key_id == ApiKey.id)
        .outerjoin(month, month.c.key_id == ApiKey.id)
        .order_by(totals.c.requests.desc())
    )
    bucket_start = func.date_trunc(bucket, RequestLog.created_at).label("bucket")
    series_query = (
        select(
            RequestLog.key_id,
            bucket_start,
            func.count().label("requests"),
            func.coalesce(func.sum(tokens), 0).label("tokens"),
            func.coalesce(func.sum(RequestLog.cost_usd), 0).label("cost_usd"),
            cast(p95, Float).label("p95_latency_ms"),
        )
        .where(in_window)
        .group_by(RequestLog.key_id, bucket_start)
        .order_by(bucket_start)
    )

    async with request.app.state.sessionmaker() as session:
        key_rows = (await session.execute(keys_query)).all()
        series_rows = (await session.execute(series_query)).mappings().all()

    keys = []
    for row in key_rows:
        api_key: ApiKey = row[0]
        budget = api_key.monthly_token_budget
        keys.append(
            KeyUsage(
                key_id=api_key.id,
                team_name=api_key.team_name,
                key_prefix=api_key.key_prefix,
                revoked=api_key.revoked_at is not None,
                requests=row.requests,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                cost_usd=row.cost_usd,
                avg_latency_ms=row.avg_latency_ms,
                p95_latency_ms=row.p95_latency_ms,
                avg_ttft_ms=row.avg_ttft_ms,
                errors=row.errors,
                blocked=row.blocked,
                month_tokens=row.month_tokens or 0,
                monthly_token_budget=budget,
                usable_budget=budget - headroom(settings, api_key.max_concurrent) if budget else None,
            )
        )
    return UsageReport(since=since, bucket=bucket, keys=keys, series=[UsagePoint(**r) for r in series_rows])


class BlockedRequest(BaseModel):
    id: uuid.UUID
    created_at: datetime
    key_id: uuid.UUID
    team_name: str
    key_prefix: str
    model: str
    rules: list[str]
    score: float
    # False in monitor mode: the request would have been blocked but was let through.
    blocked: bool


@router.get("/blocked")
async def blocked(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    key_id: uuid.UUID | None = None,
) -> list[BlockedRequest]:
    """Requests blocked by injection detection, and (in monitor mode) ones that would have been.

    Only rule ids and scores are shown: prompts are never stored, so there is nothing else to show.
    """
    threshold = request.app.state.settings.injection_threshold
    query = (
        select(RequestLog, ApiKey.team_name, ApiKey.key_prefix)
        .join(ApiKey, ApiKey.id == RequestLog.key_id)
        .where(
            or_(RequestLog.status == "blocked_injection", RequestLog.injection_score >= threshold),
            RequestLog.key_id == key_id if key_id else true(),
        )
        .order_by(RequestLog.created_at.desc())
        .limit(limit)
    )
    async with request.app.state.sessionmaker() as session:
        rows = (await session.execute(query)).all()
    return [
        BlockedRequest(
            id=log.id,
            created_at=log.created_at,
            key_id=log.key_id,
            team_name=team_name,
            key_prefix=key_prefix,
            model=log.model,
            rules=log.blocked_rules or [],
            score=log.injection_score or 0.0,
            blocked=log.status == "blocked_injection",
        )
        for log, team_name, key_prefix in rows
    ]
