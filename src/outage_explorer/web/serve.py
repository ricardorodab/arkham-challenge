"""Launch the local Django project through Waitress and its WSGI application.

Frontend HTML/assets and JSON service routes share this server and origin. The ASGI
application is an alternate integration entry point, not the server selected by web.local
serve. Refresh execution remains a separate process.
"""

from __future__ import annotations

import argparse
import os
from ipaddress import ip_address

from waitress import serve


def main():
    """Validate the bind address/port and serve with bounded HTTP/concurrency settings.

    Use twelve request threads and explicit body/header/connection/time limits; discard
    untrusted proxy headers and suppress tracebacks. This launcher does not provision HTTPS,
    start a refresh worker or install the analytical runtime. Loopback remains the
    default; containers explicitly select their internal 0.0.0.0 interface.
    """
    parser = argparse.ArgumentParser(description="Serve the local Outage Explorer API")
    parser.add_argument("--host", type=ip_address, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "outage_explorer.web.settings")
    from .wsgi import application

    serve(
        application,
        host=str(args.host),
        port=args.port,
        threads=12,
        max_request_body_size=65536,
        max_request_header_size=16384,
        connection_limit=32,
        channel_timeout=30,
        clear_untrusted_proxy_headers=True,
        expose_tracebacks=False,
    )


if __name__ == "__main__":
    main()
