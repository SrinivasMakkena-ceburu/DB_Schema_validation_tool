"""Read-only introspection of a target PostgreSQL database.

One query per catalog kind for the whole schema, so cost does not grow with
the number of tables. The session is read-only at the server.
"""
import psycopg
from psycopg import sql

SESSION_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=30000"


class InspectError(Exception):
    pass


def connect(conninfo):
    try:
        return psycopg.connect(
            **conninfo,
            options=SESSION_OPTIONS,
            connect_timeout=10,
            autocommit=True,
            application_name="schemasync-validator",
        )
    except psycopg.Error as exc:
        raise InspectError(f"Cannot connect: {exc}") from exc


def check_connection(conninfo):
    with connect(conninfo) as conn:
        return conn.execute("SELECT version()").fetchone()[0]


_RELATIONS = """
SELECT c.relname, c.relkind
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %(schema)s AND c.relkind IN ('r', 'p', 'v', 'm')
  AND NOT c.relispartition
ORDER BY c.relname
"""

_COLUMNS = """
SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       (ad.adbin IS NOT NULL OR a.attidentity <> '' OR a.attgenerated <> '')
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_attrdef ad ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
WHERE n.nspname = %(schema)s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

_CONSTRAINTS = """
SELECT c.relname, con.contype,
  ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
        ORDER BY k.ord),
  fc.relname,
  ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum
        ORDER BY k.ord)
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_class fc ON fc.oid = con.confrelid
WHERE n.nspname = %(schema)s AND con.contype IN ('p', 'u', 'f')
ORDER BY c.relname, con.conname
"""

_INDEXES = """
SELECT t.relname, ix.indisunique, ix.indisprimary, ix.indpred IS NOT NULL,
  ARRAY(SELECT a.attname FROM unnest(ix.indkey::int2[]) WITH ORDINALITY k(attnum, ord)
        LEFT JOIN pg_attribute a ON a.attrelid = ix.indrelid AND a.attnum = k.attnum
        WHERE k.ord <= ix.indnkeyatts
        ORDER BY k.ord)
FROM pg_index ix
JOIN pg_class t ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = %(schema)s
ORDER BY t.relname
"""


def _add_unique(column_sets, cols):
    if cols not in column_sets:
        column_sets.append(cols)


def inspect_database(conninfo, schema="public"):
    """Return {tables, views, migrations, query_count} for one schema."""
    params = {"schema": schema}
    try:
        with connect(conninfo) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_namespace WHERE nspname = %(schema)s", params
            ).fetchone()
            if not exists:
                raise InspectError(f"Schema '{schema}' not found")

            tables, views = {}, []
            for name, kind in conn.execute(_RELATIONS, params):
                if kind in ("v", "m"):
                    views.append(name)
                else:
                    tables[name] = {"columns": {}, "pk": [], "uniques": [], "fks": [], "indexes": []}

            for table, column, type_, not_null, has_default in conn.execute(_COLUMNS, params):
                if table in tables:
                    tables[table]["columns"][column] = {
                        "type": type_,
                        "nullable": not not_null,
                        "has_default": has_default,
                    }

            for table, kind, cols, ref_table, ref_cols in conn.execute(_CONSTRAINTS, params):
                if table not in tables:
                    continue
                if kind == "p":
                    tables[table]["pk"] = list(cols)
                elif kind == "u":
                    _add_unique(tables[table]["uniques"], list(cols))
                else:
                    tables[table]["fks"].append(
                        {"columns": list(cols), "ref_table": ref_table, "ref_columns": list(ref_cols)}
                    )

            for table, unique, primary, partial, cols in conn.execute(_INDEXES, params):
                cols = list(cols)
                if table not in tables or primary or partial or None in cols:
                    continue
                target = tables[table]["uniques" if unique else "indexes"]
                _add_unique(target, cols)

            migrations = None
            has_table = conn.execute(
                "SELECT to_regclass(%s)", [f'"{schema}"."django_migrations"']
            ).fetchone()[0]
            query_count = 6
            if has_table:
                query = sql.SQL("SELECT app, name, applied FROM {}.django_migrations ORDER BY id").format(
                    sql.Identifier(schema)
                )
                migrations = [
                    {"app": app, "name": name, "applied": applied.isoformat() if applied else None}
                    for app, name, applied in conn.execute(query)
                ]
                query_count += 1
    except psycopg.Error as exc:
        raise InspectError(str(exc)) from exc

    return {"tables": tables, "views": views, "migrations": migrations, "query_count": query_count}
