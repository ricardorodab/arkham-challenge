"""Durable queue, authorization, deduplication and real worker-death behavior."""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock
from urllib.parse import quote

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from outage_explorer.data.eia import EIAClientError
from outage_explorer.data.storage import Catalog
from outage_explorer.service import refresh
from outage_explorer.service.models import Persona, QueryCacheEntry, RefreshJob
from outage_explorer.service.refresh import (
    IdempotencyConflictError,
    RefreshBusyError,
    RefreshPermissionError,
    RefreshValidationError,
    RefreshWorker,
    WorkerAlreadyRunning,
    enqueue_refresh,
)
from tests.helpers import FakeClient


@pytest.fixture
def refresh_admin(db):
    user = get_user_model().objects.create_user(username="refresh-owner", password="synthetic")
    Persona.objects.create(user=user, role=Persona.Role.ADMIN)
    return user


@pytest.fixture
def control_directory(settings, tmp_path):
    settings.OUTAGE_STATE_DIR = tmp_path / "state"
    return settings.OUTAGE_STATE_DIR


def published_result():
    return {"snapshot_id": "a" * 32, "model_id": "b" * 32}


def test_enqueue_only_persists_and_replays_without_starting_work(refresh_admin, monkeypatch):
    monkeypatch.setattr(
        "outage_explorer.service.refresh.perform_ingest",
        lambda mode: pytest.fail("An enqueue request must never execute ingestion."),
    )
    job, created = enqueue_refresh(refresh_admin, "recent", "browser-retry-1")
    same, replay_created = enqueue_refresh(refresh_admin, "recent", "browser-retry-1")
    assert created and not replay_created and same.pk == job.pk
    assert job.status == "queued" and job.started_at is None
    with pytest.raises(IdempotencyConflictError):
        enqueue_refresh(refresh_admin, "full", "browser-retry-1")
    with pytest.raises(RefreshBusyError):
        enqueue_refresh(refresh_admin, "recent", "another-request")


@pytest.mark.parametrize("role", ["viewer", "analyst", None])
def test_enqueue_requires_fresh_admin_persona(refresh_admin, role):
    if role is None:
        refresh_admin.persona.delete()
    else:
        Persona.objects.filter(user=refresh_admin).update(role=role)
    with pytest.raises(RefreshPermissionError):
        enqueue_refresh(refresh_admin, "recent")


def test_enqueue_denies_deactivated_admin_even_with_cached_user(refresh_admin):
    get_user_model().objects.filter(pk=refresh_admin.pk).update(is_active=False)
    with pytest.raises(RefreshPermissionError):
        enqueue_refresh(refresh_admin, "recent")


@pytest.mark.django_db(transaction=True)
def test_account_deleted_after_permission_read_cannot_enqueue_an_orphan_job(
    refresh_admin, monkeypatch
):
    """Delete at the read/INSERT boundary and translate the real deferred FK failure."""
    original = refresh._active_admin
    removed = False

    def permission_then_delete(user_id):
        nonlocal removed
        allowed = original(user_id)
        if allowed and not removed:
            get_user_model().objects.filter(pk=user_id).delete()
            removed = True
        return allowed

    monkeypatch.setattr(refresh, "_active_admin", permission_then_delete)
    with pytest.raises(RefreshPermissionError):
        enqueue_refresh(refresh_admin, "recent")
    assert removed and not RefreshJob.objects.exists()
    assert not get_user_model().objects.filter(pk=refresh_admin.pk).exists()


@pytest.mark.parametrize(
    "mode,key",
    [("arbitrary", None), ("full", ""), ("recent", "x" * 129), ("recent", "contains spaces")],
)
def test_enqueue_validates_mode_and_bounded_idempotency_key(refresh_admin, mode, key):
    with pytest.raises(RefreshValidationError):
        enqueue_refresh(refresh_admin, mode, key)
    assert RefreshJob.objects.count() == 0


def test_database_constraint_prevents_direct_second_active_job(refresh_admin):
    RefreshJob.objects.create(owner=refresh_admin, mode="recent")
    with pytest.raises(IntegrityError), transaction.atomic():
        RefreshJob.objects.create(owner=refresh_admin, mode="full", status="running")


