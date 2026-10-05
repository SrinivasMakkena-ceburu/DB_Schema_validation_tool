"""Drop a column: preview, backup (primary key + value), drop without CASCADE, restore script."""
import gzip
import json
import shutil
from pathlib import Path

import psycopg
from django.utils import timezone
from psycopg import sql

from .db_inspector import InspectError, connect
from .executor import OperationError, _dollar, _read_lines, connect_for_writes

_COLUMN = """
SELECT c.oid, a.attnum, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       pg_get_expr(ad.adbin, ad.adrelid)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = %(column)s AND NOT a.attisdropped AND a.attnum > 0
LEFT JOIN pg_attrdef ad ON ad.adrelid = c.oid AND ad.adnum = a.attnum
WHERE n.nspname = %(schema)s AND c.relname = %(table)s AND c.relkind IN ('r', 'p')
"""
_INDEXES = """
SELECT i.relname, pg_get_indexdef(ix.indexrelid) FROM pg_index ix JOIN pg_class i ON i.oid = ix.indexrelid
WHERE ix.indrelid = %(oid)s AND %(attnum)s = ANY(ix.indkey::int2[])
  AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = ix.indexrelid)
ORDER BY 1
"""
_CONSTRAINTS = """
SELECT conname, pg_get_constraintdef(oid), contype FROM pg_constraint
WHERE conrelid = %(oid)s AND %(attnum)s = ANY(conkey) ORDER BY 1
"""
_REFERENCED_BY = """
SELECT conrelid::regclass::text || ' (' || conname || ')' FROM pg_constraint
WHERE confrelid = %(oid)s AND %(attnum)s = ANY(confkey) ORDER BY 1
"""
_VIEWS = """
SELECT DISTINCT v.relname FROM pg_depend d
JOIN pg_rewrite r ON r.oid = d.objid JOIN pg_class v ON v.oid = r.ev_class
WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = %(oid)s AND d.refobjsubid = %(attnum)s
  AND v.oid <> %(oid)s
ORDER BY 1
"""
_PK = """
SELECT a.attname FROM pg_constraint k
JOIN unnest(k.conkey) WITH ORDINALITY u(attnum, ord) ON TRUE
JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.attnum
WHERE k.conrelid = %(oid)s AND k.contype = 'p' ORDER BY u.ord
"""


def _describe(conn, schema, table, column, extracted=None, samples=5):
    row = conn.execute(_COLUMN, {"schema": schema, "table": table, "column": column}).fetchone()
    if not row:
        raise OperationError(f"Column {table}.{column} not found in schema {schema}")
    oid, attnum, type_name, not_null, default = row
    params = {"oid": oid, "attnum": attnum}
    ident = sql.Identifier(schema, table)
    col = sql.Identifier(column)
    rows, non_null = conn.execute(sql.SQL("SELECT count(*), count({}) FROM {}").format(col, ident)).fetchone()
    constraints = conn.execute(_CONSTRAINTS, params).fetchall()
    pk = [r[0] for r in conn.execute(_PK, params)]
    info = {
        "schema": schema, "table": table, "column": column, "type": type_name, "not_null": not_null,
        "default": default, "rows": rows, "non_null": non_null,
        "samples": [r[0] for r in conn.execute(sql.SQL(
            "SELECT DISTINCT CAST({col} AS text) FROM {t} WHERE {col} IS NOT NULL LIMIT {n}").format(
            col=col, t=ident, n=sql.Literal(samples)))],
        "indexes": [r[0] for r in conn.execute(_INDEXES, params)],
        "index_defs": [r[1] for r in conn.execute(_INDEXES, params)],
        "constraints": [name for name, _, kind in constraints if kind != "p"],
        "constraint_defs": [[name, definition] for name, definition, kind in constraints if kind != "p"],
        "referenced_by": [r[0] for r in conn.execute(_REFERENCED_BY, params)],
        "views": [r[0] for r in conn.execute(_VIEWS, params)],
        "pk": pk,
        "in_model": None,
    }
    if extracted is not None:
        model = next((m for m in extracted["models"] if m["db_table"] == table), None)
        info["in_model"] = bool(model and any(c["name"] == column for c in model["columns"]))
    blockers = [f"view {v} depends on it" for v in info["views"]]
    blockers += [f"foreign key {r} references it" for r in info["referenced_by"]]
    if column in pk:
        blockers.append("it is part of the primary key")
    if not pk:
        blockers.append("the table has no primary key, so the values could not be restored")
    info["blockers"] = blockers
    return info


