"""Canonical spelling of PostgreSQL type names.

Django's db_type() and PostgreSQL's format_type() spell the same type
differently; both sides are normalised before comparing.
"""
import re

_PREFIXES = [
    ("character varying", "varchar"),
    ("character", "char"),
    ("timestamp with time zone", "timestamptz"),
    ("timestamp without time zone", "timestamp"),
    ("time with time zone", "timetz"),
    ("time without time zone", "time"),
]
_ALIASES = {
    "serial": "integer",
    "serial4": "integer",
    "int": "integer",
    "int4": "integer",
    "bigserial": "bigint",
    "serial8": "bigint",
    "int8": "bigint",
    "smallserial": "smallint",
    "serial2": "smallint",
    "int2": "smallint",
    "bool": "boolean",
    "float8": "double precision",
    "float4": "real",
    "decimal": "numeric",
}


def normalize_type(name):
    text = re.sub(r"\s+", " ", (name or "").strip().lower())
    array = ""
    while text.endswith("[]"):
        array += "[]"
        text = text[:-2].strip()
    base, args = text, ""
    match = re.match(r"^(.*?)\s*(\(.*\))$", text)
    if match:
        base, args = match.group(1), match.group(2).replace(" ", "")
    for prefix, short in _PREFIXES:
        if base == prefix:
            base = short
            break
    base = _ALIASES.get(base, base)
    return f"{base}{args}{array}"