@pytest.mark.django_db(transaction=True)
def test_competing_queue_requests_have_at_most_one_winner():
    user = get_user_model().objects.create_user(username="race-owner", password="synthetic")
    Persona.objects.create(user=user, role="admin")
    barrier = Barrier(2)

    def compete(index):
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            try:
                job, created = enqueue_refresh(user, "recent", f"race-{index}")
                return "created" if created else str(job.pk)
            except RefreshBusyError:
                return "busy"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(compete, [1, 2]))
    assert outcomes.count("created") == 1
    assert outcomes.count("busy") == 1
    assert RefreshJob.objects.filter(status__in=["queued", "running"]).count() == 1


def test_worker_success_records_only_publication_ids_and_invalidates_cache(
    refresh_admin, control_directory
):
    QueryCacheEntry.objects.create(
        key="c" * 64,
        owner=refresh_admin,
        role="admin",
        model_id="a" * 32,
        publication_id="a" * 32,
        expires_at=timezone.now() + timedelta(seconds=60),
        payload={"rows": []},
    )
    job, _ = enqueue_refresh(refresh_admin, "recent")
    with RefreshWorker(
        executor=lambda mode: {**published_result(), "ignored_secret": "synthetic"}
    ) as worker:
        completed = worker.run_once()
        assert worker.run_once() is None
    job.refresh_from_db()
    assert completed.pk == job.pk and job.status == "succeeded"
    assert job.started_at is not None and job.finished_at >= job.started_at
    assert job.result == {**published_result(), "mode": "recent"}
    assert not QueryCacheEntry.objects.exists()


@pytest.mark.parametrize("revocation", ["role", "inactive"])
def test_worker_revalidates_owner_before_any_ingest(refresh_admin, control_directory, revocation):
    job, _ = enqueue_refresh(refresh_admin, "full")
    if revocation == "role":
        Persona.objects.filter(user=refresh_admin).update(role="viewer")
    else:
        get_user_model().objects.filter(pk=refresh_admin.pk).update(is_active=False)
    with RefreshWorker(
        executor=lambda mode: pytest.fail("Revoked owner must not execute.")
    ) as worker:
        worker.run_once()
    job.refresh_from_db()
    assert job.status == "failed" and job.error_code == "owner_permission_revoked"


@pytest.mark.parametrize("code", ["network", "configuration"])
def test_worker_failure_never_persists_exception_text(refresh_admin, control_directory, code):
    job, _ = enqueue_refresh(refresh_admin, "full")

    def failed(mode):
        raise EIAClientError(code, "synthetic-sensitive-exception-value")

    with RefreshWorker(executor=failed) as worker:
        worker.run_once()
    job.refresh_from_db()
    assert job.status == "failed" and job.error_code == "upstream_refresh_failed"
    assert "synthetic-sensitive-exception-value" not in job.public_message
    assert job.result == {}
    assert enqueue_refresh(refresh_admin, "recent")[1]


@pytest.mark.parametrize("key", [None, "", " \t\n "], ids=["absent", "empty", "whitespace"])
def test_unconfigured_worker_fails_job_without_source_io_and_accepts_next_job(
    refresh_admin, control_directory, settings, tmp_path, monkeypatch, key
):
    """Persist an actionable terminal failure while keeping a keyless worker usable."""
    settings.OUTAGE_DATA_DIR = tmp_path / "untouched-data"
    if key is None:
        monkeypatch.delenv("EIA_API_KEY", raising=False)
    else:
        monkeypatch.setenv("EIA_API_KEY", key)
    client = Mock(side_effect=AssertionError("Missing credentials must fail before client setup."))
    ingestion = Mock(side_effect=AssertionError("Missing credentials must not start ingestion."))
    monkeypatch.setattr(refresh, "EIAClient", client)
    monkeypatch.setattr(refresh, "ingest", ingestion)
    with RefreshWorker() as worker:
        job, created = enqueue_refresh(refresh_admin, "recent")
        assert created and job.status == "queued"
        completed = worker.run_once()
        job.refresh_from_db()
        assert completed.pk == job.pk and job.status == "failed"
        assert job.error_code == "refresh_not_configured"
        assert job.public_message == (
            "Refresh is not configured. Set EIA_API_KEY for the refresh worker, "
            "recreate the worker, and queue a new refresh."
        )
        assert job.started_at is not None and job.finished_at >= job.started_at
        assert job.result == {}
        assert worker.run_once() is None
        following, following_created = enqueue_refresh(refresh_admin, "full")
        assert following_created and following.pk != job.pk and following.status == "queued"
    client.assert_not_called()
    ingestion.assert_not_called()
    assert not settings.OUTAGE_DATA_DIR.exists()


