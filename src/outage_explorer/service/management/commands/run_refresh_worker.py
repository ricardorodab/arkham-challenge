"""Poll durable refresh jobs in a separately launched Django management process.

The HTTP endpoint only enqueues jobs. This command owns the loop around the stateful
RefreshWorker context, sanitized worker logs and signal cleanup.
"""

import json
import signal
import threading

from django.core.management.base import BaseCommand, CommandError

from outage_explorer.runtime_secrets import SecretConfigurationError, read_secret
from outage_explorer.service.refresh import (
    RefreshWorker,
    WorkerAlreadyRunning,
    configure_worker_logging,
)


def _refresh_log_redaction() -> None:
    """Refresh file-backed redaction without making an unconfigured worker fail startup."""
    try:
        key = read_secret("EIA_API_KEY").strip()
    except SecretConfigurationError:
        # perform_ingest reports invalid/missing credentials as a safe job failure. Reading
        # the environment directly here would bypass production's file-only policy.
        key = ""
    configure_worker_logging(key)


class Command(BaseCommand):
    """Run the sole refresh consumer, or consume at most one job with --once."""

    help = "Run the single refresh worker; --once processes at most one queued job."
    requires_system_checks = []

    def add_arguments(self, parser):
        """Declare one-job mode and a bounded idle polling interval."""
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-interval", type=float, default=0.5)

    def handle(self, *args, **options):
        """Hold worker ownership while polling run_once and reporting safe job outcomes.

        Validate the poll interval, install interruption handlers only on the main thread, and
        restore prior handlers on exit. Wait only when idle; --once exits after one attempt
        even if the queue is empty. Worker context cleanup releases the OS control lock;
        Ctrl+C is reported as stopped, not as an ingestion rollback.
        """
        _refresh_log_redaction()
        interval = options["poll_interval"]
        if not 0.05 <= interval <= 5:
            raise CommandError("Poll interval must be between 0.05 and 5 seconds.")
        previous_handlers = {}

        def interrupted(signum, frame):
            """Translate process termination signals into the worker's interruption path.

            The enclosing command temporarily installs this callback and restores previous
            handlers in finally. The active worker can then record interruption and unwind.
            """
            raise KeyboardInterrupt

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, interrupted)
        try:
            with RefreshWorker() as worker:
                while True:
                    _refresh_log_redaction()
                    job = worker.run_once()
                    if job is not None:
                        self.stdout.write(json.dumps({"job_id": str(job.pk), "status": job.status}))
                    if options["once"]:
                        break
                    if job is None:
                        threading.Event().wait(interval)
        except WorkerAlreadyRunning as error:
            raise CommandError(error.public_message) from None
        except KeyboardInterrupt:
            self.stdout.write(json.dumps({"status": "stopped"}))
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
