"""ASGI entry point; refresh execution stays in a separate worker process."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "outage_explorer.web.settings")
application = get_asgi_application()
