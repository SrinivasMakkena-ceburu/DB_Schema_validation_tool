"""Data browser (read-only), data operations (delete / cleanup / drop column), recipes and jobs."""
import re
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from psycopg import sql

from . import operations
from .cascade import build_graph
from .column_ops import drop_column_restore_script
from .crypto import DecryptError
from .db_inspector import InspectError, connect, inspect_database
from .executor import restore_script
from .filters import OPERATORS, FilterError, describe, parse_filters, validate_filters
from .forms import RecipeForm
from .jobs import recover_interrupted, start_job
from .models import CleanupRecipe, DataOperation, DatabaseTarget, Job, Project
from .planner import qualified
from .project_loader import ExtractorError, load_project_cached

PAGE_SIZE = 50
COUNT_CAP = 100_000
SECRET_COLUMN = re.compile(r"pass(word)?|secret|token|api_?key|private_?key|credential|salt", re.I)
LABEL_COLUMNS = ("name", "title", "email", "username", "hostname", "slug", "code")


# Helpers -------------------------------------------------------------------

def _project(request):
    pk = request.session.get("data_project_id")
    return Project.objects.filter(pk=pk).first() if pk else None


def _extracted(request):
    project = _project(request)
    if not project:
        return None, ""
    try:
        return load_project_cached(project), ""
    except ExtractorError as exc:
        return None, f"Could not load project {project.name}: {exc.args[0]}"


def _inspect(database):
    try:
        return inspect_database(database.conninfo(), database.schema), ""
    except (InspectError, DecryptError) as exc:
        return None, str(exc)


def _table_or_404(db_info, table):
    if not db_info or table not in db_info["tables"]:
        raise Http404("No such table")
    return db_info["tables"][table]


def _row_label(row):
    for name in LABEL_COLUMNS:
        if row.get(name):
            return str(row[name])
    return ""


def _filter_rows(columns, filters):
    """Rows for the filter editor: the current filters plus one empty row."""
    return list(filters) + [{"column": "", "op": "eq", "value": ""}]


# Data browser ----------------------------------------------------------------

def data_home(request):
    recover_interrupted()
    return render(request, "validator/data_home.html", {
        "databases": DatabaseTarget.objects.all(),
        "projects": Project.objects.all(),
        "project": _project(request),
        "operations": DataOperation.objects.all()[:8],
    })


@require_POST
def data_project(request):
    pk = request.POST.get("project")
    request.session["data_project_id"] = int(pk) if pk and pk.isdigit() else None
    target = request.POST.get("next", "")
    return redirect(target if target.startswith("/") and not target.startswith("//") else "data_home")


def data_tables(request, db):
    database = get_object_or_404(DatabaseTarget, pk=db)
    db_info, error = _inspect(database)
    tables = sorted(db_info["tables"].items()) if db_info else []
    return render(request, "validator/data_tables.html", {
        "database": database, "tables": tables, "views": db_info["views"] if db_info else [], "error": error,
        "project": _project(request), "projects": Project.objects.all(),
    })


