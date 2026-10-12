import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
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
