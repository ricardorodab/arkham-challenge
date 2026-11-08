"""Real job progress is durable, bounded and independent of publication outcomes."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.db import OperationalError, connection
from django.db.migrations.executor import MigrationExecutor

from outage_explorer.progress import ProgressEvent
from outage_explorer.service import job_progress, refresh
from outage_explorer.service.job_progress import JobProgressReporter
from outage_explorer.service.models import Persona, RefreshJob


def mocked_reporter(monkeypatch):
    writes = []
    query = Mock()
    query.update.side_effect = lambda **values: writes.append(deepcopy(values["progress"]))
    manager = Mock()
    manager.filter.return_value = query
    monkeypatch.setattr(job_progress, "RefreshJob", SimpleNamespace(objects=manager))
    now = [0.0]
    return JobProgressReporter("synthetic", clock=lambda: now[0]), writes, now, manager


def test_parallel_page_deltas_are_aggregated_with_bounded_writes(monkeypatch):
    reporter, writes, now, manager = mocked_reporter(monkeypatch)
    reporter(ProgressEvent("extracting"))
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(
            pool.map(lambda _: reporter(ProgressEvent("extracting", "facility", 50, 1)), range(90))
        )
    assert len(writes) == 1
    now[0] = 1.01
    reporter(ProgressEvent("extracting", "national", 3, 1))
    assert len(writes) == 2
    assert writes[-1]["rows_received"] == 4503
    assert writes[-1]["pages_fetched"] == 91
    assert writes[-1]["routes"]["facility"] == {"rows_received": 4500, "pages_fetched": 90}
    reporter(ProgressEvent("model_sources"))
    assert len(writes) == 3  # Meaningful stage changes bypass the page throttle.
    assert "route" not in writes[-1]
    reporter.flush()
    assert len(writes) == 4
    assert all(
        call.kwargs == {"pk": "synthetic", "status": "running"}
        for call in manager.filter.call_args_list
    )


@pytest.mark.parametrize("error_type", [OperationalError, OSError, ValueError])
def test_progress_failure_is_throttled_and_does_not_escape(monkeypatch, caplog, error_type):
    reporter, writes, now, manager = mocked_reporter(monkeypatch)
    manager.filter.return_value.update.side_effect = error_type("private path and key")
    reporter(ProgressEvent("extracting", "facility", 10, 1))
    for _ in range(40):
        reporter(ProgressEvent("extracting", "facility", 10, 1))
    assert manager.filter.return_value.update.call_count == 1
    now[0] = 1.01
    reporter(ProgressEvent("extracting", "facility", 10, 1))
    assert manager.filter.return_value.update.call_count == 2
    assert "private path" not in caplog.text
    assert caplog.text.count("refresh_progress_write_failed") == 1
    manager.filter.return_value.update.side_effect = lambda **values: writes.append(
        values["progress"]
    )
    reporter.flush()
    assert writes[-1]["rows_received"] == 420


def test_route_connection_cleanup_failure_cannot_escape_reporting(monkeypatch):
    reporter, writes, _, _ = mocked_reporter(monkeypatch)
    close = Mock(side_effect=OSError("private connection details"))
    monkeypatch.setattr(job_progress, "connections", {"default": SimpleNamespace(close=close)})
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(reporter, ProgressEvent("extracting", "national", 3, 1)).result()
    assert close.call_count == 1 and writes[-1]["rows_received"] == 3


@pytest.mark.django_db
@pytest.mark.parametrize("outcome", ["success", "failure", "interrupt"])
def test_ordinary_progress_errors_cannot_change_worker_outcome(
    settings, tmp_path, monkeypatch, outcome
):
    user = get_user_model().objects.create_user(username="broken-observer")
    Persona.objects.create(user=user, role="admin")
    settings.OUTAGE_STATE_DIR = tmp_path / "broken-observer-state"
    job, _ = refresh.enqueue_refresh(user, "full")
    monkeypatch.setattr(
        job_progress.JobProgressReporter, "_write", Mock(side_effect=OSError("private observer"))
    )

    def execute(mode):
        if outcome == "failure":
            raise ValueError("private executor")
        if outcome == "interrupt":
            raise KeyboardInterrupt
        return {"snapshot_id": "a" * 32, "model_id": "b" * 32}

    with refresh.RefreshWorker(executor=execute) as worker:
        if outcome == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                worker.run_once()
        else:
            worker.run_once()
    job.refresh_from_db()
    assert (
        job.status
        == {"success": "succeeded", "failure": "failed", "interrupt": "interrupted"}[outcome]
    )
    assert "private" not in job.public_message + str(job.progress)


@pytest.mark.django_db
@pytest.mark.parametrize("interrupted", [False, True])
def test_terminal_failure_preserves_last_real_event(settings, tmp_path, monkeypatch, interrupted):
    user = get_user_model().objects.create_user(username="progress-owner")
    Persona.objects.create(user=user, role="admin")
    settings.OUTAGE_STATE_DIR = tmp_path / "isolated-state"
    job, _ = refresh.enqueue_refresh(user, "full")

    def execute(mode, *, progress):
        assert mode == "full"
        progress(ProgressEvent("extracting", "generator", 12, 1))
        if interrupted:
            raise KeyboardInterrupt
        raise RuntimeError("private SQL, path and credential")

    monkeypatch.setattr(refresh, "perform_ingest", execute)
    with refresh.RefreshWorker() as worker:
        if interrupted:
            with pytest.raises(KeyboardInterrupt):
                worker.run_once()
        else:
            worker.run_once()
    job.refresh_from_db()
    assert job.status == ("interrupted" if interrupted else "failed")
    assert job.progress["stage"] == "extracting"
    assert job.progress["rows_received"] == 12
    assert job.progress["pages_fetched"] == 1
    assert "private" not in str(job.progress) + job.public_message
    snapshot = job.progress
    # A late route callback cannot mutate a terminal job.
    reporter = JobProgressReporter(job.pk)
    reporter(ProgressEvent("completed"))
    job.refresh_from_db()
    assert job.progress == snapshot


@pytest.mark.django_db(transaction=True)
def test_progress_migration_preserves_existing_jobs_and_defaults_to_unknown():
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    previous = [("service", "0001_initial")]
    current = [("service", "0002_refreshjob_progress")]
    executor.migrate(previous)
    try:
        user = get_user_model().objects.create_user(username="legacy-progress")
        legacy = executor.loader.project_state(previous).apps.get_model("service", "RefreshJob")
        job = legacy.objects.create(owner_id=user.pk, mode="full", status="failed", result={})
        executor = MigrationExecutor(connection)
        executor.migrate(current)
        restored = RefreshJob.objects.get(pk=job.pk)
        assert restored.status == "failed" and restored.progress == {} and restored.result == {}
        assert restored.owner_id == user.pk and restored.created_at == job.created_at
    finally:
        # Restore the complete current schema rather than leaving later account
        # and saved-view migrations unapplied for subsequent transaction tests.
        MigrationExecutor(connection).migrate(latest)
