"""Run a planned delete: rehearse (rolled back) or execute (backed up, verified, committed).

Execution re-plans inside its own transaction so the rows backed up and
deleted are exactly the rows counted; if those counts differ from the
preview the user confirmed, nothing is changed.
"""
import gzip
import json
import secrets
import shutil
import time
from pathlib import Path

import psycopg
from django.utils import timezone
from psycopg import sql

from .planner import DEL, NUL, plan, qualified


class OperationError(Exception):
    pass


def connect_for_writes(conninfo, timeout_s):
    try:
        return psycopg.connect(
            **conninfo,
            connect_timeout=10,
            application_name="schemasync-operation",
            options=f"-c lock_timeout=5000 -c statement_timeout={int(timeout_s) * 1000}",
        )
    except psycopg.Error as exc:
        raise OperationError(f"Cannot connect for writing: {exc}") from exc


def connect_for_preview(conninfo, timeout_s):
    """A session for previews: may create temp tables, and the caller always rolls back."""
    try:
        return psycopg.connect(
            **conninfo,
            connect_timeout=10,
            application_name="schemasync-preview",
            options=f"-c lock_timeout=5000 -c statement_timeout={int(timeout_s) * 1000}",
        )
    except psycopg.Error as exc:
        raise OperationError(f"Cannot connect: {exc}") from exc


def plan_signature(plan_result):
    """What the user confirmed: rows deleted per table and columns nulled."""
    return {
        "delete": {t["table"]: t["count"] for t in plan_result["tables"]},
        "null": {f"{n['table']}.{n['column']}": n["count"] for n in plan_result["nulls"]},
    }


def _deletion_order(conn):
    # Deepest first; deferred foreign keys make the order irrelevant for Django's
    # constraints, it matters only for non-deferrable database-only ones.
    return [r[0] for r in conn.execute(sql.SQL(
        "SELECT tbl FROM {} GROUP BY tbl ORDER BY max(depth) DESC, tbl").format(DEL))]


def _apply(conn, graph, log):
    """Null, then delete, using the temp tables left by plan(). Returns counts."""
    conn.execute("SET CONSTRAINTS ALL DEFERRED")
    counts = {"deleted": {}, "nulled": {}}
    null_cols = {}
    for tbl, col in conn.execute(sql.SQL("SELECT DISTINCT tbl, col FROM {} ORDER BY tbl, col").format(NUL)):
        null_cols.setdefault(tbl, []).append(col)
    for tbl, cols in null_cols.items():
        # One UPDATE per table: an UPDATE moves the row, so its old ctid is only valid once.
        assignments = sql.SQL(", ").join(
            sql.SQL("{col} = CASE WHEN ctid IN (SELECT rid FROM {nul} WHERE tbl = {tbl} AND col = {c}) "
                    "THEN NULL ELSE {col} END").format(col=sql.Identifier(c), nul=NUL, tbl=sql.Literal(tbl),
                                                       c=sql.Literal(c))
            for c in cols)
        query = sql.SQL("UPDATE {table} SET {assignments} WHERE ctid IN (SELECT rid FROM {nul} WHERE tbl = {tbl})").format(
            table=qualified(graph, tbl), assignments=assignments, nul=NUL, tbl=sql.Literal(tbl))
        log.append(query.as_string(conn))
        conn.execute(query)
        for c in cols:
            counts["nulled"][f"{tbl}.{c}"] = conn.execute(sql.SQL(
                "SELECT count(DISTINCT rid) FROM {} WHERE tbl = %s AND col = %s").format(NUL), [tbl, c]).fetchone()[0]
    for tbl in _deletion_order(conn):
        planned = conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE tbl = %s").format(DEL), [tbl]).fetchone()[0]
        query = sql.SQL("DELETE FROM {table} WHERE ctid IN (SELECT rid FROM {del} WHERE tbl = {tbl})").format(
            table=qualified(graph, tbl), tbl=sql.Literal(tbl), **{"del": DEL})
        log.append(query.as_string(conn))
        deleted = conn.execute(query).rowcount
        if deleted != planned:
            raise OperationError(f"{tbl}: planned {planned} rows but deleted {deleted}; rows changed meanwhile")
        counts["deleted"][tbl] = deleted
    # Check deferred foreign keys now, so a rehearsal sees what a commit would.
    conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
    return counts


def _blocked_message(plan_result):
    return "Operation blocked: " + "; ".join(
        f"{b['count']} row(s) via {b['relation']} ({b['action']})" for b in plan_result["blockers"])


