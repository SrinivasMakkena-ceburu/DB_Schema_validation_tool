import os
import uuid

import psycopg
import pytest

# Local PostgreSQL used by integration tests; they are skipped when it is absent.
PG_ADMIN = {
    "host": os.environ.get("SCHEMASYNC_TEST_PGHOST", "127.0.0.1"),
    "port": int(os.environ.get("SCHEMASYNC_TEST_PGPORT", "5432")),
    "user": os.environ.get("SCHEMASYNC_TEST_PGUSER", "postgres"),
    "password": os.environ.get("SCHEMASYNC_TEST_PGPASSWORD", "postgres"),
    "dbname": "postgres",
}


def pg_execute(conninfo, statements):
    with psycopg.connect(**conninfo, autocommit=True) as conn:
        for statement in statements if isinstance(statements, list) else [statements]:
            conn.execute(statement)


@pytest.fixture
def pg_conninfo():
    try:
        psycopg.connect(**PG_ADMIN, connect_timeout=3).close()
    except psycopg.Error as exc:
        pytest.skip(f"PostgreSQL not available: {exc}")
    name = f"schemasync_test_{uuid.uuid4().hex[:10]}"
    pg_execute(PG_ADMIN, f'CREATE DATABASE "{name}"')
    yield {**PG_ADMIN, "dbname": name}
    pg_execute(PG_ADMIN, f'DROP DATABASE "{name}" WITH (FORCE)')
