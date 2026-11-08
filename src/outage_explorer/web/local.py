"""Provide explicit local init, HTTP serve and separate refresh-worker launchers.

init creates/loads private generated credentials, configures Django and migrates the application
database. It seeds accounts only for a fresh product state or an explicit password reset.
serve runs Waitress; worker calls the polling management
command in its own invocation. The launcher loads exactly its four generated Django/password
inputs, never .env, .env.example or an EIA key file. Other settings remain process-environment
inputs or defaults in web.settings.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
from ipaddress import ip_address
from pathlib import Path


def _configuration(state: Path, *, create: bool):
    """Return the private configuration path and its four validated generated inputs.

    Create a missing file only when create is true, with an exclusive write, fsync and
    restricted permissions for the newly generated file. Reject symlinks, extra keys or
    empty/nonstring values; remove an incomplete newly created file if setup fails. Keys are
    the Django signing secret and three independent seed passwords, not the application's
    general settings registry.
    """
    state.mkdir(parents=True, exist_ok=True)
    path = state / "local-config.json"
    if path.is_symlink():
        raise RuntimeError("The local configuration must be a regular private file.")
    if not path.exists():
        if not create:
            raise RuntimeError("Run the local init command first.")
        value = {
            "OUTAGE_DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
            **{
                f"OUTAGE_{role.upper()}_PASSWORD": secrets.token_urlsafe(24)
                for role in ("viewer", "analyst", "admin")
            },
        }
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == "nt":
                # Restrict only this newly generated file; no parent ACL changes.
                result = subprocess.run(
                    ["whoami", "/user", "/fo", "csv", "/nh"],
                    capture_output=True,
                    check=True,
                    text=True,
                )
                import csv

                sid = next(csv.reader([result.stdout.strip()]))[1]
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
                    capture_output=True,
                    check=True,
                )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "OUTAGE_DJANGO_SECRET_KEY",
        "OUTAGE_VIEWER_PASSWORD",
        "OUTAGE_ANALYST_PASSWORD",
        "OUTAGE_ADMIN_PASSWORD",
    }
    if set(value) != expected or any(not isinstance(v, str) or not v for v in value.values()):
        raise RuntimeError("The local configuration is invalid.")
    return path, value


def main():
    """Parse one local action, supply generated configuration and delegate its work.

    State defaults to cwd/.service unless OUTAGE_STATE_DIR is set. Override the process Django
    key with generated configuration, without autoloading environment files. init migrates
    then seeds only a fresh product state or an explicit reset; ordinary initialization
    preserves Admin-managed account names, roles, removals and passwords. serve removes upstream/seed
    secrets before Waitress; worker keeps its EIA key but removes seed passwords before
    run_refresh_worker. Emit safe setup failures and exit 2 without printing generated values.
    """
    parser = argparse.ArgumentParser(description="Initialize and run the local Django API")
    actions = parser.add_subparsers(dest="action", required=True)
    init = actions.add_parser("init", help="Create private random credentials, migrate and seed")
    init.add_argument("--reset-passwords", action="store_true")
    api = actions.add_parser("serve", help="Serve the API (loopback by default)")
    api.add_argument("--host", type=ip_address, default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    worker = actions.add_parser("worker", help="Consume durable Admin refresh jobs")
    worker.add_argument("--once", action="store_true")
    args = parser.parse_args()
    state = Path(os.environ.get("OUTAGE_STATE_DIR", Path.cwd() / ".service")).resolve()
    try:
        config_path, values = _configuration(state, create=args.action == "init")
        os.environ["OUTAGE_STATE_DIR"] = str(state)
        os.environ["OUTAGE_DJANGO_SECRET_KEY"] = values["OUTAGE_DJANGO_SECRET_KEY"]
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "outage_explorer.web.settings")
        if args.action == "serve":
            os.environ.pop("EIA_API_KEY", None)
            for role in ("viewer", "analyst", "admin"):
                os.environ.pop(f"OUTAGE_{role.upper()}_PASSWORD", None)
            from .serve import main as serve

            sys.argv = [sys.argv[0], "--host", str(args.host), "--port", str(args.port)]
            serve()
            return
        import django

        django.setup()
        from django.core.management import call_command

        if args.action == "init":
            for key, value in values.items():
                os.environ[key] = value
            call_command("migrate", verbosity=0, interactive=False)
            from outage_explorer.service.models import Persona

            seeded = args.reset_passwords or not Persona.objects.exists()
            if seeded:
                call_command("seed_users", reset_passwords=args.reset_passwords)
            print(
                json.dumps(
                    {
                        "initialized": True,
                        "credentials_file": str(config_path),
                        "seeded": seeded,
                        "users": ["viewer", "analyst", "admin"] if seeded else [],
                    }
                )
            )
        else:
            for role in ("viewer", "analyst", "admin"):
                os.environ.pop(f"OUTAGE_{role.upper()}_PASSWORD", None)
            call_command("run_refresh_worker", once=args.once)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(
            json.dumps({"error": "local_configuration_failed", "error_type": type(error).__name__}),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
