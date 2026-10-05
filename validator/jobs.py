"""Background jobs: run on a thread (or inline when settings.JOBS_INLINE), with progress."""
import threading
import traceback

from django.conf import settings
from django.db import close_old_connections, connection
from django.utils import timezone

from .models import DataOperation, Job

_RUNNING = set()  # job ids running in this process


class JobCancelled(Exception):
    pass


def start_job(kind, title, target):
    """Create a Job and run target(job) -> result URL (or None). Returns the Job."""
    job = Job.objects.create(kind=kind, title=title)
    _RUNNING.add(job.pk)
    if settings.JOBS_INLINE:
        _run(job.pk, target, inline=True)
    else:
        threading.Thread(target=_run, args=(job.pk, target), daemon=True, name=f"job-{job.pk}").start()
    job.refresh_from_db()
    return job


def _run(job_id, target, inline=False):
    if not inline:
        close_old_connections()
    job = Job.objects.get(pk=job_id)
    job.status = "running"
    job.save(update_fields=["status"])
    try:
        result = target(job)
        job.status, job.progress, job.result_url = "done", 100, result or ""
    except JobCancelled:
        job.status, job.message = "cancelled", "Cancelled"
    except Exception as exc:
        job.status, job.message = "failed", str(exc).splitlines()[0][:500] if str(exc) else type(exc).__name__
        job.log += traceback.format_exc()
    finally:
        job.finished_at = timezone.now()
        job.save()
        _RUNNING.discard(job_id)
        if not inline:
            connection.close()


def update(job, *, progress=None, message=None, log=None):
    fields = []
    if progress is not None:
        job.progress = max(0, min(100, int(progress)))
        fields.append("progress")
    if message is not None:
        job.message = message[:500]
        fields.append("message")
    if log:
        job.log += log.rstrip("\n") + "\n"
        fields.append("log")
    if fields:
        job.save(update_fields=fields)


def check_cancel(job):
    job.refresh_from_db(fields=["cancel_requested"])
    if job.cancel_requested:
        raise JobCancelled()


def recover_interrupted():
    """Jobs left running by a previous server process are marked interrupted."""
    stale = Job.objects.filter(status__in=["queued", "running"]).exclude(pk__in=list(_RUNNING))
    ids = list(stale.values_list("pk", flat=True))
    if ids:
        Job.objects.filter(pk__in=ids).update(status="interrupted", finished_at=timezone.now(),
                                              message="The server stopped while this was running")
        DataOperation.objects.filter(job_id__in=ids, status="running").update(
            status="failed", error="Interrupted: the server stopped while this was running. Check the database "
                                   "and the backup folder before retrying.")
