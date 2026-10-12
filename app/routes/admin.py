import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app import keys
from app.auth import require_admin
from app.budget import headroom
from app.errors import GatewayError
from app.models import ApiKey

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


class CreateKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_name: str = Field(min_length=1, max_length=100)
    rpm_limit: int = Field(default=60, ge=1, le=100_000)
    max_concurrent: int = Field(default=4, ge=1, le=1_000)
    monthly_token_budget: int | None = Field(default=None, ge=1)
    expires_in_days: int | None = Field(default=None, ge=1, le=3_650)


class KeyInfo(BaseModel):
    """What the admin API shows about a key. Never includes the key or its hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    team_name: str
    key_prefix: str
    rpm_limit: int
    max_concurrent: int
    monthly_token_budget: int | None
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


class CreatedKey(KeyInfo):
    # The full key, returned exactly once, at creation.
    key: str


@router.post("/keys", status_code=201)
async def create_key(body: CreateKeyRequest, request: Request) -> CreatedKey:
    settings = request.app.state.settings

    if body.monthly_token_budget is not None:
        minimum = 10 * headroom(settings, body.max_concurrent)
        if body.monthly_token_budget < minimum:
            raise GatewayError(
                400,
                f"monthly_token_budget must be at least {minimum} with max_concurrent={body.max_concurrent}, "
                "so the reserved headroom stays under 10% of the budget",
                "invalid_request_error",
                "budget_too_small",
            )

    key = keys.generate_key()
    api_key = ApiKey(
        team_name=body.team_name,
        key_prefix=keys.display_prefix(key),
        key_hash=keys.hash_key(key),
        rpm_limit=body.rpm_limit,
        max_concurrent=body.max_concurrent,
        monthly_token_budget=body.monthly_token_budget,
        expires_at=(
            datetime.now(timezone.utc) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
        ),
    )
    async with request.app.state.sessionmaker() as session:
        session.add(api_key)
        await session.commit()
        await session.refresh(api_key)

    return CreatedKey(**KeyInfo.model_validate(api_key).model_dump(), key=key)


@router.get("/keys")
async def list_keys(request: Request) -> list[KeyInfo]:
    async with request.app.state.sessionmaker() as session:
        rows = await session.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))
        return [KeyInfo.model_validate(row) for row in rows]


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(key_id: uuid.UUID, request: Request) -> Response:
    async with request.app.state.sessionmaker() as session:
        api_key = await session.get(ApiKey, key_id)
        if api_key is None:
            raise GatewayError(404, "API key not found", "invalid_request_error", "key_not_found")
        # Revoking twice keeps the original revocation time.
        if api_key.revoked_at is None:
            api_key.revoked_at = datetime.now(timezone.utc)
            await session.commit()
    return Response(status_code=204)