def data_table(request, db, table):
    database = get_object_or_404(DatabaseTarget, pk=db)
    db_info, error = _inspect(database)
    if error:
        return render(request, "validator/data_tables.html", {"database": database, "error": error, "tables": []})
    info = _table_or_404(db_info, table)
    columns = {name: c["type"] for name, c in info["columns"].items()}
    filters = parse_filters(request.GET)
    sort = request.GET.get("sort", "")
    descending = sort.startswith("-")
    sort_column = sort.lstrip("-")
    if sort_column not in columns:
        sort_column, descending = (info["pk"][0] if info["pk"] else ""), False
    page = max(1, int(request.GET.get("page", "1")) if request.GET.get("page", "1").isdigit() else 1)
    reveal = request.GET.get("reveal") == "1"

    rows, total, filter_error = [], None, ""
    ident = sql.Identifier(database.schema, table)
    try:
        with connect(database.conninfo()) as conn:
            where = validate_filters(conn, ident, columns, filters)
            order = sql.SQL("")
            if sort_column:
                order = sql.SQL(" ORDER BY {} {}").format(sql.Identifier(sort_column),
                                                         sql.SQL("DESC" if descending else "ASC"))
            cursor = conn.execute(sql.SQL("SELECT * FROM {} WHERE {}{} LIMIT {} OFFSET {}").format(
                ident, where, order, sql.Literal(PAGE_SIZE + 1), sql.Literal((page - 1) * PAGE_SIZE)))
            names = [d.name for d in cursor.description]
            rows = [dict(zip(names, r)) for r in cursor.fetchall()]
            total = conn.execute(sql.SQL("SELECT count(*) FROM (SELECT 1 FROM {} WHERE {} LIMIT {}) s").format(
                ident, where, sql.Literal(COUNT_CAP + 1))).fetchone()[0]
    except FilterError as exc:
        filter_error = str(exc)
    except InspectError as exc:
        filter_error = str(exc)

    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    pk = info["pk"][0] if len(info["pk"]) == 1 else None
    masked = {c for c in columns if SECRET_COLUMN.search(c)}
    display = [{
        "pk": r.get(pk) if pk else None,
        "cells": [("••••••" if c in masked and not reveal and r[c] is not None else r[c]) for c in columns],
    } for r in rows]
    query = request.GET.copy()
    for key in ("page", "sort"):
        query.pop(key, None)
    return render(request, "validator/data_table.html", {
        "database": database, "table": table, "info": info, "columns": columns, "rows": display,
        "filters": filters, "filter_rows": _filter_rows(columns, filters), "operators": OPERATORS,
        "filter_text": describe(filters), "filter_error": filter_error, "total": total, "count_cap": COUNT_CAP,
        "page": page, "has_next": has_next, "sort_column": sort_column, "descending": descending,
        "query": query.urlencode(), "masked": masked, "reveal": reveal, "pk": pk,
        "project": _project(request),
    })


def data_row(request, db, table, pk):
    database = get_object_or_404(DatabaseTarget, pk=db)
    db_info, error = _inspect(database)
    info = _table_or_404(db_info, table)
    if len(info["pk"]) != 1:
        raise Http404("This table has no single-column primary key")
    pk_column = info["pk"][0]
    columns = {name: c["type"] for name, c in info["columns"].items()}
    extracted, project_error = _extracted(request)
    graph = build_graph(db_info, extracted, schema=database.schema)
    ident = sql.Identifier(database.schema, table)
    reveal = request.GET.get("reveal") == "1"
    try:
        with connect(database.conninfo()) as conn:
            where = validate_filters(conn, ident, columns, [{"column": pk_column, "op": "eq", "value": pk}])
            cursor = conn.execute(sql.SQL("SELECT * FROM {} WHERE {}").format(ident, where))
            names = [d.name for d in cursor.description]
            found = cursor.fetchone()
            if not found:
                raise Http404("No such row")
            row = dict(zip(names, found))
            parents, children = [], []
            for rel in graph.relations:
                if rel.child_table == table and rel.action != "parent_link" and rel.action != "generic":
                    value = row.get(rel.child_column)
                    parent_pk = graph.tables[rel.parent_table]["pk"]
                    link = (reverse("data_row", args=[database.pk, rel.parent_table, value])
                            if value is not None and parent_pk == [rel.parent_column] else "")
                    parents.append({"column": rel.child_column, "table": rel.parent_table, "value": value,
                                    "link": link})
                elif rel.parent_table == table and rel.action != "parent_link":
                    value = row.get(rel.parent_column)
                    if value is None:
                        continue
                    child = qualified(graph, rel.child_table)
                    condition = sql.SQL("{} = {}").format(sql.Identifier(rel.child_column), sql.Literal(value))
                    if rel.action == "generic":
                        ct = conn.execute(sql.SQL("SELECT id FROM {} WHERE app_label = %s AND model = %s").format(
                            qualified(graph, "django_content_type")), list(rel.content_type)).fetchone()
                        if not ct:
                            continue
                        condition = sql.SQL("{} = {} AND CAST({} AS text) = {}").format(
                            sql.Identifier(rel.ct_column), sql.Literal(ct[0]), sql.Identifier(rel.child_column),
                            sql.Literal(str(value)))
                    n = conn.execute(sql.SQL("SELECT count(*) FROM {} WHERE {}").format(child, condition)).fetchone()[0]
                    link = reverse("data_table", args=[database.pk, rel.child_table])
                    link += f"?f_col={rel.child_column}&f_op=eq&f_val={value}"
                    children.append({"table": rel.child_table, "column": rel.child_column, "count": n,
                                     "action": rel.action, "relation": rel.id, "link": link})
    except InspectError as exc:
        raise Http404(str(exc))
    fields = [(c, ("••••••" if SECRET_COLUMN.search(c) and not reveal and row[c] is not None else row[c]))
              for c in names]
    return render(request, "validator/data_row.html", {
        "database": database, "table": table, "pk": pk, "pk_column": pk_column, "fields": fields,
        "label": _row_label(row), "parents": parents, "children": sorted(children, key=lambda c: -c["count"]),
        "project": _project(request), "project_error": project_error,
    })


