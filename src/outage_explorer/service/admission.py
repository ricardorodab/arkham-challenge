"""Coordinate bounded analytical admission across processes on the same host.

OS writer locks enforce one outstanding operation per account, two admitted operations, eight
waiting tickets and at most five seconds of queue wait. Locks are shared through the
configured state directory and release on process death; this is bounded polling, not a FIFO
or multi-host scheduler. ContextVar holds execution-local reentrancy/deadline state, not the
shared lock registry.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar

from django.conf import settings

from outage_explorer.data.storage import StorageError, writer_lock

from .errors import APIError

# Python supplies execution-local context isolation; the filesystem locks below
# remain the cross-process authority for capacity and account exclusion.
_ACTIVE: ContextVar[tuple[int, float] | None] = ContextVar("outage_query_slot", default=None)


def remaining_query_seconds(maximum: float = 8) -> float:
    """Return maximum capped by the current operation's monotonic deadline.

    Without an admitted context, return the caller's maximum. Raise query_timeout when the
    remaining budget is nonpositive. Compiler and engine callers consume the same admitted
    deadline rather than starting independent full budgets.
    """
    active = _ACTIVE.get()
    remaining = maximum if active is None else min(maximum, active[1] - time.monotonic())
    if remaining <= 0:
        raise APIError(504, "query_timeout", "The query exceeded its execution deadline.")
    return remaining


def _take(stack: ExitStack, path) -> bool:
    """Enter a nonblocking writer lock into stack and report whether it was acquired.

    Only already_running becomes False; propagate other storage failures. ExitStack retains
    successful acquisitions and releases them on every context exit.
    """
    try:
        stack.enter_context(writer_lock(path))
        return True
    except StorageError as error:
        if error.code != "already_running":
            raise
        return False


@contextmanager
def query_slot(user_id: int):
    """Admit one account operation and yield while its shared execution budget runs.

    Acquire the account lock, a bounded waiting ticket and one of two host slots. Release the
    waiting ticket once admitted, then establish the monotonic execution deadline.
    Same-account nested calls reuse the existing slot and deadline; a different account cannot
    borrow that context. Reset ContextVar with its token and release all locks on normal
    completion or exceptions.
    """
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
