import json

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .crypto import DecryptError
from .db_inspector import InspectError, check_connection
from .forms import ComparisonForm, DatabaseForm, ProjectForm
from .models import ComparisonRun, DatabaseTarget, Project
from .project_loader import ExtractorError, git_info, load_project
from .runner import run_batch
from .templatetags.validator_tags import CATEGORY_ORDER


def _batches(runs):
    """Group runs (newest first) by batch, preserving order."""
    batches = {}
    for run in runs:
        batches.setdefault(run.batch_id, []).append(run)
    return [{"id": batch_id, "runs": items, "first": items[0]} for batch_id, items in batches.items()]


def dashboard(request):
    form = ComparisonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        batch_id = run_batch(form.cleaned_data["project"], list(form.cleaned_data["databases"]), form.options())
        return redirect("batch_detail", batch_id=batch_id)
    recent = ComparisonRun.objects.all()[:60]
    return render(request, "validator/dashboard.html", {
        "form": form,
        "batches": _batches(recent)[:8],
        "has_projects": Project.objects.exists(),
        "has_databases": DatabaseTarget.objects.exists(),
    })


def batch_detail(request, batch_id):
    runs = list(ComparisonRun.objects.filter(batch_id=batch_id).order_by("database_name"))
    if not runs:
        return render(request, "validator/not_found.html", {"what": "comparison"}, status=404)
    present = set()
    for run in runs:
        present.update(run.summary.get("by_category", {}))
    rows = [
        {"category": cat, "cells": [run.summary.get("by_category", {}).get(cat) for run in runs]}
        for cat in CATEGORY_ORDER if cat in present
    ]
    return render(request, "validator/batch.html", {"runs": runs, "rows": rows, "first": runs[0]})


def run_detail(request, pk):
    run = get_object_or_404(ComparisonRun, pk=pk)
    return render(request, "validator/run_detail.html", {
        "run": run,
        "report": run.report,
        "siblings": ComparisonRun.objects.filter(batch_id=run.batch_id).exclude(pk=run.pk).order_by("database_name"),
    })


def run_fix_sql(request, pk):
    run = get_object_or_404(ComparisonRun, pk=pk)
    response = HttpResponse(run.fix_sql, content_type="application/sql; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="fix-{run.database_name}-{run.pk}.sql"'
    return response


def run_report_json(request, pk):
    run = get_object_or_404(ComparisonRun, pk=pk)
    data = {
        "project": run.project_name, "database": run.database_name, "branch": run.git_branch,
        "commit": run.git_commit, "started_at": run.started_at.isoformat(), "status": run.status,
        "error": run.error, "options": run.options, "summary": run.summary, "report": run.report,
    }
    response = HttpResponse(json.dumps(data, indent=2), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="report-{run.database_name}-{run.pk}.json"'
    return response


def run_list(request):
    runs = ComparisonRun.objects.all()
    project, database = request.GET.get("project", ""), request.GET.get("database", "")
    if project:
        runs = runs.filter(project_name=project)
    if database:
        runs = runs.filter(database_name=database)
    return render(request, "validator/run_list.html", {
        "runs": runs[:200],
        "project": project,
        "database": database,
        "project_names": ComparisonRun.objects.values_list("project_name", flat=True).distinct().order_by("project_name"),
        "database_names": ComparisonRun.objects.values_list("database_name", flat=True).distinct().order_by("database_name"),
    })


# Projects -----------------------------------------------------------------

def project_list(request):
    projects = list(Project.objects.all())
    for project in projects:
        project.branch, project.commit = git_info(project.path)
    return render(request, "validator/project_list.html", {"projects": projects})


def project_form(request, pk=None):
    project = get_object_or_404(Project, pk=pk) if pk else None
    form = ProjectForm(request.POST or None, instance=project)
    if request.method == "POST" and form.is_valid():
        project = form.save()
        messages.success(request, f"Saved project {project.name}.")
        return redirect("project_list")
    return render(request, "validator/form.html", {
        "form": form, "object": project, "kind": "project", "cancel": "project_list",
    })


@require_POST
def project_check(request, pk):
    project = get_object_or_404(Project, pk=pk)
    try:
        data = load_project(project)
    except ExtractorError as exc:
        return render(request, "validator/project_check.html", {"project": project, "error": str(exc)})
    migrations = sum(len(info["nodes"]) for info in data["migrations"].values())
    pending = sum(len(ops) for ops in data["pending_changes"].values())
    return render(request, "validator/project_check.html", {
        "project": project, "data": data, "migration_count": migrations, "pending_count": pending,
    })


# Databases ----------------------------------------------------------------

def database_list(request):
    return render(request, "validator/database_list.html", {"databases": DatabaseTarget.objects.all()})


def database_form(request, pk=None):
    database = get_object_or_404(DatabaseTarget, pk=pk) if pk else None
    form = DatabaseForm(request.POST or None, instance=database)
    if request.method == "POST" and form.is_valid():
        database = form.save()
        messages.success(request, f"Saved database {database.name}.")
        return redirect("database_list")
    return render(request, "validator/form.html", {
        "form": form, "object": database, "kind": "database", "cancel": "database_list",
    })


@require_POST
def database_test(request, pk):
    database = get_object_or_404(DatabaseTarget, pk=pk)
    try:
        version = check_connection(database.conninfo())
    except (InspectError, DecryptError) as exc:
        messages.error(request, f"{database.name}: {exc}")
    else:
        messages.success(request, f"{database.name}: connected (read-only). {version.split(',')[0]}")
    return redirect("database_list")


# Shared -------------------------------------------------------------------

def delete_object(request, kind, pk):
    model = {"project": Project, "database": DatabaseTarget}[kind]
    obj = get_object_or_404(model, pk=pk)
    target = f"{kind}_list"
    if request.method == "POST":
        name = obj.name
        obj.delete()
        messages.success(request, f"Deleted {kind} {name}. Its past runs are kept.")
        return redirect(target)
    return render(request, "validator/confirm_delete.html", {"object": obj, "kind": kind, "cancel": target})
