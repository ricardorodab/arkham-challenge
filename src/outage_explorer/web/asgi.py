"""Expose an alternate Django ASGI callable for an ASGI-capable host.

The current local serve command uses Waitress/WSGI instead. ASGI initialization does not make
ingestion asynchronous or start a refresh worker.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "outage_explorer.web.settings")
application = get_asgi_application()
