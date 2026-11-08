"""Verify production upgrades preserve accounts and committed SQLite WAL data."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_deployment_module(filename):
    spec = importlib.util.spec_from_file_location(
        filename.replace("-", "_"), ROOT / "deploy" / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def initialization_environment(state):
    environment = dict(os.environ)
    environment.update(
        DJANGO_SETTINGS_MODULE="outage_explorer.web.settings",
        OUTAGE_STATE_DIR=str(state),
        OUTAGE_DATA_DIR=str(state.parent / "data"),
        OUTAGE_PROJECT_DIR=str(ROOT),
        OUTAGE_LOCAL_HTTP="1",
        OUTAGE_DJANGO_SECRET_KEY="synthetic-deployment-signing-secret-" * 3,
        OUTAGE_VIEWER_PASSWORD="Synthetic-Viewer-Deployment-Password-123!",
        OUTAGE_ANALYST_PASSWORD="Synthetic-Analyst-Deployment-Password-123!",
        OUTAGE_ADMIN_PASSWORD="Synthetic-Admin-Deployment-Password-123!",
    )
    environment.pop("OUTAGE_ALLOWED_HOSTS", None)
    environment.pop("OUTAGE_TRUSTED_PROXY", None)
    environment.pop("OUTAGE_DEPLOY_REVISION", None)
    return environment


def run_initialization(state):
    environment = initialization_environment(state)
    result = subprocess.run(
        [sys.executable, str(ROOT / "deploy" / "app-control.py"), "init"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for key, value in environment.items():
        if key == "OUTAGE_DJANGO_SECRET_KEY" or key.endswith("_PASSWORD"):
            assert value not in result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_upgrade_preserves_renamed_accounts_passwords_and_all_account_deletion(tmp_path):
    control = load_deployment_module("app-control.py")
    state = tmp_path / "state"
    state.mkdir()
    first = run_initialization(state)
    assert first["seeded"] is True
    database = state / "web.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE auth_user SET username='operator-admin' WHERE username='admin'")
    before = control.database_state(database)
    repeated = run_initialization(state)
    assert repeated["seeded"] is False
    assert repeated["migrations_applied"] is False
    assert repeated["migration_backup"] is None
    assert control.database_state(database) == before
    assert not list(state.glob("migration-backup-*.sqlite3"))
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM service_persona")
        connection.execute("DELETE FROM auth_user")
    deleted = control.database_state(database)
    assert run_initialization(state)["seeded"] is False
    assert control.database_state(database) == deleted
    assert deleted["tables"]["auth_user"]["rows"] == 0


def test_existing_database_migration_backs_up_committed_wal_and_never_seeds(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    database = state / "web.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE operator_note (value TEXT)")
        connection.execute("INSERT INTO operator_note VALUES ('committed-in-WAL')")
        connection.commit()
        assert Path(str(database) + "-wal").stat().st_size > 0
        result = run_initialization(state)
        assert result["seeded"] is False
        assert result["migrations_applied"] is True
        backup = state / result["migration_backup"]
        with sqlite3.connect(backup) as saved:
            assert saved.execute("SELECT value FROM operator_note").fetchone() == (
                "committed-in-WAL",
            )
        assert connection.execute("SELECT COUNT(*) FROM auth_user").fetchone() == (0,)
        assert connection.execute("SELECT value FROM operator_note").fetchone() == (
            "committed-in-WAL",
        )
    finally:
        connection.close()


def test_deployment_fingerprints_detect_rows_queue_and_incompatible_schema(tmp_path):
    control = load_deployment_module("app-control.py")
    verify = load_deployment_module("verify-state.py")
    database = tmp_path / "web.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE auth_user (id INTEGER PRIMARY KEY, password TEXT)")
        connection.execute("CREATE TABLE service_refreshjob (status TEXT)")
        connection.execute("INSERT INTO auth_user VALUES (1,'hashed-password')")
        connection.execute("INSERT INTO service_refreshjob VALUES ('succeeded')")
    before = {"web": control.database_state(database), "catalog": {"exists": False}}
    assert verify.preserved(before, before)
    assert verify.compatible(before, database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE service_refreshjob SET status='running'")
    after = {**before, "web": control.database_state(database)}
    assert after["web"]["active_refresh"] == 1
    assert not verify.preserved(before, after)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE service_refreshjob SET status='succeeded'")
        connection.execute("CREATE TABLE new_feature (value TEXT)")
    added = {**before, "web": control.database_state(database)}
    assert added["web"]["schema"] != before["web"]["schema"]
    assert verify.preserved(before, added)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM service_refreshjob")
    lost_history = {**before, "web": control.database_state(database)}
    assert not verify.preserved(before, lost_history)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE auth_user ADD COLUMN changed TEXT")
    assert not verify.compatible(before, database)


def test_public_health_requires_exact_revision_and_tls_policy(tmp_path):
    verify = load_deployment_module("verify-state.py")
    headers = tmp_path / "headers"
    body = tmp_path / "body"
    revision = "a" * 40
    headers.write_text(
        f"HTTP/2 200\nStrict-Transport-Security: max-age=31536000\nX-Outage-Revision: {revision}\n"
    )
    body.write_text('{"status":"ok"}')
    assert verify.https(headers, body, revision)
    assert not verify.https(headers, body, "b" * 40)
    headers.write_text(f"HTTP/2 200\nX-Outage-Revision: {revision}\n")
    assert not verify.https(headers, body, revision)
