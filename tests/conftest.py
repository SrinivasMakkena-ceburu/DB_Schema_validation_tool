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


@pytest.fixture(autouse=True)
def _inline_jobs(settings, tmp_path):
    """Background jobs run inline in tests; backups go to a temp folder."""
    settings.JOBS_INLINE = True
    settings.BACKUP_DIR = tmp_path / "backups"


SAMPLE_DATA = [
    "INSERT INTO devices_customer (id, name) VALUES (1, 'Acme'), (2, 'Other'), (3, 'Protected'), (4, 'Premium')",
    "INSERT INTO devices_premiumcustomer (customer_ptr_id, tier) VALUES (4, 'gold')",
    "INSERT INTO devices_operator (id, customer_id, email, password) VALUES (1, 1, 'a@acme', 'pw1'), (2, 2, 'b@other', 'pw2')",
    "INSERT INTO devices_device (id, customer_id, parent_id, hostname) VALUES"
    " (10, 1, NULL, 'acme-root'), (11, 1, 10, 'acme-child'), (12, 1, 11, 'acme-grandchild'), (20, 2, NULL, 'other-1')",
    "INSERT INTO devices_execution (id, device_id, customer_id, operator_id, started, status) VALUES"
    " (100, 10, 1, 1, now() - interval '200 days', 'ok'),"
    " (101, 11, 1, 2, now() - interval '5 days', 'failed'),"
    " (102, 20, 2, 1, now() - interval '120 days', 'ok')",
    "INSERT INTO devices_contract (id, customer_id, code) VALUES (1, 3, 'C-3')",
    "INSERT INTO devices_auditentry (id, device_id, customer_id) VALUES (1, 10, 1), (2, 20, 2)",
    "INSERT INTO devices_note (id, content_type_id, object_id, text) SELECT 1, id, 1, 'about acme'"
    " FROM django_content_type WHERE app_label = 'devices' AND model = 'customer'",
    "INSERT INTO devices_note (id, content_type_id, object_id, text) SELECT 2, id, 2, 'about other'"
    " FROM django_content_type WHERE app_label = 'devices' AND model = 'customer'",
    "INSERT INTO devices_group (id, name) VALUES (1, 'all')",
    "INSERT INTO devices_group_devices (group_id, device_id) VALUES (1, 10), (1, 20)",
]


def migrate_sample(conninfo):
    import subprocess
    import sys
    from pathlib import Path

    sample = Path(__file__).resolve().parent / "sample_project"
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "sampleproj.settings", "SAMPLE_SECRET_KEY": "x",
           "SAMPLE_DB_NAME": conninfo["dbname"], "SAMPLE_DB_HOST": conninfo["host"],
           "SAMPLE_DB_PORT": str(conninfo["port"]), "SAMPLE_DB_USER": conninfo["user"],
           "SAMPLE_DB_PASSWORD": conninfo["password"]}
    proc = subprocess.run([sys.executable, "manage.py", "migrate", "--noinput"], cwd=sample, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr


def _pg_available():
    try:
        psycopg.connect(**PG_ADMIN, connect_timeout=3).close()
        return True
    except psycopg.Error:
        return False


@pytest.fixture(scope="session")
def _sample_template():
    """Migrate the sample project once; tests clone it with CREATE DATABASE … TEMPLATE."""
    if not _pg_available():
        pytest.skip("PostgreSQL not available")
    name = f"schemasync_tpl_{uuid.uuid4().hex[:10]}"
    pg_execute(PG_ADMIN, f'CREATE DATABASE "{name}"')
    info = {**PG_ADMIN, "dbname": name}
    migrate_sample(info)
    pg_execute(info, SAMPLE_DATA)
    yield name
    pg_execute(PG_ADMIN, f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def sample_db(_sample_template):
    """The sample project migrated into a fresh database, with SAMPLE_DATA loaded."""
    name = f"schemasync_test_{uuid.uuid4().hex[:10]}"
    pg_execute(PG_ADMIN, f'CREATE DATABASE "{name}" TEMPLATE "{_sample_template}"')
    yield {**PG_ADMIN, "dbname": name}
    pg_execute(PG_ADMIN, f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def sample_extracted(tmp_path_factory):
    import json
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    out = tmp_path_factory.mktemp("extract") / "out.json"
    proc = subprocess.run(
        [sys.executable, str(root / "validator" / "extractor.py"), "--settings", "sampleproj.settings",
         "--output", str(out)],
        cwd=root / "tests" / "sample_project", env={**os.environ, "SAMPLE_SECRET_KEY": "x"},
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text())


@pytest.fixture
def sample_db2(_sample_template):
    """A second, independent copy of the sample database."""
    name = f"schemasync_test_{uuid.uuid4().hex[:10]}"
    pg_execute(PG_ADMIN, f'CREATE DATABASE "{name}" TEMPLATE "{_sample_template}"')
    yield {**PG_ADMIN, "dbname": name}
    pg_execute(PG_ADMIN, f'DROP DATABASE "{name}" WITH (FORCE)')
