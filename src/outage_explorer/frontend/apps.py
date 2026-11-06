"""Register the browser-shell application in the shared Django project."""

from django.apps import AppConfig


class FrontendConfig(AppConfig):
    """Django application metadata for packaged templates and static resources."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "outage_explorer.frontend"
