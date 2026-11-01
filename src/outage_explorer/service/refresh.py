"""Database-backed refresh queue consumed only by a separately invoked worker."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from outage_explorer.domain import DataValidationError
from outage_explorer.eia import EIAClient, EIAClientError
from outage_explorer.pipeline import IngestConfig, ingest
from outage_explorer.storage import Catalog, StorageError, writer_lock

from .models import Persona, QueryCacheEntry, RefreshJob

LOG = logging.getLogger(__name__)


class RefreshServiceError(Exception):
    code = "refresh_error"
    public_message = "The refresh request could not be completed."

    def __init__(self) -> None:
        super().__init__(self.public_message)


class RefreshBusyError(RefreshServiceError):
    code = "refresh_busy"
    public_message = "The refresh queue is busy; retry the request shortly."


class RefreshPermissionError(RefreshServiceError):
    code = "forbidden"
    public_message = "An active Admin account is required to refresh data."


class RefreshValidationError(RefreshServiceError):
    code = "invalid_refresh_request"
    public_message = "Choose full or recent and provide a valid optional idempotency key."


class IdempotencyConflictError(RefreshServiceError):
    code = "idempotency_conflict"
    public_message = "This idempotency key was already used with another refresh mode."


class WorkerAlreadyRunning(RefreshBusyError):
    code = "worker_already_running"
    public_message = "Another refresh worker holds the worker control lock."


def _active_admin(user_id: Any) -> bool:
    return (
        get_user_model()
        .objects.filter(pk=user_id, is_active=True, persona__role=Persona.Role.ADMIN)
        .exists()
    )


def enqueue_refresh(user, mode: str, idempotency_key: str | None = None):
    """Insert under DB uniqueness constraints; no worker starts in this process."""
    try:
        return _enqueue_refresh(user, mode, idempotency_key)
    except OperationalError as error:
        sqlite_code = getattr(error.__cause__, "sqlite_errorcode", 0)
        if sqlite_code & 0xFF in {5, 6}:
            raise RefreshBusyError from None
        raise


def _enqueue_refresh(user, mode: str, idempotency_key: str | None):
    if not getattr(user, "is_authenticated", False) or not _active_admin(user.pk):
        raise RefreshPermissionError
    if mode not in RefreshJob.Mode.values:
        raise RefreshValidationError
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key) is None
    ):
        raise RefreshValidationError

    def replay():
        if idempotency_key is None:
            return None
        previous = RefreshJob.objects.filter(
            owner_id=user.pk, idempotency_key=idempotency_key
        ).first()
        if previous is not None and previous.mode != mode:
            raise IdempotencyConflictError
        return previous

    previous = replay()
    if previous is not None:
        return previous, False
    try:
        # No read precedes this INSERT inside the transaction: SQLite need not
        # upgrade a read snapshot to a write lock during competing requests.
        with transaction.atomic():
            job = RefreshJob.objects.create(
                owner_id=user.pk, mode=mode, idempotency_key=idempotency_key
            )
        return job, True
    except IntegrityError:
        previous = replay()
        if previous is not None:
            return previous, False
        if RefreshJob.objects.filter(status__in=["queued", "running"]).exists():
            raise RefreshBusyError from None
        raise


def perform_ingest(mode: str) -> dict[str, str]:
    """Only the worker calls this credential boundary; no key file is read."""
    api_key = os.environ.get("EIA_API_KEY", "").strip()
    client = EIAClient(api_key)
    manifest = ingest(IngestConfig(data_dir=Path(settings.OUTAGE_DATA_DIR), mode=mode), client)
    with Catalog(Path(settings.OUTAGE_DATA_DIR)) as catalog:
        model = catalog.current_model()
        if model is None or model.manifest["source_snapshot_id"] != manifest["snapshot_id"]:
            # An operator's CLI run may advance publication after ingest releases
            # its writer lock. Preserve this job's own immutable source binding.
            completed = next(
                (
                    item
                    for item in catalog.model_history(limit=1000)
                    if item["source_snapshot_id"] == manifest["snapshot_id"]
                    and item["status"] == "succeeded"
                ),
                None,
            )
            model = catalog.model(completed["model_id"]) if completed is not None else None
        if model is None or model.manifest["source_snapshot_id"] != manifest["snapshot_id"]:
            raise StorageError("Refresh publication could not be verified.", code="invalid_model")
    return {"snapshot_id": manifest["snapshot_id"], "model_id": model.id, "mode": mode}


def _public_failure(error: Exception) -> tuple[str, str]:
    if isinstance(error, EIAClientError):
        return (
            "upstream_refresh_failed",
            "EIA refresh did not complete. Review publication status before retrying.",
        )
    if isinstance(error, (DataValidationError, StorageError)):
        return (
            "refresh_validation_failed",
            "Refresh validation failed. Review publication status before retrying.",
        )
    return "refresh_failed", "Refresh failed safely. Check worker operations before retrying."


class RefreshWorker:
    """Hold an OS lock for the whole process lifetime, not a stale PID lease."""

    def __init__(self, *, executor: Callable[[str], dict[str, Any]] | None = None):
        self.executor = executor or perform_ingest
        self._lock = None
        self.recovered_count = 0

    def __enter__(self):
        if self._lock is not None:
            raise RuntimeError("A refresh worker context cannot be entered twice.")
        self._lock = writer_lock(Path(settings.OUTAGE_STATE_DIR) / "refresh-worker-control")
        try:
            self._lock.__enter__()
        except BaseException as error:
            self._lock = None
            if isinstance(error, StorageError) and error.code == "already_running":
                raise WorkerAlreadyRunning from None
            raise
        try:
            self.recovered_count = RefreshJob.objects.filter(status="running").update(
                status=RefreshJob.Status.INTERRUPTED,
                finished_at=timezone.now(),
                error_code="worker_interrupted",
                public_message="The previous worker stopped before recording completion. Review publication status before retrying.",
                result={},
            )
        except BaseException:
            self._lock.__exit__(None, None, None)
            self._lock = None
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._lock is not None:
            self._lock.__exit__(exc_type, exc, traceback)
            self._lock = None

    def run_once(self) -> RefreshJob | None:
        if self._lock is None:
            raise RuntimeError("The refresh worker control lock must be held.")
        job = RefreshJob.objects.filter(status="queued").order_by("created_at", "id").first()
        if job is None:
            return None
        claimed = RefreshJob.objects.filter(pk=job.pk, status="queued").update(
            status=RefreshJob.Status.RUNNING,
            started_at=timezone.now(),
            public_message="Refresh is running.",
        )
        if not claimed:
            return None
        LOG.info("refresh_job_started", extra={"run_id": str(job.pk), "mode": job.mode})
        if not _active_admin(job.owner_id):
            self._finish(
                job,
                RefreshJob.Status.FAILED,
                error_code="owner_permission_revoked",
                message="The refresh owner is no longer an active Admin.",
            )
            return job
        try:
            result = self.executor(job.mode)
            if not isinstance(result, dict) or any(
                not isinstance(result.get(field), str)
                or re.fullmatch(r"[0-9a-f]{32}", result[field]) is None
                for field in ("snapshot_id", "model_id")
            ):
                raise ValueError("The refresh result must identify the published source and model.")
            result = {
                "snapshot_id": result["snapshot_id"],
                "model_id": result["model_id"],
                "mode": job.mode,
            }
        except (KeyboardInterrupt, SystemExit):
            self._finish(
                job,
                RefreshJob.Status.INTERRUPTED,
                error_code="worker_interrupted",
                message="The worker was interrupted before recording completion.",
            )
            raise
        except Exception as error:
            code, message = _public_failure(error)
            self._finish(job, RefreshJob.Status.FAILED, error_code=code, message=message)
        else:
            self._finish(
                job, RefreshJob.Status.SUCCEEDED, message="Refresh completed.", result=result
            )
            try:
                QueryCacheEntry.objects.all().delete()
            except Exception:
                # Generation-bound cache keys prevent reuse even if cleanup fails.
                LOG.warning("refresh_cache_cleanup_failed")
        return job

    @staticmethod
    def _finish(job, status, *, error_code="", message="", result=None):
        RefreshJob.objects.filter(pk=job.pk, status="running").update(
            status=status,
            finished_at=timezone.now(),
            error_code=error_code,
            public_message=message,
            result=result or {},
        )
        job.refresh_from_db()
        LOG.info("refresh_job_" + status, extra={"run_id": str(job.pk), "error_code": error_code})
