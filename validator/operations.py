"""Data operations (delete, cleanup, drop column): preview, confirmation rules, execution."""
import math
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from psycopg import sql

from .cascade import build_graph
from .column_ops import execute_drop_column, plan_drop_column
from .crypto import DecryptError
from .db_inspector import InspectError, inspect_database
from .executor import OperationError, connect_for_preview, execute, plan_signature, rehearse
from .filters import FilterError, build_where, validate_filters
from .jobs import check_cancel, update
from .planner import PlanError, plan, qualified
from .project_loader import ExtractorError, load_project_cached


def _extracted(op):
    return load_project_cached(op.project) if op.project else None


def _graph(op, extracted):
    db_info = inspect_database(op.database.conninfo(), op.database.schema)
    return build_graph(db_info, extracted, op.cascade_overrides, schema=op.database.schema)


def preview_operation(op):
    """Fill op.plan (and op.rehearsal when writes are enabled). Never changes data."""
    op.error = ""
    try:
        if not op.database:
            raise OperationError("The database of this operation was deleted")
        extracted = _extracted(op)
        if op.kind == "drop_column":
            op.plan = plan_drop_column(op.database.conninfo(), op.database.schema, op.root_table, op.column, extracted)
            op.rehearsal = {}
            op.status = "blocked" if op.plan["blockers"] else "planned"
        else:
            graph = _graph(op, extracted)
            if op.root_table not in graph.tables:
                raise OperationError(f"Table {op.root_table} is not in {op.database_name}")
            columns = graph.columns(op.root_table)
            limit = op.batch_size if op.kind == "cleanup" else None
            timeout = op.database.operation_timeout_s
            with connect_for_preview(op.database.conninfo(), timeout) as conn:
                where = validate_filters(conn, qualified(graph, op.root_table), columns, op.filters)
                result = plan(conn, graph, op.root_table, where, limit=limit)
                if op.kind == "cleanup":
                    result["root_total"] = conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE {}").format(
                        qualified(graph, op.root_table), where)).fetchone()[0]
                    result["batches"] = math.ceil(result["root_total"] / op.batch_size) if op.batch_size else 1
                conn.rollback()
            result["signature"] = plan_signature(result)
            result["null_rows"] = sum(n["count"] for n in result["nulls"])
            result["pk"] = graph.tables[op.root_table]["pk"]
            op.plan = result
            if result["blockers"]:
                op.status, op.rehearsal = "blocked", {}
            elif not op.database.writes_enabled:
                op.status, op.rehearsal = "planned", {"skipped": "Writes are not enabled for this database"}
            else:
                op.rehearsal = rehearse(op.database.write_conninfo(), graph, op.root_table, where,
                                        timeout_s=timeout, limit=limit)
                op.status = "planned"
        op.planned_at = timezone.now()
    except (OperationError, PlanError, FilterError, InspectError, ExtractorError, DecryptError) as exc:
        op.status, op.error = "error", str(exc)
    op.save()
    return op


def _pk_value(op):
    if op.kind != "delete" or len(op.filters) != 1:
        return None
    f = op.filters[0]
    pk = op.plan.get("pk") or []
    if f["op"] == "eq" and op.plan.get("root_count") == 1 and f["column"] in (pk or [f["column"]]):
        return f["value"]
    return None


def confirmation_phrase(op):
    db = op.database_name
    if op.kind == "drop_column":
        return f"drop {op.root_table}.{op.column} on {db}"
    if op.kind == "cleanup":
        return f"cleanup {op.recipe.name if op.recipe else op.root_table} on {db}"
    value = _pk_value(op)
    if value is not None:
        return f"delete {op.root_table} {value} on {db}"
    return f"delete {op.plan.get('root_count', 0)} rows from {op.root_table} on {db}"