def test_worker_publishes_source_and_model_together_and_preserves_both_on_failure(
    refresh_admin, control_directory, settings, tmp_path, monkeypatch
):
    """Exercise real ingest/model publication with a synthetic source, offline."""
    settings.OUTAGE_DATA_DIR = tmp_path / "synthetic-data"
    monkeypatch.setenv("EIA_API_KEY", "synthetic-worker-only-not-a-real-key")
    monkeypatch.setattr(refresh, "EIAClient", lambda key: FakeClient())
    with RefreshWorker() as worker:
        first, _ = enqueue_refresh(refresh_admin, "full")
        worker.run_once()
        first.refresh_from_db()
        assert first.status == "succeeded"
        assert first.progress["stage"] == "completed"
        assert first.progress["rows_received"] == 21
        assert first.progress["pages_fetched"] == 3
        assert first.progress["routes"] == {
            "national": {"rows_received": 3, "pages_fetched": 1},
            "facility": {"rows_received": 6, "pages_fetched": 1},
            "generator": {"rows_received": 12, "pages_fetched": 1},
        }
        with Catalog(settings.OUTAGE_DATA_DIR) as catalog:
            source_id = catalog.current().id
            model_id = catalog.current_model().id
            assert catalog.current_model().manifest["source_snapshot_id"] == source_id
        assert first.result == {"snapshot_id": source_id, "model_id": model_id, "mode": "full"}
        QueryCacheEntry.objects.create(
            key="d" * 64,
            owner=refresh_admin,
            role="admin",
            model_id=model_id,
            publication_id=source_id,
            expires_at=timezone.now() + timedelta(seconds=60),
            payload={"rows": []},
        )
        failing = FakeClient()
        failing.fail_route = "generator-nuclear-outages"
        monkeypatch.setattr(refresh, "EIAClient", lambda key: failing)
        second, _ = enqueue_refresh(refresh_admin, "recent")
        worker.run_once()
    second.refresh_from_db()
    assert second.status == "failed"
    with Catalog(settings.OUTAGE_DATA_DIR) as catalog:
        assert (catalog.current().id, catalog.current_model().id) == (source_id, model_id)
    assert QueryCacheEntry.objects.count() == 1


def test_worker_loads_rotated_file_credential_without_secret_environment_values(
    refresh_admin, control_directory, settings, tmp_path, monkeypatch
):
    settings.OUTAGE_DATA_DIR = tmp_path / "synthetic-file-data"
    secret = tmp_path / "eia-secret"
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.setenv("OUTAGE_SECRET_FILES_ONLY", "1")
    monkeypatch.setenv("EIA_API_KEY_FILE", str(secret))
    observed = []

    def client(key):
        observed.append(key)
        assert "EIA_API_KEY" not in os.environ
        return FakeClient()

    monkeypatch.setattr(refresh, "EIAClient", client)
    with RefreshWorker() as worker:
        for key in ("synthetic-first-file-key", "synthetic-rotated-file-key"):
            secret.write_text(key, encoding="utf-8")
            job, _ = enqueue_refresh(refresh_admin, "full")
            worker.run_once()
            job.refresh_from_db()
            assert job.status == "succeeded"
            assert key not in json.dumps(job.result)
    assert observed == ["synthetic-first-file-key", "synthetic-rotated-file-key"]


def test_missing_worker_secret_file_fails_safely_before_upstream_io(
    refresh_admin, control_directory, settings, tmp_path, monkeypatch
):
    settings.OUTAGE_DATA_DIR = tmp_path / "untouched-data"
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.setenv("OUTAGE_SECRET_FILES_ONLY", "1")
    monkeypatch.setenv("EIA_API_KEY_FILE", str(tmp_path / "private-missing-path-canary"))
    client = Mock(side_effect=AssertionError("An unavailable key must not reach EIA."))
    monkeypatch.setattr(refresh, "EIAClient", client)
    with RefreshWorker() as worker:
        job, _ = enqueue_refresh(refresh_admin, "recent")
        worker.run_once()
    job.refresh_from_db()
    assert job.status == "failed" and job.error_code == "refresh_not_configured"
    assert "canary" not in job.public_message
    client.assert_not_called()


