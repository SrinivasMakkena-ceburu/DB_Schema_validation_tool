"""Structured row filters: (column, operator, value) -> a safe SQL WHERE clause.

There is no free-form SQL anywhere in the tool. Columns must exist in the
table, operators come from a fixed list, values are quoted literals cast to
the column's type.
"""
import re

import psycopg
from psycopg import sql

OPERATORS = {
    "eq": "=",
    "ne": "≠",
    "lt": "<",
    "le": "≤",
    "gt": ">",
    "ge": "≥",
    "in": "in (comma separated)",
    "contains": "contains",
    "isnull": "is empty (NULL)",
    "notnull": "is not empty",
    "older_than_days": "older than N days",
    "newer_than_days": "newer than N days",
}
_COMPARISONS = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
_NO_VALUE = {"isnull", "notnull"}
# format_type() output we are willing to splice in as a cast target.
_SAFE_TYPE = re.compile(r"^[a-z][a-z0-9_ ]*(\(\d+(,\s*\d+)?\))?(\[\])*$")


class FilterError(ValueError):
    pass


def _is_temporal(type_name):
    return any(word in type_name for word in ("timestamp", "date", "time"))


def _cast(value, type_name):
    if _SAFE_TYPE.match(type_name or ""):
        return sql.SQL("CAST({} AS {})").format(sql.Literal(value), sql.SQL(type_name))
    return sql.Literal(value)


def _column_expr(column, type_name):
    ident = sql.Identifier(column)
    # Unusual types (enums, domains in other schemas) are compared as text.
    return ident if _SAFE_TYPE.match(type_name or "") else sql.SQL("CAST({} AS text)").format(ident)


def _condition(columns, f):
    column, op, value = f.get("column", ""), f.get("op", ""), str(f.get("value", "")).strip()
    if column not in columns:
        raise FilterError(f"Unknown column: {column}")
    if op not in OPERATORS:
        raise FilterError(f"Unknown operator: {op}")
    type_name = columns[column]
    col = sql.Identifier(column)
    if op == "isnull":
        return sql.SQL("{} IS NULL").format(col)
    if op == "notnull":
        return sql.SQL("{} IS NOT NULL").format(col)
    if not value:
        raise FilterError(f"Filter on {column} needs a value")
    if op in _COMPARISONS:
        return sql.SQL("{} {} {}").format(_column_expr(column, type_name), sql.SQL(_COMPARISONS[op]),
                                          _cast(value, type_name))
    if op == "in":
        items = [v.strip() for v in value.split(",") if v.strip()]
        target = f"{type_name}[]" if _SAFE_TYPE.match(type_name) and "[]" not in type_name else "text[]"
        return sql.SQL("{} = ANY(CAST({} AS {}))").format(_column_expr(column, type_name), sql.Literal(items),
                                                         sql.SQL(target))
    if op == "contains":
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return sql.SQL("CAST({} AS text) ILIKE {}").format(col, sql.Literal(f"%{escaped}%"))
    # older_than_days / newer_than_days
    if not _is_temporal(type_name):
        raise FilterError(f"'{OPERATORS[op]}' only works on date/time columns; {column} is {type_name}")
    if not value.isdigit():
        raise FilterError(f"'{OPERATORS[op]}' needs a whole number of days, got {value!r}")
    compare = "<" if op == "older_than_days" else ">="
    return sql.SQL("{} {} now() - make_interval(days => {})").format(col, sql.SQL(compare), sql.Literal(int(value)))


def build_where(columns, filters):
    """columns: {name: format_type}; filters: [{column, op, value}] (AND-ed)."""
    if not filters:
        return sql.SQL("TRUE")
    return sql.SQL(" AND ").join(sql.SQL("({})").format(_condition(columns, f)) for f in filters)


def validate_filters(conn, table_ident, columns, filters):
    """Raise FilterError when a value cannot be used with its column's type."""
    where = build_where(columns, filters)
    try:
        with conn.transaction():
            conn.execute(sql.SQL("SELECT 1 FROM {} WHERE {} LIMIT 1").format(table_ident, where))
    except psycopg.errors.DataError as exc:
        detail = str(exc).splitlines()[0]
        raise FilterError(f"A value is not valid for {_guess_column(filters, exc) or 'its column'}: {detail}") from exc
    return where


def _guess_column(filters, exc):
    text = str(exc)
    for f in filters:
        if f.get("value") and f["value"] in text:
            return f["column"]
    return filters[0]["column"] if len(filters) == 1 else ""


def parse_filters(querydict):
    rows = zip(querydict.getlist("f_col"), querydict.getlist("f_op"), querydict.getlist("f_val"))
    return [{"column": c, "op": o, "value": v} for c, o, v in rows if c]


def describe(filters):
    if not filters:
        return "all rows"
    parts = []
    for f in filters:
        op = f["op"]
        if op in _NO_VALUE:
            parts.append(f"{f['column']} {OPERATORS[op]}")
        elif op in ("older_than_days", "newer_than_days"):
            parts.append(f"{f['column']} {op.split('_')[0]} than {f['value']} days")
        elif op in _COMPARISONS:
            parts.append(f"{f['column']} {OPERATORS[op]} {f['value']}")
        else:
            parts.append(f"{f['column']} {op} {f['value']}")
    return " and ".join(parts)
