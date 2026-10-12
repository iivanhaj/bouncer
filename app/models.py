import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    team_name: Mapped[str] = mapped_column(String(100))
    # First characters of the key, for display only. Never used to authenticate.
    key_prefix: Mapped[str] = mapped_column(String(16))
    # SHA-256 of the full key, hex encoded. The key itself is never stored.
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    rpm_limit: Mapped[int]
    max_concurrent: Mapped[int]
    # NULL means unlimited.
    monthly_token_budget: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # NULL means the key never expires.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Soft delete: revoked keys keep their row so usage history still points at them.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RequestLog(Base):
    """One row per request that got past the rate limit (DESIGN.md §6).

    Deliberately has no column for prompt or answer text: raw prompts are never stored.
    """

    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_key_id_created_at", "key_id", "created_at"),
        Index("ix_requests_status_created_at", "status", "created_at"),
    )

    # The same id the client sees in the x-request-id header.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    key_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("api_keys.id"))
    model: Mapped[str] = mapped_column(String(100))
    upstream: Mapped[str | None] = mapped_column(String(50))
    prompt_tokens: Mapped[int]
    completion_tokens: Mapped[int]
    # True when the upstream didn't report usage and the gateway estimated it.
    usage_estimated: Mapped[bool]
    # Calculated when the row is written, at that moment's price.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 8))
    latency_ms: Mapped[int]
    ttft_ms: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(32))
    blocked_rules: Mapped[list[str] | None] = mapped_column(ARRAY(String(64)))
    injection_score: Mapped[float | None]
    # e.g. {"EMAIL": 2}: what was redacted, never the values.
    # none_as_null: Python None must become SQL NULL, not the JSON value null.
    redaction_counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # When the request started (not when the row was written).
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
