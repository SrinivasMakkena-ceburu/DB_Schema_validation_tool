"""Work out exactly which rows a delete reaches, inside the caller's transaction.

Rows are tracked by (tableoid, ctid) — ctid alone repeats across the partitions
of a partitioned table — in two temporary tables (pg_temp._ss_del and
pg_temp._ss_null, dropped at commit/rollback). The set grows round by round:
round N follows every relation out of the rows added in round N-1, so cycles
and rows reachable through several paths are handled (each row is added once).
The executor runs the same plan inside its own transaction and then acts on
these temp tables, so what is backed up and deleted is exactly what was counted.
"""
from psycopg import sql

from .cascade import BLOCKING, CASCADE, GENERIC, ORPHAN, PARENT_LINK, PROTECT, SET_NULL

MAX_DEPTH = 100
DEL = sql.SQL("pg_temp._ss_del")
NUL = sql.SQL("pg_temp._ss_null")


class PlanError(Exception):
    pass


def qualified(graph, table):
    return sql.Identifier(graph.schema, table)


def _deleted_parents(graph, rel, depth=None):
    """SELECT of the parent column values of deleted parent rows (optionally one round only)."""
    depth_filter = sql.SQL(" AND d.depth = {}").format(sql.Literal(depth)) if depth is not None else sql.SQL("")
    return sql.SQL(
        "SELECT p.{pcol} FROM {parent} p JOIN {del} d ON d.tbl = {ptbl} AND d.toid = p.tableoid "
        "AND d.rid = p.ctid{depth}"
    ).format(pcol=sql.Identifier(rel.parent_column), parent=qualified(graph, rel.parent_table),
             ptbl=sql.Literal(rel.parent_table), depth=depth_filter, **{"del": DEL})


def _not_deleted(alias, table):
    return sql.SQL("NOT EXISTS (SELECT 1 FROM {del} x WHERE x.tbl = {tbl} AND x.toid = {alias}.tableoid "
                   "AND x.rid = {alias}.ctid)").format(
        tbl=sql.Literal(table), alias=sql.Identifier(alias), **{"del": DEL})


def _referencing(graph, rel, *, depth=None, content_type_id=None):
    """FROM/WHERE selecting child rows that reference deleted parent rows."""
    if rel.action == GENERIC:
        match = sql.SQL("c.{ct} = {ct_id} AND CAST(c.{col} AS text) IN (SELECT CAST(q.v AS text) FROM ({parents}) q(v))").format(
            ct=sql.Identifier(rel.ct_column), ct_id=sql.Literal(content_type_id), col=sql.Identifier(rel.child_column),
            parents=_deleted_parents(graph, rel, depth))
    else:
        match = sql.SQL("c.{col} IN ({parents})").format(col=sql.Identifier(rel.child_column),
                                                          parents=_deleted_parents(graph, rel, depth))
    return sql.SQL("FROM {child} c WHERE {match}").format(child=qualified(graph, rel.child_table), match=match)


def _content_type_ids(conn, graph):
    if "django_content_type" not in graph.tables:
        return {}
    rows = conn.execute(sql.SQL("SELECT app_label, model, id FROM {}").format(
        qualified(graph, "django_content_type"))).fetchall()
    return {(app, model): ct_id for app, model, ct_id in rows}


def _expand(conn, graph, rel, depth, ct_ids):
    """Add rows reached through `rel` from the rows added in round `depth`."""
    if rel.action == PARENT_LINK:
        query = sql.SQL(
            "INSERT INTO {del} (tbl, toid, rid, depth, via) SELECT {ptbl}, p.tableoid, p.ctid, {d}, {via} "
            "FROM {parent} p WHERE p.{pcol} IN (SELECT c.{ccol} FROM {child} c JOIN {del} d ON d.tbl = {ctbl} "
            "AND d.toid = c.tableoid AND d.rid = c.ctid AND d.depth = {depth}) AND {fresh}"
        ).format(ptbl=sql.Literal(rel.parent_table), d=sql.Literal(depth + 1), via=sql.Literal(rel.id),
                 parent=qualified(graph, rel.parent_table), pcol=sql.Identifier(rel.parent_column),
                 ccol=sql.Identifier(rel.child_column), child=qualified(graph, rel.child_table),
                 ctbl=sql.Literal(rel.child_table), depth=sql.Literal(depth),
                 fresh=_not_deleted("p", rel.parent_table), **{"del": DEL})
    else:
        query = sql.SQL("INSERT INTO {del} (tbl, toid, rid, depth, via) "
                        "SELECT {ctbl}, c.tableoid, c.ctid, {d}, {via} {ref} AND {fresh}").format(
            ctbl=sql.Literal(rel.child_table), d=sql.Literal(depth + 1), via=sql.Literal(rel.id),
            ref=_referencing(graph, rel, depth=depth, content_type_id=ct_ids.get(rel.content_type)),
            fresh=_not_deleted("c", rel.child_table), **{"del": DEL})
    return conn.execute(query).rowcount


