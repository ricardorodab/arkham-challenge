"""Process-shared query admission using OS locks that release on process death."""

from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar

from django.conf import settings

from outage_explorer.storage import StorageError, writer_lock

from .errors import APIError

_ACTIVE: ContextVar[tuple[int, float] | None] = ContextVar("outage_query_slot", default=None)


def remaining_query_seconds(maximum: float = 8) -> float:
    active = _ACTIVE.get()
    remaining = maximum if active is None else min(maximum, active[1] - time.monotonic())
    if remaining <= 0:
        raise APIError(504, "query_timeout", "The query exceeded its execution deadline.")
    return remaining


def _take(stack: ExitStack, path) -> bool:
    try:
        stack.enter_context(writer_lock(path))
        return True
    except StorageError as error:
        if error.code != "already_running":
            raise
        return False


@contextmanager
def query_slot(user_id: int):
    active = _ACTIVE.get()
    if active is not None:
        if active[0] != user_id:
            raise APIError(
                429, "query_capacity", "Query capacity is busy; retry shortly.", retry_after=1
            )
        remaining_query_seconds()
        yield
        return
    base = settings.OUTAGE_STATE_DIR / "query-admission"
    with ExitStack() as user, ExitStack() as queue, ExitStack() as slot:
        if not _take(user, base / f"user-{user_id}"):
            raise APIError(
                429,
                "user_query_busy",
                "One query is already running for this account.",
                retry_after=1,
            )
        if not any(_take(queue, base / f"queue-{index}") for index in range(8)):
            raise APIError(429, "query_queue_full", "The query queue is full.", retry_after=1)
        deadline = time.monotonic() + 5
        while not any(_take(slot, base / f"slot-{index}") for index in range(2)):
            if time.monotonic() >= deadline:
                raise APIError(
                    429, "query_capacity", "Query capacity is busy; retry shortly.", retry_after=1
                )
            time.sleep(0.05)
        queue.close()
        token = _ACTIVE.set((user_id, time.monotonic() + settings.OUTAGE_QUERY_TIMEOUT_SECONDS))
        try:
            yield
        finally:
            _ACTIVE.reset(token)
