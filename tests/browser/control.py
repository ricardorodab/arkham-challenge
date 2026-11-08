"""Out-of-band browser fixture control, not an HTTP or deployed capability."""

from __future__ import annotations

import argparse
import json
import time

from .fixture_support import configure, publish, reset


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("reset")
    commands.add_parser("unpublish")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("username", choices=("viewer", "analyst", "admin"))
    revoke.add_argument("--kind", choices=("sessions", "inactive", "viewer"), default="sessions")
    worker = commands.add_parser("worker")
    worker.add_argument("--outcome", choices=("success", "failure"), default="success")
    worker.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()
    configure()
    import django

    django.setup()
    import logging

    logging.getLogger("outage_explorer").setLevel(logging.WARNING)
    logging.getLogger("outage_explorer.http").setLevel(logging.WARNING)
    if args.command == "reset":
        reset()
    elif args.command == "unpublish":
        # configure() confines this command to the run-ID browser fixture. Keep
        # immutable bundles intact; only clear its two current publication refs.
        import sqlite3

        from django.conf import settings

        with sqlite3.connect(settings.OUTAGE_DATA_DIR / "catalog.sqlite3") as database:
            database.execute("UPDATE publication SET current_snapshot_id=NULL WHERE singleton=1")
            database.execute("UPDATE model_publication SET current_model_id=NULL WHERE singleton=1")
    elif args.command == "revoke":
        from django.contrib.auth import get_user_model
        from django.contrib.sessions.models import Session

        from outage_explorer.service.models import Persona

        user = get_user_model().objects.get(username=args.username)
        if args.kind == "inactive":
            get_user_model().objects.filter(pk=user.pk).update(is_active=False)
        elif args.kind == "viewer":
            Persona.objects.filter(user=user).update(role="viewer")
        else:
            for session in Session.objects.all():
                if session.get_decoded().get("_auth_user_id") == str(user.pk):
                    session.delete()
    else:
        from outage_explorer.service.refresh import RefreshWorker

        def execute(mode):
            time.sleep(args.delay)
            return publish(revision=1, fail=args.outcome == "failure", mode=mode)

        with RefreshWorker(executor=execute) as consumer:
            job = consumer.run_once()
            print(
                json.dumps(
                    {
                        "job_id": str(job.pk) if job else None,
                        "status": job.status if job else "idle",
                    }
                )
            )


if __name__ == "__main__":
    main()