def _source_table(rel):
    return rel.child_table if rel.action == PARENT_LINK else rel.parent_table


def plan(conn, graph, root_table, where, *, limit=None, samples=5):
    """Compute the delete set for rows of `root_table` matching `where` (a psycopg Composable).

    Must run inside a transaction (conn.autocommit False); the caller commits or
    rolls back. Leaves pg_temp._ss_del / _ss_null populated for the executor.
    """
    if conn.autocommit:
        raise PlanError("plan() must run inside a transaction")
    if root_table not in graph.tables:
        raise PlanError(f"Table {root_table} is not in this database")

    conn.execute("DROP TABLE IF EXISTS pg_temp._ss_del, pg_temp._ss_null")
    conn.execute("CREATE TEMP TABLE _ss_del (tbl text NOT NULL, toid oid NOT NULL, rid tid NOT NULL, "
                 "depth int NOT NULL, via text) ON COMMIT DROP")
    conn.execute("CREATE INDEX ON pg_temp._ss_del (tbl, toid, rid)")
    conn.execute("CREATE TEMP TABLE _ss_null (tbl text NOT NULL, toid oid NOT NULL, rid tid NOT NULL, "
                 "col text NOT NULL, via text NOT NULL) ON COMMIT DROP")

    limit_sql = sql.SQL(" LIMIT {}").format(sql.Literal(int(limit))) if limit else sql.SQL("")
    root_count = conn.execute(sql.SQL(
        "INSERT INTO {del} (tbl, toid, rid, depth) SELECT {tbl}, tableoid, ctid, 0 FROM {root} WHERE {where}{limit}"
    ).format(tbl=sql.Literal(root_table), root=qualified(graph, root_table), where=where, limit=limit_sql,
             **{"del": DEL})).rowcount

    ct_ids = _content_type_ids(conn, graph)
    skipped = list(graph.skipped)
    followed = []
    for rel in graph.relations:
        if rel.action == GENERIC and rel.content_type not in ct_ids:
            skipped.append(f"{rel.id}: content type {'.'.join(rel.content_type)} not registered")
        elif rel.action in (CASCADE, PARENT_LINK, GENERIC):
            followed.append(rel)

    depth = 0
    while True:
        sources = {row[0] for row in conn.execute(
            sql.SQL("SELECT DISTINCT tbl FROM {} WHERE depth = %s").format(DEL), [depth])}
        if not sources:
            break
        if depth >= MAX_DEPTH:
            raise PlanError(f"Cascade is deeper than {MAX_DEPTH} levels")
        for rel in followed:
            if _source_table(rel) in sources:
                _expand(conn, graph, rel, depth, ct_ids)
        depth += 1

    deleted_tables = {row[0] for row in conn.execute(sql.SQL("SELECT DISTINCT tbl FROM {}").format(DEL))}
    touched = [rel for rel in graph.relations if rel.parent_table in deleted_tables and rel.action != PARENT_LINK]

    blockers, orphans = [], []
    for rel in touched:
        if rel.action == SET_NULL:
            if graph.tables[rel.child_table]["columns"][rel.child_column]["nullable"]:
                conn.execute(sql.SQL("INSERT INTO {nul} (tbl, toid, rid, col, via) "
                                     "SELECT {ctbl}, c.tableoid, c.ctid, {col}, {via} {ref} AND {fresh}").format(
                    nul=NUL, ctbl=sql.Literal(rel.child_table), col=sql.Literal(rel.child_column),
                    via=sql.Literal(rel.id), ref=_referencing(graph, rel), fresh=_not_deleted("c", rel.child_table)))
                continue
            blockers.append(_blocker(conn, graph, rel, samples, note="on_delete=SET_NULL but the column is NOT NULL "
                                                                      "in this database"))
        elif rel.action in BLOCKING:
            blocker = _blocker(conn, graph, rel, samples)
            if blocker:
                blockers.append(blocker)
        elif rel.action == ORPHAN:
            orphan = _blocker(conn, graph, rel, samples)
            if orphan:
                orphans.append(orphan)
    blockers = [b for b in blockers if b]
    blockers += _hidden_fk_blockers(conn, graph, deleted_tables, samples)

    return {
        "root_table": root_table,
        "root_count": root_count,
        "total_rows": conn.execute(sql.SQL("SELECT count(*) FROM {}").format(DEL)).fetchone()[0],
        "tables": _table_summary(conn, graph, samples),
        "nulls": [
            {"table": t, "column": c, "count": n, "relation": via}
            for t, c, via, n in conn.execute(sql.SQL(
                "SELECT tbl, col, via, count(DISTINCT (toid, rid)) FROM {} GROUP BY tbl, col, via ORDER BY tbl, col"
            ).format(NUL))
        ],
        "blockers": blockers,
        "orphans": orphans,
        "skipped": skipped,
        "depth": depth,
    }


