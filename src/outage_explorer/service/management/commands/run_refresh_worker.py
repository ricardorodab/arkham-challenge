"""Consume durable refresh jobs in a dedicated process, never an API thread."""

import json
import logging
import os
import signal
import threading

from django.core.management.base import BaseCommand, CommandError

from outage_explorer.cli import JsonLogFormatter
from outage_explorer.service.refresh import RefreshWorker, WorkerAlreadyRunning


class Command(BaseCommand):
    help = "Run the single refresh worker; --once processes at most one queued job."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-interval", type=float, default=0.5)

    def handle(self, *args, **options):
        for handler in logging.getLogger("outage_explorer").handlers:
            handler.setFormatter(JsonLogFormatter(os.environ.get("EIA_API_KEY", "").strip()))
        interval = options["poll_interval"]
        if not 0.05 <= interval <= 5:
            raise CommandError("Poll interval must be between 0.05 and 5 seconds.")
        previous_handlers = {}

        def interrupted(signum, frame):
            raise KeyboardInterrupt

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, interrupted)
        try:
            with RefreshWorker() as worker:
                while True:
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
