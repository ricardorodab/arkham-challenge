"""Build the Django WSGI callable used by the local Waitress launcher.

Import initializes Django from environment settings and exposes the one project containing
service and frontend apps. It does not launch the refresh worker.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "outage_explorer.web.settings")
application = get_wsgi_application()