def plan_drop_column(conninfo, schema, table, column, extracted=None):
    try:
        with connect(conninfo) as conn:
            return _describe(conn, schema, table, column, extracted)
    except InspectError as exc:
        raise OperationError(str(exc)) from exc


def execute_drop_column(conninfo, schema, table, column, *, backup_dir, timeout_s, log):
    backup_dir = Path(backup_dir)
    with connect_for_writes(conninfo, timeout_s) as conn:
        try:
            info = _describe(conn, schema, table, column)
            if info["blockers"]:
                raise OperationError("Cannot drop column: " + "; ".join(info["blockers"]))
            backup_dir.mkdir(parents=True, exist_ok=False)
            fields = sql.SQL(", ").join(sql.SQL("{}, x.{}").format(sql.Literal(c), sql.Identifier(c))
                                        for c in info["pk"] + [column])
            query = sql.SQL("COPY (SELECT json_build_object({})::text FROM {} x) TO STDOUT").format(
                fields, sql.Identifier(schema, table))
            backed_up = 0
            with gzip.open(backup_dir / "column.jsonl.gz", "wt", encoding="utf-8") as fh, \
                    conn.cursor().copy(query) as copy:
                for (line,) in copy.rows():
                    fh.write(line + "\n")
                    backed_up += 1
            (backup_dir / "manifest.json").write_text(json.dumps(
                {**info, "kind": "drop_column", "created": timezone.now().isoformat(), "rows_backed_up": backed_up},
                indent=2))
            drop = sql.SQL("ALTER TABLE {} DROP COLUMN {}").format(sql.Identifier(schema, table),
                                                                  sql.Identifier(column))
            log.append(drop.as_string(conn))
            conn.execute(drop)
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            shutil.rmtree(backup_dir, ignore_errors=True)
            if isinstance(exc, psycopg.Error):
                raise OperationError(str(exc).strip()) from exc
            raise
    return {"rows_backed_up": backed_up, "backup": str(backup_dir)}


def drop_column_restore_script(backup_dir):
    backup_dir = Path(backup_dir)
    m = json.loads((backup_dir / "manifest.json").read_text())
    target = f'"{m["schema"]}"."{m["table"]}"'
    col = f'"{m["column"]}"'
    match = " AND ".join(f'{target}."{c}" = r."{c}"' for c in m["pk"])
    yield f"-- Schema Sync restore script: re-create {m['table']}.{m['column']} and its values\n"
    yield "BEGIN;\n"
    yield f"ALTER TABLE {target} ADD COLUMN {col} {m['type']};\n"
    for line in _read_lines(backup_dir / "column.jsonl.gz"):
        if json.loads(line).get(m["column"]) is None:
            continue
        yield f"UPDATE {target} SET {col} = r.{col} FROM json_populate_record(NULL::{target}, {_dollar(line)}) r WHERE {match};\n"
    if m["default"]:
        yield f"ALTER TABLE {target} ALTER COLUMN {col} SET DEFAULT {m['default']};\n"
    if m["not_null"]:
        yield f"ALTER TABLE {target} ALTER COLUMN {col} SET NOT NULL;\n"
    for definition in m["index_defs"]:
        yield f"{definition};\n"
    for name, definition in m["constraint_defs"]:
        yield f'ALTER TABLE {target} ADD CONSTRAINT "{name}" {definition};\n'
    yield "COMMIT;\n"