def test_worker_command_redacts_file_credentials_and_refreshes_between_jobs(
    refresh_admin, control_directory, settings, tmp_path, monkeypatch, capsys
):
    """Emit unsafe source logs through the real command, pipeline and rotating file source."""
    from outage_explorer.service.management.commands import run_refresh_worker as command

    settings.OUTAGE_DATA_DIR = tmp_path / "redaction-data"
    path = tmp_path / "eia-secret"
    keys = ["synthetic-first-file-canary/+?=", "synthetic-rotated-file-canary/+?="]
    path.write_text(keys[0], encoding="utf-8")
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.setenv("EIA_API_KEY_FILE", str(path))
    monkeypatch.setenv("OUTAGE_SECRET_FILES_ONLY", "1")
    received = []
    stream = io.StringIO()
    logger = logging.getLogger("outage_explorer")
    monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(stream)])
    monkeypatch.setattr(logger, "level", logging.INFO)
    monkeypatch.setattr(logger, "propagate", False)

    def client(key):
        received.append(key)
        logger.warning(
            "synthetic-upstream raw=%s encoded=%s https://example.invalid/?api_key=unknown-query-canary",
            key,
            quote(key, safe=""),
            extra={"route": key, "mode": {"api_key": key, "nested": quote(key, safe="")}},
        )
        return FakeClient()

    class TwoJobs(RefreshWorker):
        calls = 0

        def run_once(self):
            if self.calls == 2:
                raise KeyboardInterrupt
            result = super().run_once()
            self.calls += 1
            if self.calls == 1:
                path.write_text(keys[1], encoding="utf-8")
                enqueue_refresh(refresh_admin, "full")
            return result

    monkeypatch.setattr(refresh, "EIAClient", client)
    monkeypatch.setattr(command, "RefreshWorker", TwoJobs)
    enqueue_refresh(refresh_admin, "full")
    call_command("run_refresh_worker", poll_interval=0.05)
    assert received == keys
    assert RefreshJob.objects.filter(status="succeeded").count() == 2
    captured = capsys.readouterr()
    emitted = stream.getvalue() + captured.out + captured.err
    assert all(key not in emitted and quote(key, safe="") not in emitted for key in keys)
    assert "unknown-query-canary" not in emitted
    source_events = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["event"].startswith("synthetic-upstream")
    ]
    assert len(source_events) == 2
    assert all("[REDACTED]" in record["event"] for record in source_events)
    assert all(record["route"] == "[REDACTED]" for record in source_events)


