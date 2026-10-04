import pytest

from validator.pgtypes import normalize_type


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("character varying(255)", "varchar(255)"),
        ("varchar(255)", "varchar(255)"),
        ("character varying", "varchar"),
        ("character(2)", "char(2)"),
        ("timestamp with time zone", "timestamptz"),
        ("timestamp without time zone", "timestamp"),
        ("time without time zone", "time"),
        ("time with time zone", "timetz"),
        ("serial", "integer"),
        ("bigserial", "bigint"),
        ("smallserial", "smallint"),
        ("int4", "integer"),
        ("int8", "bigint"),
        ("bool", "boolean"),
        ("numeric(10, 2)", "numeric(10,2)"),
        ("integer[]", "integer[]"),
        ("character varying(20)[]", "varchar(20)[]"),
        ("JSONB", "jsonb"),
        ("double precision", "double precision"),
        ("  text ", "text"),
    ],
)
def test_normalize_type(raw, expected):
    assert normalize_type(raw) == expected
