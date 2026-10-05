"""Settings for the Schema Sync Validator. A local, single-user tool."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SCHEMASYNC_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _secret_key():
    path = DATA_DIR / "django_secret.txt"
    if not path.exists():
        from django.core.management.utils import get_random_secret_key

        path.write_text(get_random_secret_key())
    return path.read_text().strip()


SECRET_KEY = os.environ.get("SCHEMASYNC_SECRET_KEY") or _secret_key()
DEBUG = os.environ.get("SCHEMASYNC_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "validator",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"

ROOT_URLCONF = "schemasync.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
WSGI_APPLICATION = "schemasync.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "schemasync.sqlite3",
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"

# How long the extractor may take to import the target project.
EXTRACTOR_TIMEOUT_SECONDS = 180

# Background jobs run on a thread; SCHEMASYNC_JOBS_INLINE=1 runs them in the request.
JOBS_INLINE = os.environ.get("SCHEMASYNC_JOBS_INLINE", "0") == "1"
# Row backups taken before every delete / cleanup / column drop.
BACKUP_DIR = DATA_DIR / "backups"
# A previewed operation older than this must be previewed again before it can run.
PLAN_MAX_AGE_MINUTES = 15
