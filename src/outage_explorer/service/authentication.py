"""Bound login attempts in shared operational state; never persist usernames/IPs."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from outage_explorer.storage import StorageError, writer_lock

from .errors import APIError
from .models import RateLimitBucket


def limit_login(request, username: str) -> None:
    now = timezone.now()
    ip = request.META.get("REMOTE_ADDR", "unknown")
    buckets = (("account:" + username, 10), ("address:" + ip, 30))
    try:
        with writer_lock(settings.OUTAGE_STATE_DIR / "login-admission"), transaction.atomic():
            for scope, limit in buckets:
                key = salted_hmac("login-rate", scope, algorithm="sha256").hexdigest()
                bucket, _ = RateLimitBucket.objects.get_or_create(
                    key=key, defaults={"window_started_at": now, "attempts": 0}
                )
                if bucket.window_started_at <= now - timedelta(minutes=5):
                    bucket.window_started_at, bucket.attempts = now, 0
                if bucket.attempts >= limit:
                    raise APIError(
                        429,
                        "login_rate_limited",
                        "Too many login attempts; retry later.",
                        retry_after=300,
                    )
                bucket.attempts += 1
                bucket.save(update_fields=["window_started_at", "attempts"])
            RateLimitBucket.objects.filter(window_started_at__lt=now - timedelta(days=1)).delete()
    except StorageError:
        raise APIError(429, "login_busy", "Login is busy; retry shortly.", retry_after=1) from None
