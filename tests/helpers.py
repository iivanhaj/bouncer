import asyncio
import os
from pathlib import Path

import asyncpg

from app.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "MODELGUARD_TEST_DATABASE_URL", "postgresql+asyncpg://modelguard:modelguard@localhost:5432/modelguard_test"
)
TEST_REDIS_URL = os.environ.get("MODELGUARD_TEST_REDIS_URL", "redis://localhost:6379/1")
ADMIN_TOKEN = "test-admin-token-0123456789abcdefghijklmnop"
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
UPSTREAM = "http://upstream.test"
UPSTREAM_URL = f"{UPSTREAM}/v1/chat/completions"
TEST_MODELS_FILE = Path(__file__).parent / "models.test.yaml"


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "admin_token": ADMIN_TOKEN,
        "upstreams": {"test": UPSTREAM},
        "models_file": TEST_MODELS_FILE,
        "metrics_port": None,
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


def fetch_rows(sql: str, *args) -> list[dict]:
    """Read rows from the test database, outside the app."""

    async def run():
        connection = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
        try:
            return [dict(row) for row in await connection.fetch(sql, *args)]
        finally:
            await connection.close()

    return asyncio.run(run())


def create_key(client, **fields) -> dict:
    """Create an API key through the admin API and return the response body (including the key)."""
    response = client.post("/admin/keys", json={"team_name": "team-a", **fields}, headers=ADMIN_HEADERS)
    assert response.status_code == 201, response.text
    return response.json()
