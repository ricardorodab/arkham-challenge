"""Define best-effort, credential-free notifications for ingestion and model work.

Events describe entered phases and completed work, not provider totals or an
overall completion estimate. Route callbacks can run concurrently. Observers own
aggregation, validation and persistence; notifications are not publication evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from outage_explorer.data.domain import ROUTES as _SOURCE_ROUTES

STAGES = frozenset(
    {
        "preparing",
        "metadata",
        "extracting",
        "validating_source",
        "model_sources",
        "model_facts",
        "model_fleet",
        "model_reconciliation",
        "model_quality",
        "validating_model",
        "exporting_model",
        "verifying_model",
        "publishing",
        "completed",
    }
)
ROUTES = frozenset(spec.name for spec in _SOURCE_ROUTES)


@dataclass(frozen=True)
class ProgressEvent:
    """Describe a phase, a validated page delta or one model export checkpoint.

    ``rows_received`` and ``pages_fetched`` count completed validated/inserted
    pages, including deletion-confirmation rereads. They are deltas rather than
    cumulative counts. ``completed``/``total`` are used only for the fixed model
    dataset export loop; they do not estimate ingestion or model-build completion.
    ``route`` is a local source-grain name, never an arbitrary upstream URL.
    """

    stage: str
    route: str | None = None
    rows_received: int = 0
    pages_fetched: int = 0
    completed: int | None = None
    total: int | None = None


ProgressCallback = Callable[[ProgressEvent], None]


def emit_progress(callback: ProgressCallback | None, event: ProgressEvent) -> None:
    """Notify an optional observer without letting ordinary observer failures abort work.

    Do not log observer exceptions or event payloads. Process-control exceptions
    such as KeyboardInterrupt and SystemExit retain their normal propagation.
    """
    if callback is not None:
        try:
            callback(event)
        except Exception:
            pass