# Operations ----------------------------------------------------------------

def _preview_job(op):
    def target(job):
        operations.preview_operation(op)
        return reverse("op_detail", args=[op.pk])
    return start_job("preview", f"Preview {op}", target)


@require_POST
def op_new(request):
    database = get_object_or_404(DatabaseTarget, pk=request.POST.get("database"))
    kind = request.POST.get("kind")
    table = request.POST.get("table", "")
    back = reverse("data_table", args=[database.pk, table]) if table else reverse("data_tables", args=[database.pk])
    filters = parse_filters(request.POST)
    if kind not in ("delete", "drop_column"):
        raise Http404("Unknown operation")
    if kind == "delete" and not filters:
        messages.error(request, "Deleting needs at least one filter. To empty a table, filter on a column that "
                                "every row matches and confirm the count.")
        return redirect(back)
    op = DataOperation.objects.create(
        kind=kind, database=database, database_name=database.name, environment=database.environment,
        project=_project(request), root_table=table, column=request.POST.get("column", ""), filters=filters,
    )
    job = _preview_job(op)
    return redirect(job.result_url or reverse("job_detail", args=[job.pk]))


def op_list(request):
    recover_interrupted()
    return render(request, "validator/op_list.html", {"operations": DataOperation.objects.all()[:200]})


def op_detail(request, pk):
    op = get_object_or_404(DataOperation, pk=pk)
    files = []
    if op.backup_dir and Path(op.backup_dir).is_dir():
        root = Path(op.backup_dir)
        files = [{"name": str(p.relative_to(root)), "size": p.stat().st_size}
                 for p in sorted(root.rglob("*")) if p.is_file()]
    blockers = op.plan.get("blockers", []) if isinstance(op.plan, dict) else []
    return render(request, "validator/op_detail.html", {
        "op": op, "plan": op.plan, "phrase": operations.confirmation_phrase(op) if op.plan else "",
        "filter_text": describe(op.filters), "files": files,
        "overridable": [b for b in blockers if b.get("source") == "db"],
        "plan_max_age": settings.PLAN_MAX_AGE_MINUTES,
        "can_write": bool(op.database and op.database.writes_enabled),
    })


@require_POST
def op_repreview(request, pk):
    op = get_object_or_404(DataOperation, pk=pk)
    if op.status in ("running", "done"):
        messages.error(request, f"This operation is {op.status}.")
        return redirect("op_detail", pk=op.pk)
    op.cascade_overrides = request.POST.getlist("override")
    op.status = "planning"
    op.save(update_fields=["cascade_overrides", "status"])
    job = _preview_job(op)
    return redirect(job.result_url or reverse("job_detail", args=[job.pk]))