def rehearse(conninfo, graph, root_table, where, *, timeout_s, limit=None):
    """Plan and run the delete, then roll back. Never changes data."""
    started = time.monotonic()
    with connect_for_writes(conninfo, timeout_s) as conn:
        try:
            plan_result = plan(conn, graph, root_table, where, limit=limit, samples=0)
            if plan_result["blockers"]:
                return {"ok": False, "error": _blocked_message(plan_result), "counts": {},
                        "duration_ms": int((time.monotonic() - started) * 1000)}
            counts = _apply(conn, graph, [])
            return {"ok": True, "error": "", "counts": counts,
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        except (psycopg.Error, OperationError) as exc:
            return {"ok": False, "error": str(exc).strip(), "counts": {},
                    "duration_ms": int((time.monotonic() - started) * 1000)}
        finally:
            conn.rollback()


def _write_backup(conn, graph, plan_result, backup_dir):
    backup_dir.mkdir(parents=True, exist_ok=False)
    manifest = {"schema": graph.schema, "created": timezone.now().isoformat(), "tables": {}, "nulls": []}
    for t in plan_result["tables"]:
        tbl = t["table"]
        query = sql.SQL("COPY (SELECT row_to_json(x)::text FROM {table} x WHERE x.ctid IN "
                        "(SELECT rid FROM {del} WHERE tbl = {tbl})) TO STDOUT").format(
            table=qualified(graph, tbl), tbl=sql.Literal(tbl), **{"del": DEL})
        rows = _copy_to_file(conn, query, backup_dir / f"{tbl}.jsonl.gz")
        manifest["tables"][tbl] = {"rows": rows, "depth": t["max_depth"], "pk": graph.tables[tbl]["pk"]}
    for n in plan_result["nulls"]:
        tbl, col = n["table"], n["column"]
        pk = graph.tables[tbl]["pk"]
        entry = {"table": tbl, "column": col, "pk": pk, "file": f"_null.{tbl}.{col}.jsonl.gz", "rows": 0}
        if pk:
            fields = sql.SQL(", ").join(sql.SQL("{}, x.{}").format(sql.Literal(c), sql.Identifier(c)) for c in pk + [col])
            query = sql.SQL("COPY (SELECT json_build_object({fields})::text FROM {table} x WHERE x.ctid IN "
                            "(SELECT rid FROM {nul} WHERE tbl = {tbl} AND col = {col})) TO STDOUT").format(
                fields=fields, table=qualified(graph, tbl), nul=NUL, tbl=sql.Literal(tbl), col=sql.Literal(col))
            entry["rows"] = _copy_to_file(conn, query, backup_dir / entry["file"])
        else:
            entry["note"] = "table has no primary key: nulled values cannot be restored"
        manifest["nulls"].append(entry)
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def _copy_to_file(conn, query, path):
    rows = 0
    with gzip.open(path, "wt", encoding="utf-8") as fh, conn.cursor().copy(query) as copy:
        for (line,) in copy.rows():
            fh.write(line + "\n")
            rows += 1
    return rows


def execute(conninfo, graph, root_table, where, *, expected, backup_dir, timeout_s, log, limit=None):
    """Plan, verify against `expected` (plan_signature of the preview), back up, delete, commit."""
    backup_dir = Path(backup_dir)
    with connect_for_writes(conninfo, timeout_s) as conn:
        try:
            plan_result = plan(conn, graph, root_table, where, limit=limit, samples=0)
            if plan_result["blockers"]:
                raise OperationError(_blocked_message(plan_result))
            if expected is not None and plan_signature(plan_result) != expected:
                raise OperationError(
                    "The data changed since the preview (expected "
                    f"{_fmt(expected)}, now {_fmt(plan_signature(plan_result))}). Nothing was deleted; preview again.")
            if plan_result["root_count"] == 0:
                conn.rollback()
                return {"root_count": 0, "counts": {"deleted": {}, "nulled": {}}, "backup": None}
            _write_backup(conn, graph, plan_result, backup_dir)
            counts = _apply(conn, graph, log)
            conn.commit()
        except BaseException as exc:
            conn.rollback()
            shutil.rmtree(backup_dir, ignore_errors=True)
            if isinstance(exc, psycopg.Error):
                raise OperationError(str(exc).strip()) from exc
            raise
    return {"root_count": plan_result["root_count"], "counts": counts, "backup": str(backup_dir)}


def _fmt(signature):
    parts = [f"{t}: {n}" for t, n in sorted(signature["delete"].items())]
    parts += [f"{c} → NULL: {n}" for c, n in sorted(signature["null"].items())]
    return ", ".join(parts)


def _dollar(text):
    while True:
        tag = f"$ss{secrets.token_hex(4)}$"
        if tag not in text:
            return f"{tag}{text}{tag}"


def _read_lines(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield line.rstrip("\n")


def _backup_dirs(root):
    root = Path(root)
    if (root / "manifest.json").exists():
        return [root]
    return sorted(p for p in root.iterdir() if (p / "manifest.json").exists())


def restore_script(backup_root):
    """SQL that puts back every deleted row and nulled value (parents first)."""
    yield f"-- Schema Sync restore script for {Path(backup_root).name}\n"
    yield "-- Re-inserts deleted rows and restores nulled columns. Review before running.\n"
    yield "BEGIN;\nSET CONSTRAINTS ALL DEFERRED;\n"
    for directory in _backup_dirs(backup_root):
        manifest = json.loads((directory / "manifest.json").read_text())
        schema = manifest["schema"]
        yield f"\n-- from {directory.name}\n"
        for tbl, info in sorted(manifest["tables"].items(), key=lambda kv: (kv[1]["depth"], kv[0])):
            target = f'"{schema}"."{tbl}"'
            for line in _read_lines(directory / f"{tbl}.jsonl.gz"):
                yield (f"INSERT INTO {target} OVERRIDING SYSTEM VALUE SELECT * FROM "
                       f"json_populate_record(NULL::{target}, {_dollar(line)});\n")
        for entry in manifest["nulls"]:
            if not entry["pk"]:
                yield f"-- {entry['table']}.{entry['column']}: {entry.get('note', 'not restorable')}\n"
                continue
            target = f'"{schema}"."{entry["table"]}"'
            col = f'"{entry["column"]}"'
            match = " AND ".join(f'{target}."{c}" = r."{c}"' for c in entry["pk"])
            for line in _read_lines(directory / entry["file"]):
                yield (f"UPDATE {target} SET {col} = r.{col} FROM json_populate_record(NULL::{target}, "
                       f"{_dollar(line)}) r WHERE {match};\n")
    yield "\nCOMMIT;\n"
