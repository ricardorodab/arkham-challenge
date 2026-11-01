"""Local WSGI entrypoint with explicit HTTP/body/concurrency limits."""

from __future__ import annotations

import argparse
import os

from waitress import serve


def main():
    parser = argparse.ArgumentParser(description="Serve the local Outage Explorer API")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "outage_explorer.web.settings")
    from .wsgi import application

    serve(
        application,
        host="127.0.0.1",
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
