"""Run production initialization and SQLite-safe deployment checks without credentials output.

This script is mounted from each immutable source release. Normal app and worker commands
read the Django signing key from a root-owned host env file; they never load seed passwords.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path


def database_state(path: Path) -> dict:
    """Read a consistent WAL-aware snapshot, hashing rows without exposing their contents."""
    if not path.exists():
        return {"exists": False, "schema": None, "tables": {}, "active_refresh": 0}
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        connection.execute("BEGIN")
        schema = connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
        names = [row[1] for row in schema if row[0] == "table"]
        tables = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            # Sorting per-row hashes is deterministic even for a table with no primary key.
            hashes = []
            for row in connection.execute(f"SELECT * FROM {quoted}"):
                encoded = json.dumps(
                    row, ensure_ascii=True, separators=(",", ":"), default=lambda v: v.hex()
                ).encode()
                hashes.append(hashlib.sha256(encoded).hexdigest())
            tables[name] = {
                "rows": len(hashes),
                "sha256": hashlib.sha256("".join(sorted(hashes)).encode()).hexdigest(),
            }
        active = 0
        if "service_refreshjob" in names:
            active = connection.execute(
                "SELECT COUNT(*) FROM service_refreshjob WHERE status IN ('queued','running')"
            ).fetchone()[0]
        return {
            "exists": True,
            "schema": hashlib.sha256(json.dumps(schema).encode()).hexdigest(),
            "tables": tables,
            "active_refresh": active,
        }
    finally:
        connection.close()


def initialize(state: Path) -> dict:
    """Migrate existing installations; seed only a new, never-served bootstrap attempt."""
    database = state / "web.sqlite3"
    pending = state / ".production-bootstrap-pending"
    initialized = state / ".production-initialized"
    # Record bootstrap intent before Django creates its database. A failed first init can
    # be retried, but an existing database without this marker can never reseed accounts.
    fresh = not database.exists() and not initialized.exists()
    if fresh and not pending.exists():
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    seed = pending.exists() and not initialized.exists()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "outage_explorer.web.settings")
    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    migration_pending = bool(executor.migration_plan(executor.loader.graph.leaf_nodes()))
    backup = None
    if migration_pending and not fresh and database.exists():
        # SQLite's backup API includes committed WAL records. Never copy only its main file.
        backup = state / f"migration-backup-{time.time_ns()}.sqlite3"
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
            source.backup(target)

    call_command("migrate", verbosity=0, interactive=False)
    if seed:
        call_command("seed_users", verbosity=0)
    initialized.touch(mode=0o600, exist_ok=True)
    pending.unlink(missing_ok=True)
    return {
        "initialized": True,
        "seeded": seed,
        "migrations_applied": migration_pending,
        "migration_backup": backup.name if backup else None,
    }


def health() -> dict:
    """Require HTTP health and the running image's expected revision through loopback."""
    request = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/health/live",
        headers={"Host": "localhost"},
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
        request, timeout=3
    ) as response:
        body = json.load(response)
        revision = response.headers.get("X-Outage-Revision")
        if response.status != 200 or body.get("status") != "ok":
            raise RuntimeError("Application health failed.")
        expected = os.environ.get("OUTAGE_DEPLOY_REVISION")
        if not expected or revision != expected:
            raise RuntimeError("Application revision health check failed.")
    return {"healthy": True, "revision": revision}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "inspect", "health"])
    action = parser.parse_args().action
    state = Path(os.environ.get("OUTAGE_STATE_DIR", "/appdata/state"))
    if action == "init":
        result = initialize(state)
    elif action == "health":
        result = health()
    else:
        result = {
            "web": database_state(state / "web.sqlite3"),
            "catalog": database_state(
                Path(os.environ.get("OUTAGE_DATA_DIR", "/appdata/data")) / "catalog.sqlite3"
            ),
        }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": "deployment_control_failed", "type": type(error).__name__}))
        sys.exit(1)
