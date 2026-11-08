"""Configure the single Django project from process environment and code defaults.

No dotenv file is loaded. Direct setup requires a strong Django secret; web.local instead
supplies its generated key and explicit state path. Import resolves paths and creates the
state directory. web.sqlite3 stores users/sessions/jobs/cache, separate from
data/catalog.sqlite3 and immutable Parquet. Hosted deployments supply explicit allowed hosts
and disable local HTTP; the Waitress launcher validates its optional trusted proxy separately.
"""

from __future__ import annotations

import os
import re
from ipaddress import ip_address
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(os.environ.get("OUTAGE_PROJECT_DIR", Path.cwd())).resolve()
SECRET_KEY = os.environ.get("OUTAGE_DJANGO_SECRET_KEY", "").strip()
if len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured(
        "OUTAGE_DJANGO_SECRET_KEY must be configured with at least 50 characters, "
        "at least five distinct characters, and no django-insecure- prefix."
    )

DEBUG = os.environ.get("OUTAGE_DJANGO_DEBUG", "0") == "1"


def _allowed_hosts(value: str) -> list[str]:
    """Accept explicit host names/IPs, never URL prefixes, ports or wildcard hosts."""
    hosts = [host.strip().lower() for host in value.split(",")]
    for host in hosts:
        if host.startswith("[") and host.endswith("]"):
            try:
                valid = ip_address(host[1:-1]).version == 6
            except ValueError:
                valid = False
        else:
            valid = len(host) <= 253 and re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*",
                host,
            )
        if not valid:
            raise ImproperlyConfigured(
                "OUTAGE_ALLOWED_HOSTS must contain comma-separated explicit host names or IPs."
            )
    return list(dict.fromkeys(hosts))


ALLOWED_HOSTS = _allowed_hosts(os.environ.get("OUTAGE_ALLOWED_HOSTS", "127.0.0.1,localhost"))
OUTAGE_DEPLOY_REVISION = os.environ.get("OUTAGE_DEPLOY_REVISION", "").strip().lower()
if OUTAGE_DEPLOY_REVISION and not re.fullmatch(
    r"[0-9a-f]{40}|[0-9a-f]{64}", OUTAGE_DEPLOY_REVISION
):
    raise ImproperlyConfigured("OUTAGE_DEPLOY_REVISION must be a full hexadecimal Git object ID.")
OUTAGE_LOCAL_HTTP = os.environ.get("OUTAGE_LOCAL_HTTP", "1") == "1"
OUTAGE_DATA_DIR = Path(os.environ.get("OUTAGE_DATA_DIR", BASE_DIR / "data")).resolve()
OUTAGE_STATE_DIR = Path(os.environ.get("OUTAGE_STATE_DIR", BASE_DIR / ".service")).resolve()
OUTAGE_STATE_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR = OUTAGE_DATA_DIR
STATE_DIR = OUTAGE_STATE_DIR
OUTAGE_QUERY_TIMEOUT_SECONDS = 8
OUTAGE_QUERY_MAX_ROWS = 1000
OUTAGE_QUERY_MAX_BYTES = 2_097_152

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "outage_explorer.service.apps.ServiceConfig",
    "outage_explorer.frontend.apps.FrontendConfig",
]
MIDDLEWARE = [
    "outage_explorer.service.middleware.RequestMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "outage_explorer.service.localization.InterfaceLocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "outage_explorer.web.urls"
WSGI_APPLICATION = "outage_explorer.web.wsgi.application"
ASGI_APPLICATION = "outage_explorer.web.asgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": OUTAGE_STATE_DIR / "web.sqlite3",
        "OPTIONS": {
            "timeout": 10,
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;",
        },
        "ATOMIC_REQUESTS": False,
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("es", "Español")]
LANGUAGE_COOKIE_NAME = "outage_language"
LANGUAGE_COOKIE_AGE = 365 * 24 * 60 * 60
LANGUAGE_COOKIE_SAMESITE = "Lax"
LANGUAGE_COOKIE_SECURE = not OUTAGE_LOCAL_HTTP
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not OUTAGE_LOCAL_HTTP
SESSION_COOKIE_AGE = 1800
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not OUTAGE_LOCAL_HTTP
CSRF_FAILURE_VIEW = "outage_explorer.service.views.csrf_failure"
SECURE_SSL_REDIRECT = not OUTAGE_LOCAL_HTTP
# The private container health probe uses HTTP and never creates a session or reads data.
SECURE_REDIRECT_EXEMPT = [r"^api/v1/health/live$"]
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 0
STATIC_URL = "/assets/"
STATIC_ROOT = OUTAGE_STATE_DIR / "static"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "http": {"format": "%(message)s"},
        "operations": {"()": "outage_explorer.cli.JsonLogFormatter"},
    },
    "handlers": {
        "http": {"class": "logging.StreamHandler", "formatter": "http"},
        "operations": {"class": "logging.StreamHandler", "formatter": "operations"},
    },
    "loggers": {
        "outage_explorer": {"handlers": ["operations"], "level": "INFO", "propagate": False},
        "outage_explorer.http": {"handlers": ["http"], "level": "INFO", "propagate": False},
    },
}