def check_executable(op, typed, backup_ack):
    """Reasons the operation may not run now (empty list = it may)."""
    errors = []
    if not op.database or not op.database.writes_enabled:
        errors.append("Writes are not enabled for this database. Enable them on the database settings page.")
    if op.status == "blocked":
        errors.append("The operation is blocked; resolve the blockers and preview again.")
    elif op.status != "planned":
        errors.append(f"The operation is {op.status}; only a previewed operation can run.")
    if op.rehearsal and op.rehearsal.get("ok") is False:
        errors.append("The rehearsal failed: " + op.rehearsal.get("error", ""))
    max_age = timedelta(minutes=settings.PLAN_MAX_AGE_MINUTES)
    if not op.planned_at or timezone.now() - op.planned_at > max_age:
        errors.append(f"The preview is older than {settings.PLAN_MAX_AGE_MINUTES} minutes; preview again.")
    if (typed or "").strip() != confirmation_phrase(op):
        errors.append("Type the confirmation phrase exactly as shown.")
    if op.database and op.database.is_prod and not backup_ack:
        errors.append("On a prod database, confirm that you have checked the backup location.")
    return errors


def _merge(total, counts):
    for kind in ("deleted", "nulled"):
        for key, n in counts.get(kind, {}).items():
            total.setdefault(kind, {})[key] = total.get(kind, {}).get(key, 0) + n


def run_operation(op, job):
    """Execute a previewed, confirmed operation (called inside a Job)."""
    op.status, op.job, op.executed_at = "running", job, timezone.now()
    op.save(update_fields=["status", "job", "executed_at"])
    backup_root = Path(settings.BACKUP_DIR) / f"op-{op.pk}"
    log = []
    database = op.database
    try:
        if op.kind == "drop_column":
            update(job, message=f"Dropping {op.root_table}.{op.column}")
            result = execute_drop_column(database.write_conninfo(), database.schema, op.root_table, op.column,
                                         backup_dir=backup_root, timeout_s=database.operation_timeout_s, log=log)
            op.counts = {"rows_backed_up": result["rows_backed_up"]}
        else:
            graph = _graph(op, _extracted(op))
            where = build_where(graph.columns(op.root_table), op.filters)
            if op.kind == "delete":
                update(job, message=f"Deleting {op.plan['total_rows']} rows")
                result = execute(database.write_conninfo(), graph, op.root_table, where,
                                 expected=op.plan["signature"], backup_dir=backup_root,
                                 timeout_s=database.operation_timeout_s, log=log)
                op.counts = result["counts"]
            else:
                _run_cleanup(op, job, graph, where, backup_root, log)
        op.status = "done"
    except Exception as exc:
        from .jobs import JobCancelled

        if isinstance(exc, JobCancelled):
            op.status = "cancelled"
        else:
            op.status, op.error = "failed", str(exc)
        raise
    finally:
        op.sql_log = "\n".join(log)
        op.backup_dir = str(backup_root) if backup_root.exists() else ""
        op.finished_at = timezone.now()
        op.save()
    return reverse("op_detail", args=[op.pk])


def _run_cleanup(op, job, graph, where, backup_root, log):
    database = op.database
    target = op.plan["root_total"]
    done, batch = 0, 0
    op.counts = {"deleted": {}, "nulled": {}, "batches": 0, "roots": 0}
    while done < target:
        check_cancel(job)
        limit = min(op.batch_size, target - done)
        result = execute(database.write_conninfo(), graph, op.root_table, where, expected=None,
                         backup_dir=backup_root / f"batch-{batch + 1:04d}", timeout_s=database.operation_timeout_s,
                         log=log, limit=limit)
        if result["root_count"] == 0:
            break
        batch += 1
        done += result["root_count"]
        _merge(op.counts, result["counts"])
        op.counts["batches"], op.counts["roots"] = batch, done
        op.save(update_fields=["counts"])
        update(job, progress=done * 100 / target, message=f"Batch {batch}: {done} of {target} rows",
               log=f"batch {batch}: {result['counts']['deleted']}")
    check_cancel(job)
