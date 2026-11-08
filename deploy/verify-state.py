"""Compare private hashed deployment snapshots and public revision health responses."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def read(path):
    return json.loads(Path(path).read_text())


def preserved(before, after):
    """Require all pre-existing rows unchanged unless a schema migration was required."""
    if before["catalog"] != after["catalog"]:
        return False
    old, new = before["web"], after["web"]
    if not old["exists"]:
        return new["exists"]
    if old["schema"] == new["schema"]:
        return old["tables"] == new["tables"]
    # New tables/indexes may be added, but every existing application's table must retain
    # its rows. Dropping a table or changing its row representation requires operator review.
    # Django's applied-migration record is the sole expected metadata change.
    return all(
        new["tables"].get(name) == table
        for name, table in old["tables"].items()
        if name != "django_migrations"
    )


def current_database(database):
    spec = importlib.util.spec_from_file_location(
        "deployment_app_control", Path(__file__).with_name("app-control.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.database_state(Path(database))


def compatible(before, database):
    current = current_database(database)
    old = before["web"]
    return (
        old["exists"]
        and current["schema"] == old["schema"]
        and current["tables"].get("django_migrations") == old["tables"].get("django_migrations")
    )


def https(headers, body, revision):
    rows = [line.split(":", 1) for line in Path(headers).read_text().splitlines() if ":" in line]
    values = {key.strip().lower(): value.strip() for key, value in rows}
    return (
        values.get("x-outage-revision") == revision
        and read(body) == {"status": "ok"}
        and values.get("strict-transport-security", "").startswith("max-age=")
    )


if __name__ == "__main__":
    action, *arguments = sys.argv[1:]
    if action == "idle":
        success = read(arguments[0])["web"]["active_refresh"] == 0
    elif action == "idle-database":
        success = current_database(arguments[0])["active_refresh"] == 0
    elif action == "preserved":
        success = preserved(read(arguments[0]), read(arguments[1]))
    elif action == "compatible":
        success = compatible(read(arguments[0]), arguments[1])
    elif action == "https":
        success = https(*arguments)
    else:
        raise SystemExit("Unknown deployment verification action.")
    raise SystemExit(0 if success else 1)