def test_worker_command_can_start_without_optional_file_credential(
    control_directory, db, tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.setenv("OUTAGE_SECRET_FILES_ONLY", "1")
    monkeypatch.setenv("EIA_API_KEY_FILE", str(tmp_path / "unconfigured"))
    call_command("run_refresh_worker", once=True)
    assert capsys.readouterr().out == ""


def test_cache_cleanup_is_best_effort_after_success(refresh_admin, control_directory, monkeypatch):
    def cleanup_failure():
        raise RuntimeError("synthetic-cleanup-failure-must-not-escape")

    class FailedCleanup:
        delete = staticmethod(cleanup_failure)

    monkeypatch.setattr(QueryCacheEntry.objects, "all", lambda: FailedCleanup())
    job, _ = enqueue_refresh(refresh_admin, "recent")
    with RefreshWorker(executor=lambda mode: published_result()) as worker:
        assert worker.run_once().status == "succeeded"
    job.refresh_from_db()
    assert job.status == "succeeded"


def test_worker_retains_its_completed_pair_when_another_publication_advances(
    refresh_admin, control_directory, settings, tmp_path, monkeypatch
):
    settings.OUTAGE_DATA_DIR = tmp_path / "synthetic-data"
    monkeypatch.setenv("EIA_API_KEY", "synthetic-worker-only-not-a-real-key")
    monkeypatch.setattr(refresh, "EIAClient", lambda key: FakeClient())
    original_ingest = refresh.ingest
    completed = {}

    def publish_then_advance(config, client, **kwargs):
        own = original_ingest(config, client, **kwargs)
        with Catalog(settings.OUTAGE_DATA_DIR) as catalog:
            completed.update(snapshot_id=own["snapshot_id"], model_id=catalog.current_model().id)
        original_ingest(config, FakeClient())
        return own

    monkeypatch.setattr(refresh, "ingest", publish_then_advance)
    job, _ = enqueue_refresh(refresh_admin, "full")
    with RefreshWorker() as worker:
        worker.run_once()
    job.refresh_from_db()
    assert job.status == "succeeded" and job.result == {**completed, "mode": "full"}
    with Catalog(settings.OUTAGE_DATA_DIR) as catalog:
        assert catalog.current().id != completed["snapshot_id"]


def test_live_worker_cannot_be_mistaken_for_a_dead_one(refresh_admin, control_directory):
    with RefreshWorker(executor=lambda mode: published_result()):
        job = RefreshJob.objects.create(owner=refresh_admin, mode="recent", status="running")
        with pytest.raises(WorkerAlreadyRunning), RefreshWorker():
            pytest.fail("A second live worker must not acquire the control lock.")
        job.refresh_from_db()
        assert job.status == "running"
    with RefreshWorker() as worker:
        assert worker.recovered_count == 1
    job.refresh_from_db()
    assert job.status == "interrupted" and job.error_code == "worker_interrupted"


def test_real_process_death_releases_lock_and_recovers_durable_running_job(tmp_path):
    """Use a separate real DB/process; never involve the operator's data or key."""
    state_dir = tmp_path / "state"
    marker = tmp_path / "running.json"
    child_env = {
        k: v for k, v in os.environ.items() if k not in {"EIA_API_KEY", "PYTHONPATH", "PYTHONHOME"}
    }
    child_env.update(
        DJANGO_SETTINGS_MODULE="outage_explorer.web.settings",
        OUTAGE_DJANGO_SECRET_KEY="synthetic-subprocess-key-only-not-a-deployment-secret",
        OUTAGE_STATE_DIR=str(state_dir),
        OUTAGE_DATA_DIR=str(tmp_path / "no-source-data"),
        OUTAGE_LOCAL_HTTP="1",
        SYNTHETIC_READY_FILE=str(marker),
        PYTHONUTF8="1",
    )
    bootstrap = """
import json, os, time
from pathlib import Path
import django
django.setup()
from django.core.management import call_command
from django.contrib.auth import get_user_model
from outage_explorer.service.models import Persona
from outage_explorer.service.refresh import enqueue_refresh, RefreshWorker
call_command('migrate', interactive=False, verbosity=0, skip_checks=True)
user=get_user_model().objects.create_user(username='process-owner',password='synthetic-only')
Persona.objects.create(user=user,role='admin')
job,_=enqueue_refresh(user,'recent')
def blocked(mode):
    Path(os.environ['SYNTHETIC_READY_FILE']).write_text(json.dumps({'job_id':str(job.pk)}))
    time.sleep(120)
    raise AssertionError('The test must terminate this process first.')
with RefreshWorker(executor=blocked) as worker:
    worker.run_once()
"""
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", bootstrap],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 25
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                pytest.fail(f"Synthetic worker exited before becoming ready: {stdout} {stderr}")
            time.sleep(0.05)
        assert marker.exists(), "Synthetic worker did not become ready in time."
        with sqlite3.connect(state_dir / "web.sqlite3") as db:
            assert db.execute("SELECT status FROM service_refreshjob").fetchone()[0] == "running"
        command = [
            sys.executable,
            "-I",
            "-m",
            "outage_explorer.web.manage",
            "run_refresh_worker",
            "--once",
        ]
        live = subprocess.run(command, env=child_env, capture_output=True, text=True, timeout=15)
        assert live.returncode != 0 and "worker control lock" in live.stderr
        with sqlite3.connect(state_dir / "web.sqlite3") as db:
            assert db.execute("SELECT status FROM service_refreshjob").fetchone()[0] == "running"
        process.kill()
        process.communicate(timeout=10)
        recovered = subprocess.run(
            command, env=child_env, capture_output=True, text=True, timeout=15
        )
        assert recovered.returncode == 0, recovered.stderr
        with sqlite3.connect(state_dir / "web.sqlite3") as db:
            assert db.execute("SELECT status,error_code FROM service_refreshjob").fetchone() == (
                "interrupted",
                "worker_interrupted",
            )
        assert not (tmp_path / "no-source-data").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