def _blocker(conn, graph, rel, samples, note=""):
    # PROTECT blocks even when the referencing row is itself being deleted (Django semantics).
    condition = sql.SQL("") if rel.action == PROTECT else sql.SQL(" AND {}").format(_not_deleted("c", rel.child_table))
    ref = _referencing(graph, rel)
    count = conn.execute(sql.SQL("SELECT count(*) {}{}").format(ref, condition)).fetchone()[0]
    if not count:
        return None
    rows = [r[0] for r in conn.execute(sql.SQL("SELECT row_to_json(c) {}{} LIMIT {}").format(
        ref, condition, sql.Literal(samples)))]
    return {"relation": rel.id, "action": rel.action, "source": rel.source, "note": note or rel.note,
            "table": rel.child_table, "count": count, "samples": rows}


_INCOMING_FKS = """
SELECT cn.nspname, c.relname, con.confdeltype,
  ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(n, o)
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n ORDER BY k.o),
  p.relname,
  ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(n, o)
        JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n ORDER BY k.o)
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace cn ON cn.oid = c.relnamespace
JOIN pg_class p ON p.oid = con.confrelid JOIN pg_namespace pn ON pn.oid = p.relnamespace
WHERE con.contype = 'f' AND con.conparentid = 0 AND NOT c.relispartition
  AND pn.nspname = %(schema)s AND p.relname = ANY(%(tables)s)
ORDER BY 1, 2
"""
_HIDDEN_NOTES = {
    "c": "ON DELETE CASCADE in the database, on a foreign key the planner cannot follow "
         "(another schema or several columns): those rows would be deleted without preview or backup",
    "n": "ON DELETE SET NULL in the database, on a foreign key the planner cannot follow "
         "(another schema or several columns)",
}


def _hidden_fk_blockers(conn, graph, deleted_tables, samples):
    """Foreign keys into the delete set that graph.relations does not cover (other schemas, composite keys)."""
    if not deleted_tables:
        return []
    followed = {(rel.child_table, rel.child_column, rel.parent_table) for rel in graph.relations}
    out = []
    for nsp, child, action, cols, parent, pcols in conn.execute(
            _INCOMING_FKS, {"schema": graph.schema, "tables": sorted(deleted_tables)}):
        if nsp == graph.schema and len(cols) == 1 and (child, cols[0], parent) in followed:
            continue
        ref = sql.SQL(
            "FROM {child} c WHERE ({ccols}) IN (SELECT {pcols} FROM {parent} p JOIN {del} d ON d.tbl = {ptbl} "
            "AND d.toid = p.tableoid AND d.rid = p.ctid)"
        ).format(child=sql.Identifier(nsp, child),
                 ccols=sql.SQL(", ").join(sql.SQL("c.{}").format(sql.Identifier(x)) for x in cols),
                 pcols=sql.SQL(", ").join(sql.SQL("p.{}").format(sql.Identifier(x)) for x in pcols),
                 parent=qualified(graph, parent), ptbl=sql.Literal(parent), **{"del": DEL})
        count = conn.execute(sql.SQL("SELECT count(*) {}").format(ref)).fetchone()[0]
        if not count:
            continue
        table = child if nsp == graph.schema else f"{nsp}.{child}"
        rows = [r[0] for r in conn.execute(sql.SQL("SELECT row_to_json(c) {} LIMIT {}").format(
            ref, sql.Literal(samples)))]
        out.append({
            "relation": f"{table}({', '.join(cols)}) -> {parent}({', '.join(pcols)})",
            "action": "block", "source": "db-hidden", "table": table, "count": count, "samples": rows,
            "note": _HIDDEN_NOTES.get(action, "database foreign key the planner cannot follow "
                                              "(another schema or several columns)"),
        })
    return out


def _table_summary(conn, graph, samples):
    via = {}
    for tbl, rel_id, count in conn.execute(sql.SQL(
            "SELECT tbl, via, count(*) FROM {} WHERE via IS NOT NULL GROUP BY tbl, via ORDER BY via").format(DEL)):
        via.setdefault(tbl, []).append({"relation": rel_id, "count": count})
    actions = {rel.id: rel.action for rel in graph.relations}
    out = []
    for tbl, count, depth, max_depth in conn.execute(sql.SQL(
            "SELECT tbl, count(*), min(depth), max(depth) FROM {} GROUP BY tbl ORDER BY min(depth), tbl").format(DEL)):
        rows = [r[0] for r in conn.execute(sql.SQL(
            "SELECT row_to_json(t) FROM {table} t WHERE (t.tableoid, t.ctid) IN "
            "(SELECT toid, rid FROM {del} WHERE tbl = {tbl} LIMIT {n})"
        ).format(table=qualified(graph, tbl), tbl=sql.Literal(tbl), n=sql.Literal(samples), **{"del": DEL}))]
        out.append({
            "table": tbl, "count": count, "depth": depth, "max_depth": max_depth,
            "via": [{**v, "action": actions.get(v["relation"], "")} for v in via.get(tbl, [])],
            "samples": rows,
        })
    return out
