"""Persist bounded, public refresh milestones without controlling publication."""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from django.db import connections
from django.utils import timezone

from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.progress import ROUTES, STAGES, ProgressEvent

from .models import RefreshJob

LOG = logging.getLogger(__name__)
MAX_COUNT = (1 << 53) - 1


def _count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNT


def public_progress(value: Any) -> dict[str, Any]:
    """Allow only known milestones, counts and a timestamp across the API boundary.

    Older jobs have an empty object. Unknown or malformed snapshots use the same
    fallback; arbitrary provider detail, paths and diagnostic text never pass through.
    Counts describe received pages/rows, including confirmation reads, not unique rows.
    """
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("stage"), str)
        or value["stage"] not in STAGES
    ):
        return {}
    stamp = value.get("updated_at")
    if not isinstance(stamp, str) or len(stamp) > 40:
        return {}
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            return {}
        normalized_stamp = parsed.astimezone(UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        return {}
    result = {"stage": value["stage"], "updated_at": normalized_stamp}
    for name in ("rows_received", "pages_fetched"):
        result[name] = value[name] if _count(value.get(name)) else 0
    if isinstance(value.get("route"), str) and value["route"] in ROUTES:
        result["route"] = value["route"]
    routes = value.get("routes")
    if isinstance(routes, dict):
        result["routes"] = {
            route: {
                name: counts[name] if _count(counts.get(name)) else 0
                for name in ("rows_received", "pages_fetched")
            }
            for route, counts in routes.items()
            if route in ROUTES and isinstance(counts, dict)
        }
    completed, total = value.get("completed"), value.get("total")
    if (
        value["stage"] == "exporting_model"
        and _count(completed)
        and type(total) is int
        and total == len(DATASETS)
        and completed <= total
    ):
        result.update(completed=completed, total=total)
    return result


class JobProgressReporter:
    """Aggregate thread-safe page deltas and throttle count-only SQLite updates.

    Phase changes and the final flush are immediate; ordinary page/export updates
    attempt at most one write per second. The pipeline may call from route threads.
    Each such write closes its own Django connection, leaving the worker connection
    alone. Reporting failures cannot fail or roll back analytical publication.
    """

    def __init__(self, job_id, *, interval: float = 1.0, clock=time.monotonic):
        self.job_id = job_id
        self.interval = interval
        self.clock = clock
        self._owner_thread = threading.get_ident()
        self._lock = threading.Lock()
        self._value: dict[str, Any] = {}
        self._last_attempt = float("-inf")
        self._warned = False

    def __call__(self, event: ProgressEvent) -> None:
        if (
            not isinstance(event, ProgressEvent)
            or not isinstance(event.stage, str)
            or event.stage not in STAGES
        ):
            return
        if not _count(event.rows_received) or not _count(event.pages_fetched):
            return
        if event.route is not None and (
            not isinstance(event.route, str) or event.route not in ROUTES
        ):
            return
        with self._lock:
            previous_stage = self._value.get("stage")
            value = dict(self._value)
            value.update(stage=event.stage)
            if previous_stage != event.stage:
                for name in ("route", "completed", "total"):
                    value.pop(name, None)
            for name in ("rows_received", "pages_fetched"):
                value[name] = min(MAX_COUNT, value.get(name, 0) + getattr(event, name))
            if event.route is not None:
                value["route"] = event.route
                routes = {name: dict(counts) for name, counts in value.get("routes", {}).items()}
                counts = routes.setdefault(event.route, {"rows_received": 0, "pages_fetched": 0})
                for name in ("rows_received", "pages_fetched"):
                    counts[name] = min(MAX_COUNT, counts[name] + getattr(event, name))
                value["routes"] = routes
            if (
                event.stage == "exporting_model"
                and _count(event.completed)
                and type(event.total) is int
                and event.total == len(DATASETS)
                and event.completed <= event.total
            ):
                value.update(completed=event.completed, total=event.total)
            if value == self._value:
                return
            value["updated_at"] = timezone.now().isoformat()
            self._value = value
            self._write(force=event.stage != previous_stage)

    def flush(self) -> None:
        """Persist the last observed work before recording any terminal outcome."""
        try:
            with self._lock:
                self._write(force=True)
        except Exception:
            # Telemetry must not mask a failure or interruption being recorded.
            pass

    def _write(self, *, force: bool) -> None:
        now = self.clock()
        if not self._value or (not force and now - self._last_attempt < self.interval):
            return
        self._last_attempt = now
        try:
            RefreshJob.objects.filter(pk=self.job_id, status="running").update(
                progress=public_progress(self._value)
            )
        except Exception:
            if not self._warned:
                LOG.warning("refresh_progress_write_failed")
                self._warned = True
        finally:
            if threading.get_ident() != self._owner_thread:
                try:
                    connections["default"].close()
                except Exception:
                    pass
