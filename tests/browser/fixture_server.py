"""Run the browser suite's isolated synthetic Waitress service on loopback.

This test-only entry point configures run-ID paths before Django import/setup. It never reads
the operator's key/private accounts or opens their data directory.
"""

from __future__ import annotations

from .fixture_support import configure, reset


def main() -> None:
    """Prepare fixture schema/state and serve the test application on port 8766.

    configure runs before Django setup; migrate/reset operate only on selected synthetic
    state. The Playwright launcher owns this process and its cleanup; refresh success uses a
    separate fixture worker, not an HTTP background thread.
    """
    configure()
    import django

    django.setup()
    import logging

    logging.getLogger("outage_explorer").setLevel(logging.WARNING)
    logging.getLogger("outage_explorer.http").setLevel(logging.WARNING)
    from django.core.management import call_command

    call_command("migrate", verbosity=0, interactive=False)
    reset()
    from waitress import serve

    from outage_explorer.web.wsgi import application

    logging.getLogger("outage_explorer").setLevel(logging.WARNING)
    logging.getLogger("outage_explorer.http").setLevel(logging.WARNING)
    serve(
        application,
        host="127.0.0.1",
        port=8766,
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
