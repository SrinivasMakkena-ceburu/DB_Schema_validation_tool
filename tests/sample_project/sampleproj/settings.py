"""Fixture project used by the validator's tests."""
import os

# Noise on stdout must not break the extractor's JSON output.
print("settings loaded")

SECRET_KEY = os.environ["SAMPLE_SECRET_KEY"]
INSTALLED_APPS = ["django.contrib.contenttypes", "catalog", "orders", "devices"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("SAMPLE_DB_NAME", "sample"),
        "HOST": os.environ.get("SAMPLE_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("SAMPLE_DB_PORT", "5432"),
        "USER": os.environ.get("SAMPLE_DB_USER", "postgres"),
        "PASSWORD": os.environ.get("SAMPLE_DB_PASSWORD", ""),
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
