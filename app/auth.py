import hmac

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, or_, select

from app import keys, metrics
from app.errors import GatewayError
from app.models import ApiKey

# auto_error=False: we raise our own OpenAI-style 401 instead of FastAPI's default 403.
bearer = HTTPBearer(auto_error=False)


def _invalid_api_key() -> GatewayError:
    metrics.REJECTED.labels("unauthorized").inc()
    # One message for missing, malformed, unknown, revoked and expired keys, so an attacker
    # can't tell which keys exist or were revoked (DESIGN.md §1).
    return GatewayError(401, "Invalid API key", "invalid_request_error", "invalid_api_key")


async def require_api_key(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
) -> ApiKey:
    if credentials is None or not keys.is_well_formed(credentials.credentials):
        raise _invalid_api_key()

    # Look up by hash on a unique index. Timing can only leak information about the hash,
    # which doesn't help anyone build a key that produces it.
    query = select(ApiKey).where(
        ApiKey.key_hash == keys.hash_key(credentials.credentials),
        ApiKey.revoked_at.is_(None),
        or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > func.now()),
    )
    # A short-lived session, not a request-long dependency: a streaming response can last
    # minutes, and holding a pooled connection that long would exhaust the pool.
    async with request.app.state.sessionmaker() as session:
        api_key = await session.scalar(query)

    if api_key is None:
        raise _invalid_api_key()
    return api_key


async def require_admin(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    expected = request.app.state.settings.admin_token.get_secret_value()
    # compare_digest takes the same time whether or not the strings match, so response timing
    # can't reveal how much of a guessed admin token was right.
    if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), expected.encode()):
        raise GatewayError(401, "Invalid admin token", "invalid_request_error", "invalid_admin_token")
