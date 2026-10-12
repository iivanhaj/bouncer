"""app.migrate: several instances migrating at once must take turns, not race."""

import os
import subprocess
import sys
from pathlib import Path

from tests.helpers import TEST_DATABASE_URL, fetch_rows

ROOT = Path(__file__).resolve().parent.parent


def test_concurrent_migrations_take_turns_and_all_succeed():
    # Start from an empty schema so the first instance really has work to do.
    env = {**os.environ, "BOUNCER_DATABASE_URL": TEST_DATABASE_URL}
    subprocess.run([sys.executable, "-m", "alembic", "downgrade", "base"], cwd=ROOT, env=env, check=True)

    try:
        processes = [
            subprocess.Popen(
                [sys.executable, "-m", "app.migrate"],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for _ in range(3)
        ]
        outputs = [p.communicate(timeout=120)[0] for p in processes]
    finally:
        # Whatever happened, leave the schema at head: an empty schema would make every later
        # test fail with "relation does not exist" and hide the real failure.
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True)

    assert [p.returncode for p in processes] == [0, 0, 0], outputs
    assert all("migration lock acquired" in out for out in outputs)
    tables = {row["tablename"] for row in fetch_rows("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"api_keys", "requests", "alembic_version"} <= tables
