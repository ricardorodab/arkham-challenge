"""Isolated, synthetic configuration; never use the local service database."""

import os
import tempfile

os.environ.setdefault(
    "OUTAGE_DJANGO_SECRET_KEY",
    "synthetic-test-key-only-never-deploy-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
)
os.environ["OUTAGE_STATE_DIR"] = tempfile.mkdtemp(prefix="outage-test-state-")
os.environ["OUTAGE_LOCAL_HTTP"] = "1"

from outage_explorer.web.settings import *  # noqa: F403, E402

ALLOWED_HOSTS = ["testserver", "127.0.0.1", "localhost"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