@require_POST
def op_execute(request, pk):
    op = get_object_or_404(DataOperation, pk=pk)
    typed = request.POST.get("confirmation", "")
    errors = operations.check_executable(op, typed, request.POST.get("backup_ack") == "on")
    if errors:
        for error in errors:
            messages.error(request, error)
        return redirect("op_detail", pk=op.pk)
    op.confirmation = typed
    op.save(update_fields=["confirmation"])
    job = start_job(op.kind, str(op), lambda job: operations.run_operation(op, job))
    return redirect(job.result_url or reverse("job_detail", args=[job.pk]))


def op_restore(request, pk):
    op = get_object_or_404(DataOperation, pk=pk)
    if not op.backup_dir or not Path(op.backup_dir).is_dir():
        raise Http404("No backup for this operation")
    script = drop_column_restore_script(op.backup_dir) if op.kind == "drop_column" else restore_script(op.backup_dir)
    response = StreamingHttpResponse(script, content_type="application/sql; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="restore-op-{op.pk}.sql"'
    return response


def op_backup_file(request, pk, name):
    op = get_object_or_404(DataOperation, pk=pk)
    if not op.backup_dir:
        raise Http404("No backup")
    root = Path(op.backup_dir).resolve()
    path = (root / name).resolve()
    if root not in path.parents or not path.is_file():
        raise Http404("No such file")
    return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)


# Recipes -------------------------------------------------------------------

def recipe_list(request):
    return render(request, "validator/recipe_list.html", {
        "recipes": CleanupRecipe.objects.all(), "databases": DatabaseTarget.objects.all(),
    })


def recipe_form(request, pk=None):
    recipe = get_object_or_404(CleanupRecipe, pk=pk) if pk else None
    initial = {} if recipe else {"root_table": request.GET.get("root_table", "")}
    form = RecipeForm(request.POST or None, instance=recipe, initial=initial)
    if request.method == "POST":
        filters = parse_filters(request.POST)
    else:
        filters = recipe.filters if recipe else parse_filters(request.GET)
    if request.method == "POST":
        if not filters:
            form.add_error(None, "A cleanup needs at least one filter.")
        elif form.is_valid():
            recipe = form.save(commit=False)
            recipe.filters = filters
            recipe.save()
            messages.success(request, f"Saved cleanup {recipe.name}.")
            return redirect("recipe_list")
    return render(request, "validator/recipe_form.html", {
        "form": form, "object": recipe, "filter_rows": _filter_rows({}, filters), "operators": OPERATORS,
    })


@require_POST
def recipe_delete(request, pk):
    recipe = get_object_or_404(CleanupRecipe, pk=pk)
    recipe.delete()
    messages.success(request, f"Deleted cleanup {recipe.name}. Its past runs stay in Operations.")
    return redirect("recipe_list")


@require_POST
def recipe_run(request, pk):
    recipe = get_object_or_404(CleanupRecipe, pk=pk)
    database = get_object_or_404(DatabaseTarget, pk=request.POST.get("database"))
    op = DataOperation.objects.create(
        kind="cleanup", database=database, database_name=database.name, environment=database.environment,
        project=recipe.project, recipe=recipe, root_table=recipe.root_table, filters=recipe.filters,
        batch_size=recipe.batch_size,
    )
    job = _preview_job(op)
    return redirect(job.result_url or reverse("job_detail", args=[job.pk]))


# Jobs ----------------------------------------------------------------------

def job_detail(request, pk):
    recover_interrupted()
    job = get_object_or_404(Job, pk=pk)
    if job.status == "done" and job.result_url and request.GET.get("stay") != "1":
        return redirect(job.result_url)
    return render(request, "validator/job.html", {"job": job})


def job_json(request, pk):
    job = get_object_or_404(Job, pk=pk)
    return JsonResponse({"status": job.status, "progress": job.progress, "message": job.message,
                         "result_url": job.result_url, "finished": job.finished})


@require_POST
def job_cancel(request, pk):
    job = get_object_or_404(Job, pk=pk)
    if not job.finished:
        job.cancel_requested = True
        job.save(update_fields=["cancel_requested"])
        messages.success(request, "Cancel requested: the job stops after the current batch.")
    return redirect("job_detail", pk=job.pk)
