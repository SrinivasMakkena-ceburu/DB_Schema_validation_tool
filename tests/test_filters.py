import psycopg
import pytest
from django.http import QueryDict
from psycopg import sql

from tests.conftest import pg_execute
from validator.filters import FilterError, build_where, describe, parse_filters, validate_filters

COLUMNS = {"id": "integer", "name": "text", "amount": "numeric(10,2)",
           "created": "timestamp with time zone", "flag": "boolean"}


@pytest.fixture
def conn(pg_conninfo):
    pg_execute(pg_conninfo, [
        "CREATE TABLE t (id int PRIMARY KEY, name text, amount numeric(10,2), created timestamptz, flag boolean)",
        "INSERT INTO t VALUES (1, 'alpha', 10, now() - interval '100 days', true),"
        " (2, 'beta_1', 20, now() - interval '10 days', NULL),"
        " (3, '50% off', 30, now(), false)",
    ])
    with psycopg.connect(**pg_conninfo, autocommit=True) as c:
        yield c


def ids(conn, filters):
    query = sql.SQL("SELECT id FROM t WHERE {} ORDER BY id").format(build_where(COLUMNS, filters))
    return [r[0] for r in conn.execute(query)]


@pytest.mark.parametrize("filters, expected", [
    ([], [1, 2, 3]),
    ([{"column": "id", "op": "eq", "value": "2"}], [2]),
    ([{"column": "id", "op": "ne", "value": "2"}], [1, 3]),
    ([{"column": "amount", "op": "lt", "value": "20"}], [1]),
    ([{"column": "amount", "op": "le", "value": "20"}], [1, 2]),
    ([{"column": "amount", "op": "gt", "value": "20"}], [3]),
    ([{"column": "amount", "op": "ge", "value": "20"}], [2, 3]),
    ([{"column": "id", "op": "in", "value": "1, 3"}], [1, 3]),
    ([{"column": "flag", "op": "isnull", "value": ""}], [2]),
    ([{"column": "flag", "op": "notnull", "value": ""}], [1, 3]),
    ([{"column": "name", "op": "contains", "value": "ALP"}], [1]),
    ([{"column": "created", "op": "older_than_days", "value": "30"}], [1]),
    ([{"column": "created", "op": "newer_than_days", "value": "30"}], [2, 3]),
    ([{"column": "amount", "op": "ge", "value": "20"}, {"column": "flag", "op": "notnull", "value": ""}], [3]),
])
def test_operators(conn, filters, expected):
    assert ids(conn, filters) == expected


def test_contains_escapes_wildcards(conn):
    assert ids(conn, [{"column": "name", "op": "contains", "value": "%"}]) == [3]
    assert ids(conn, [{"column": "name", "op": "contains", "value": "_"}]) == [2]


def test_injection_is_literal(conn):
    assert ids(conn, [{"column": "name", "op": "eq", "value": "x'; DROP TABLE t;--"}]) == []
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3


def test_unknown_column_and_operator():
    with pytest.raises(FilterError, match="Unknown column"):
        build_where(COLUMNS, [{"column": "nope", "op": "eq", "value": "1"}])
    with pytest.raises(FilterError, match="Unknown operator"):
        build_where(COLUMNS, [{"column": "id", "op": "like", "value": "1"}])


def test_value_required():
    with pytest.raises(FilterError, match="needs a value"):
        build_where(COLUMNS, [{"column": "id", "op": "eq", "value": " "}])


def test_days_only_for_dates_and_numbers():
    with pytest.raises(FilterError, match="date/time"):
        build_where(COLUMNS, [{"column": "name", "op": "older_than_days", "value": "3"}])
    with pytest.raises(FilterError, match="whole number of days"):
        build_where(COLUMNS, [{"column": "created", "op": "older_than_days", "value": "soon"}])


def test_bad_value_for_type(conn):
    with pytest.raises(FilterError, match="not valid for id"):
        validate_filters(conn, sql.Identifier("t"), COLUMNS, [{"column": "id", "op": "eq", "value": "abc"}])


def test_parse_and_describe():
    qd = QueryDict("f_col=id&f_op=eq&f_val=2&f_col=&f_op=eq&f_val=&f_col=created&f_op=older_than_days&f_val=90")
    filters = parse_filters(qd)
    assert filters == [{"column": "id", "op": "eq", "value": "2"},
                       {"column": "created", "op": "older_than_days", "value": "90"}]
    assert describe(filters) == "id = 2 and created older than 90 days"
    assert describe([]) == "all rows"
