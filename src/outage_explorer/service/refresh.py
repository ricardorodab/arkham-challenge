"""Queue durable Admin refresh requests and execute them in a separate worker.

HTTP enqueueing never starts a thread or worker process. The management command owns the
polling loop; RefreshWorker retains its control lock and injectable executor across jobs.
Data-catalog raw/model publication and Django job completion are separate transactions, so an
interrupted or failed job may already have published data. Operators must inspect
authoritative publication history.
"""

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
    """Base category for safe refresh request failures, not an executable worker."""

    code = "refresh_error"
    public_message = "The refresh request could not be completed."

    def __init__(self) -> None:
        """Initialize the exception with its category's safe public message."""
        super().__init__(self.public_message)


class RefreshBusyError(RefreshServiceError):
    """Report queue/database contention that a caller may retry later."""

    code = "refresh_busy"
    public_message = "The refresh queue is busy; retry the request shortly."


class RefreshPermissionError(RefreshServiceError):
    """Report that enqueueing requires a currently active product Admin."""

    code = "forbidden"
    public_message = "An active Admin account is required to refresh data."


class RefreshValidationError(RefreshServiceError):
    """Report an unsupported mode or malformed optional idempotency key."""

    code = "invalid_refresh_request"
    public_message = "Choose full or recent and provide a valid optional idempotency key."


class RefreshConfigurationError(RefreshServiceError):
    """Report missing worker-only credentials without exposing or accepting their value."""

    code = "refresh_not_configured"
    public_message = (
        "Refresh is not configured. Set EIA_API_KEY for the refresh worker, "
        "recreate the worker, and queue a new refresh."
    )


class IdempotencyConflictError(RefreshServiceError):
    """Reject reusing an owner's key for a different extraction mode."""

    code = "idempotency_conflict"
    public_message = "This idempotency key was already used with another refresh mode."


class WorkerAlreadyRunning(RefreshBusyError):
    """Report ownership of the long-lived worker control lock by another worker."""

    code = "worker_already_running"
    public_message = "Another refresh worker holds the worker control lock."


def _active_admin(user_id: Any) -> bool:
    """Query current user activity and Admin persona rather than trusting stale state."""
    return (
        get_user_model()
        .objects.filter(pk=user_id, is_active=True, persona__role=Persona.Role.ADMIN)
        .exists()
    )


def enqueue_refresh(user, mode: str, idempotency_key: str | None = None):
    """Return (job, created) after durable enqueueing or owner/key replay.

    Translate SQLite busy/locked failures to RefreshBusyError; propagate unrelated database
    errors. The inner operation enforces current Admin, validated input and database
    uniqueness. No ingestion or worker launch happens in this call.
    """
    try:
        return _enqueue_refresh(user, mode, idempotency_key)
    except OperationalError as error:
        sqlite_code = getattr(error.__cause__, "sqlite_errorcode", 0)
        if sqlite_code & 0xFF in {5, 6}:
            raise RefreshBusyError from None
        raise


def _enqueue_refresh(user, mode: str, idempotency_key: str | None):
    """Validate an Admin request, replay a matching key or insert one queued job.

    The owner/key constraint prevents duplicate enqueueing, while the active-job constraint
    serializes queued/running requests globally. Handle insertion races by checking replay
    again; a same-key different-mode request conflicts. Avoid a read inside the insertion
    transaction so SQLite need not upgrade a read snapshot.
    """
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
        """Find this owner's existing key and reject a mode mismatch.

        Close over validated user, mode and idempotency_key so the same replay logic can run
        both before insertion and after a uniqueness race. No key means no replay.
        """
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
    """Download/build/publish data at the worker-only upstream credential boundary.

    Read EIA_API_KEY from this process environment, invoke the normal ingestion pipeline and
    resolve the model bound to its returned source snapshot. Another operator may publish
    after the ingestion lock releases, so fall back to retained successful model history
    rather than blindly returning the current model. Return snapshot_id/model_id/mode, or fail
    if that matching publication cannot be verified. No key file is loaded and this function
    does not finish the Django job row.
    """
    api_key = os.environ.get("EIA_API_KEY", "").strip()
    if not api_key:
        raise RefreshConfigurationError
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
    """Map worker exception categories to safe public code/message pairs.

    Discard raw upstream/storage/engine detail. Messages direct operators to publication
    status because failure to record a job outcome does not roll back an already committed
    data-catalog publication.
    """
    if isinstance(error, RefreshConfigurationError):
        return error.code, error.public_message
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
    """Retain one control-lock context and executor while processing queued jobs.

    The Python object is not an OS process or a background thread: the separately launched
    management command creates it and repeatedly calls run_once. The held OS lock identifies
    the sole active worker and releases on context/process exit; database queue constraints
    and the ingestion writer lock protect separate scopes.
    """

    def __init__(self, *, executor: Callable[[str], dict[str, Any]] | None = None):
        """Configure the injectable mode-to-result executor without starting any work.

        Default to perform_ingest; keep lock/recovery state on the instance so the management
        command can reuse it across multiple jobs within one context.
        """
        self.executor = executor or perform_ingest
        self._lock = None
        self.recovered_count = 0

    def __enter__(self):
        """Acquire sole-worker ownership before recovering abandoned running jobs.

        Reject double entry and map an owned control lock to WorkerAlreadyRunning. Only after
        acquisition mark previous running jobs interrupted; their data may already be
        published. Release the lock if recovery fails, then return this worker.
        """
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
        """Release the control-lock context on normal exit or propagated exceptions."""
        if self._lock is not None:
            self._lock.__exit__(exc_type, exc, traceback)
            self._lock = None

    def run_once(self) -> RefreshJob | None:
        """Claim and process at most one oldest queued job while the control lock is held.

        Return None if no job is available/claimed. Recheck the owner's current Admin status
        before execution, invoke the executor and validate the result-ID shape. Record success,
        safe failure or interruption; interruption is re-raised for the command to stop.
        Successful jobs trigger best-effort cache cleanup, while generation-bound keys prevent
        stale reuse if deletion fails. Revocation after extraction starts does not cancel
        ingestion. This method does not poll or loop.
        """
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
        """Persist a terminal outcome for the given running job and refresh its object.

        This static method needs only job/status arguments, not retained worker state. Update
        the Django row and log a safe event; it cannot atomically commit or undo the separate
        data-catalog publication.
        """
        RefreshJob.objects.filter(pk=job.pk, status="running").update(
            status=status,
            finished_at=timezone.now(),
            error_code=error_code,
            public_message=message,
            result=result or {},
        )
        job.refresh_from_db()
        LOG.info("refresh_job_" + status, extra={"run_id": str(job.pk), "error_code": error_code})
