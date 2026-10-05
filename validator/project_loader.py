"""Run extractor.py with a project's own interpreter and read its JSON."""
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings

EXTRACTOR = Path(__file__).resolve().parent / "extractor.py"


class ExtractorError(Exception):
    def __init__(self, message, details=""):
        super().__init__(message)
        self.details = details

    def __str__(self):
        return f"{self.args[0]}\n\n{self.details}".strip()


def detect_settings_module(path):
    manage = Path(path) / "manage.py"
    try:
        match = re.search(r"DJANGO_SETTINGS_MODULE['\"]\s*,\s*['\"]([\w.]+)['\"]", manage.read_text())
    except OSError:
        return ""
    return match.group(1) if match else ""


def git_info(path):
    def git(*args):
        try:
            proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "unknown"

    return git("rev-parse", "--abbrev-ref", "HEAD"), git("rev-parse", "--short", "HEAD")


def load_project(project):
    path = Path(project.path)
    if not (path / "manage.py").is_file():
        raise ExtractorError(f"No manage.py in {path}")
    if not Path(project.python_path).is_file():
        raise ExtractorError(f"Python interpreter not found: {project.python_path}")
    settings_module = project.settings_module or detect_settings_module(path)
    if not settings_module:
        raise ExtractorError("Settings module not set and not found in manage.py")

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
    env.update(project.env_dict())
    env["DJANGO_SETTINGS_MODULE"] = settings_module
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "extract.json"
        command = [project.python_path, str(EXTRACTOR), "--settings", settings_module,
                   "--output", str(output), "--project-dir", str(path)]
        try:
            proc = subprocess.run(command, cwd=path, env=env, capture_output=True, text=True,
                                  timeout=settings.EXTRACTOR_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise ExtractorError(f"Extractor timed out after {settings.EXTRACTOR_TIMEOUT_SECONDS}s")
        except OSError as exc:
            raise ExtractorError(f"Could not start {project.python_path}: {exc}")
        if proc.returncode != 0 or not output.exists():
            details = proc.stderr.strip()
            if proc.stdout.strip():
                details += "\n\n--- stdout ---\n" + proc.stdout.strip()[-4000:]
            raise ExtractorError(f"Extractor failed (exit code {proc.returncode})", details)
        return json.loads(output.read_text())


def _git_state(project):
    """(commit, hash of working-tree changes + project settings), or None outside git."""
    def git(*args):
        try:
            proc = subprocess.run(["git", "-C", project.path, *args], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return proc.stdout if proc.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=normal", ".")
    if commit is None or status is None:
        return None
    fingerprint = "\n".join([status, project.python_path, project.settings_module, project.extra_env])
    return commit.strip(), hashlib.sha256(fingerprint.encode()).hexdigest()


def load_project_cached(project, force=False):
    """load_project(), reusing the last result while the checkout is unchanged."""
    from .models import ProjectSnapshot

    state = _git_state(project)
    if state and not force:
        snapshot = ProjectSnapshot.objects.filter(project=project, commit=state[0], status_hash=state[1]).first()
        if snapshot:
            return snapshot.data
    data = load_project(project)
    if state:
        ProjectSnapshot.objects.create(project=project, commit=state[0], status_hash=state[1], data=data)
        keep = ProjectSnapshot.objects.filter(project=project).values_list("pk", flat=True)[:5]
        ProjectSnapshot.objects.filter(project=project).exclude(pk__in=list(keep)).delete()
    return data
