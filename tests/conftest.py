import pytest
from alembic import command
from alembic.config import Config

from tests.helpers import TEST_DATABASE_URL, run_sql


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    """Build the test schema with the real migrations, so the tests also prove the migrations work."""
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.downgrade(config, "base")
    command.upgrade(config, "head")


@pytest.fixture(autouse=True)
def clean_tables():
    run_sql("TRUNCATE api_keys")


@pytest.fixture
def anyio_backend():
    return "asyncio"
