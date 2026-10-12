import asyncio
import os

import asyncpg

from app.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "BOUNCER_TEST_DATABASE_URL", "postgresql+asyncpg://bouncer:bouncer@localhost:5432/bouncer_test"
)
TEST_REDIS_URL = os.environ.get("BOUNCER_TEST_REDIS_URL", "redis://localhost:6379/1")
ADMIN_TOKEN ="test-admin-token-0123456789abcdefghijklmnop"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
UPSTREAM = "http://upstream.test"
UPSTREAM_URL = f"{UPSTREAM}/v1/chat/completions"


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "admin_token": ADMIN_TOKEN,
        "upstream_base_url": UPSTREAM,
    }
    values.update(overrides)
    return Settings(**values)


def run_sql(sql: str, *args) -> None:
    """Run one statement against the test database, outside the app (for setup and test tricks)."""

    async def run():
        connection = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
        try:
            await connection.execute(sql, *args)
        finally:
            await connection.close()

    asyncio.run(run())


def create_key(client, **fields) -> dict:
    """Create an API key through the admin API and return the response body (including the key)."""
    response = client.post("/admin/keys", json={"team_name": "team-a", **fields}, headers=ADMIN_HEADERS)
    assert response.status_code == 201, response.text
    return response.json()
