"""Durable application state, separate from immutable analytical snapshots."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Persona(models.Model):
    class Role(models.TextChoices):
        VIEWER = "viewer", "Viewer"
        ANALYST = "analyst", "Analyst"
        ADMIN = "admin", "Admin"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="persona"
    )
    role = models.CharField(max_length=7, choices=Role.choices)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(role__in=["viewer", "analyst", "admin"]), name="svc_persona_valid_role"
            )
        ]


class RefreshJob(models.Model):
    class Mode(models.TextChoices):
        FULL = "full", "Full"
        RECENT = "recent", "Recent"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        INTERRUPTED = "interrupted", "Interrupted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="refresh_jobs"
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

    class Meta:
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
    key = models.CharField(max_length=64, primary_key=True)
    window_started_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)


class QueryCacheEntry(models.Model):
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
        indexes = [
            models.Index(fields=["owner", "expires_at"], name="svc_cache_owner_exp_idx"),
            models.Index(fields=["expires_at"], name="svc_cache_expiry_idx"),
        ]
