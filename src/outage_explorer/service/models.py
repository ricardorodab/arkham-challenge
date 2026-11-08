"""Define Django operational tables, separate from the Parquet analytical model.

These rows live in the application database: product roles, refresh lifecycle, login-attempt
buckets and temporary query results. Migrations create their schema/constraints; no model here
downloads EIA rows or publishes analytical files.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Persona(models.Model):
    """Assign exactly one valid product role to a Django user.

    The one-to-one relation and check constraint define policy input; product Admin does not
    imply Django staff/superuser or infrastructure privileges.
    session_version retires prior sign-ins after credential or access changes without
    scanning the session table; version one preserves pre-existing local sessions.
    """

    class Role(models.TextChoices):
        """Enumerate persisted role values accepted by the shared authorization policy."""

        VIEWER = "viewer", "Viewer"
        ANALYST = "analyst", "Analyst"
        ADMIN = "admin", "Admin"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="persona"
    )
    role = models.CharField(max_length=7, choices=Role.choices)
    session_version = models.PositiveIntegerField(default=1)

    class Meta:
        """Enforce the allowed role vocabulary in the application database."""

        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=["viewer", "analyst", "admin"]), name="svc_persona_valid_role"
            )
        ]


class RefreshJob(models.Model):
    """Persist one owner-scoped refresh request and its observable lifecycle.

    An owner/key pair deduplicates enqueueing; replay with the same mode returns the same job,
    while a different mode conflicts in the service layer. A conditional unique constraint
    permits only one queued/running job globally. Neither rule guarantees exactly-once
    ingestion: data publication and job completion occur in different databases/transactions,
    so interruption may follow successful publication.
    Deleting an eligible account detaches its completed history instead of erasing jobs.
    """

    class Mode(models.TextChoices):
        """Choose complete-history or recent-overlap extraction for the worker."""

        FULL = "full", "Full"
        RECENT = "recent", "Recent"

    class Status(models.TextChoices):
        """Represent queued/running work and explicit terminal outcomes."""

        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        INTERRUPTED = "interrupted", "Interrupted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="refresh_jobs",
    )
    mode = models.CharField(max_length=6, choices=Mode.choices)
    status = models.CharField(max_length=11, choices=Status.choices, default=Status.QUEUED)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    public_message = models.CharField(max_length=240, default="Refresh is queued.")
    result = models.JSONField(default=dict, blank=True)
    progress = models.JSONField(default=dict, blank=True)

    class Meta:
        """Index job history and enforce active-job, owner/key and enum constraints."""

        ordering = ["-created_at", "id"]
        indexes = [
            models.Index(fields=["status", "created_at"], name="svc_refresh_status_time_idx"),
            models.Index(fields=["owner", "created_at"], name="svc_refresh_owner_time_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                models.Value(1),
                condition=Q(status__in=["queued", "running"]),
                name="svc_one_active_refresh",
            ),
            models.UniqueConstraint(
                fields=["owner", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="svc_refresh_owner_idempotency",
            ),
            models.CheckConstraint(
                condition=Q(mode__in=["full", "recent"]), name="svc_refresh_valid_mode"
            ),
            models.CheckConstraint(
                condition=Q(status__in=["queued", "running", "succeeded", "failed", "interrupted"]),
                name="svc_refresh_valid_status",
            ),
        ]


class RateLimitBucket(models.Model):
    """Store attempt count/window under an opaque HMAC scope, never raw identities."""

    key = models.CharField(max_length=64, primary_key=True)
    window_started_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)


class QueryCacheEntry(models.Model):
    """Retain private capped SQL results for short-lived page reuse.

    api_services sets a 60-second expiry, prunes to eight entries per user and binds the key
    to session/role/query/limits/model/current publication. These fields alone are not
    authorization: cache hits still require a fresh identity check. Payload contains result
    rows; this is temporary operational state, not a saved dataset.
    """

    key = models.CharField(max_length=64, primary_key=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="query_cache_entries"
    )
    role = models.CharField(max_length=16)
    model_id = models.CharField(max_length=32)
    publication_id = models.CharField(max_length=32)
    expires_at = models.DateTimeField()
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Support per-owner eviction and opportunistic expiry cleanup."""

        indexes = [
            models.Index(fields=["owner", "expires_at"], name="svc_cache_owner_exp_idx"),
            models.Index(fields=["expires_at"], name="svc_cache_expiry_idx"),
        ]


class SavedView(models.Model):
    """Persist a named SQL definition and presentation settings without result rows.

    The current reader's dataset policy is always authoritative. Audience flags grant
    access to this definition, never access to otherwise forbidden analytical data.
    created_model_id records provenance; executions use the reader's adopted model.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_views"
    )
    name = models.CharField(max_length=100)
    dataset_id = models.CharField(max_length=64)
    sql = models.TextField()
    max_rows = models.PositiveSmallIntegerField(default=1000)
    chart = models.JSONField(default=dict)
    share_viewer = models.BooleanField(default=False)
    share_analyst = models.BooleanField(default=False)
    share_admin = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    created_model_id = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Support owner listing and bounded row/revision invariants in SQLite."""

        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["owner", "created_at"], name="svc_saved_owner_created_idx"),
            models.Index(fields=["dataset_id", "created_at"], name="svc_saved_dataset_time_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(max_rows__gte=1, max_rows__lte=1000), name="svc_saved_valid_row_cap"
            ),
            models.CheckConstraint(condition=Q(revision__gte=1), name="svc_saved_valid_revision"),
        ]
