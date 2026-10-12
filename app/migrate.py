"""Run database migrations safely when several gateway instances start at once.

    python -m app.migrate

In Kubernetes every gateway pod runs this as an init container. Without coordination, two pods
starting together would both run `alembic upgrade head` and race to create the same tables.
A Postgres advisory lock makes them take turns: the first migrates, the others wait, then find
the schema already at head and do nothing.
"""

import asyncio
import subprocess
import sys

import asyncpg

from app.config import DatabaseSettings

# Any fixed number works, as long as every instance uses the same one.
_MIGRATION_LOCK_ID = 7_331_001


async def main() -> int:
    dsn = DatabaseSettings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(dsn)
    try:
        # Held on this connection while Alembic migrates over its own connection. Session-level,
        # so it is released when this connection closes, even if the process crashes.
        await connection.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
        print("migration lock acquired", flush=True)
        # The same interpreter, not the `alembic` command, which isn't on PATH everywhere
        # (e.g. a virtualenv's Python started directly).
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "alembic", "upgrade", "head"])
        return result.returncode
    finally:
        await connection.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
