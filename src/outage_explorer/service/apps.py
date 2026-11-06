"""Register the service package with Django's application/model discovery."""

from django.apps import AppConfig


class ServiceConfig(AppConfig):
    """Identify operational service models and the default surrogate-key type.

    Registration does not start a worker or serve a separate backend process.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "outage_explorer.service"
