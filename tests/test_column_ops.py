import gzip
import json

import psycopg
import pytest

from tests.conftest import pg_execute
from validator.column_ops import drop_column_restore_script, execute_drop_column, plan_drop_column
from validator.executor import OperationError


@pytest.fixture
def legacy(pg_conninfo):
    pg_execute(pg_conninfo, [
        "CREATE TABLE thing (id int PRIMARY KEY, name text, legacy_code varchar(20), old_flag boolean)",
        "INSERT INTO thing VALUES (1, 'a', 'X1', true), (2, 'b', NULL, false), (3, 'c', 'X3', NULL)",
        "CREATE INDEX thing_legacy_idx ON thing (legacy_code)",
        "ALTER TABLE thing ADD CONSTRAINT legacy_len CHECK (length(legacy_code) < 10)",
    ])
    return pg_conninfo


def columns(conninfo):
    with psycopg.connect(**conninfo) as conn:
        return [r[0] for r in conn.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = 'thing'::regclass AND attnum > 0 "
            "AND NOT attisdropped ORDER BY attnum")]


def test_plan_lists_dependents(legacy):
    result = plan_drop_column(legacy, "public", "thing", "legacy_code")
    assert result["type"] == "character varying(20)"
    assert result["non_null"] == 2
    assert result["rows"] == 3
    assert sorted(result["samples"]) == ["X1", "X3"]
    assert result["indexes"] == ["thing_legacy_idx"]
    assert result["constraints"] == ["legacy_len"]
    assert result["views"] == []
    assert result["pk"] == ["id"]
    assert result["in_model"] is None  # no project given


def test_plan_flags_column_used_by_model(legacy):
    extracted = {"models": [{"db_table": "thing", "columns": [{"name": "legacy_code"}]}]}
    assert plan_drop_column(legacy, "public", "thing", "legacy_code", extracted)["in_model"] is True
    assert plan_drop_column(legacy, "public", "thing", "old_flag", extracted)["in_model"] is False


def test_dependent_view_blocks(legacy, tmp_path):
    pg_execute(legacy, "CREATE VIEW thing_codes AS SELECT legacy_code FROM thing")
    result = plan_drop_column(legacy, "public", "thing", "legacy_code")
    assert result["views"] == ["thing_codes"]
    with pytest.raises(OperationError, match="thing_codes"):
        execute_drop_column(legacy, "public", "thing", "legacy_code", backup_dir=tmp_path / "op", timeout_s=30, log=[])
    assert "legacy_code" in columns(legacy)


def test_unknown_column(legacy):
    with pytest.raises(OperationError, match="not found"):
        plan_drop_column(legacy, "public", "thing", "nope")


def test_execute_backs_up_and_restore_brings_back(legacy, tmp_path):
    log = []
    result = execute_drop_column(legacy, "public", "thing", "legacy_code", backup_dir=tmp_path / "op",
                                 timeout_s=30, log=log)
    assert result["rows_backed_up"] == 3
    assert "legacy_code" not in columns(legacy)
    assert any("DROP COLUMN" in line for line in log)
    with gzip.open(tmp_path / "op" / "column.jsonl.gz", "rt") as fh:
        assert {json.loads(line)["legacy_code"] for line in fh} == {"X1", None, "X3"}

    script = "".join(drop_column_restore_script(tmp_path / "op"))
    with psycopg.connect(**legacy, autocommit=True) as conn:
        conn.execute(script)
        assert conn.execute("SELECT id, legacy_code FROM thing ORDER BY id").fetchall() == [
            (1, "X1"), (2, None), (3, "X3")]


def test_restore_recreates_index_and_constraint(legacy, tmp_path):
    execute_drop_column(legacy, "public", "thing", "legacy_code", backup_dir=tmp_path / "op", timeout_s=30, log=[])
    with psycopg.connect(**legacy, autocommit=True) as conn:
        assert not conn.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'thing_legacy_idx'").fetchone()
        conn.execute("".join(drop_column_restore_script(tmp_path / "op")))
        assert conn.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'thing_legacy_idx'").fetchone()
        assert conn.execute("SELECT 1 FROM pg_constraint WHERE conname = 'legacy_len'").fetchone()
