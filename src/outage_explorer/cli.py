"""Route data/model/report operations and emit machine-readable operator output.

Both python -m outage_explorer and the installed outage-explorer console script call main.
Ingestion reaches EIA; status/history/verification and trusted native DuckDB model/report
operations use local saved data. This CLI does not serve HTTP or select an authenticated
product persona. stdout is JSON; stderr is sanitized structured logging and errors with
deliberate exit codes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from outage_explorer.data.domain import DataValidationError, parse_date
from outage_explorer.data.eia import EIAClient, EIAClientError, sanitize
from outage_explorer.data.pipeline import IngestConfig, ingest
from outage_explorer.data.storage import Catalog, StorageError


class JsonLogFormatter(logging.Formatter):
    """Emit allowlisted operational fields while recursively redacting a supplied secret."""

    def __init__(self, secret: str = "") -> None:
        """Retain the process credential only for sanitization, never as output context."""
        super().__init__()
        self._secrets = (secret,)

    def format(self, record: logging.LogRecord) -> str:
        """Return a UTC JSON event with safe selected LogRecord fields.

        Sanitize the event message and nested values against known secrets and sensitive
        parameter names; do not serialize arbitrary record attributes or traceback data.
        """
        fields = (
            "route",
            "offset",
            "rows",
            "declared_total",
            "attempt",
            "run_id",
            "mode",
            "start",
            "end",
            "added",
            "changed",
            "deleted",
            "error_code",
            "delay_seconds",
            "duration_seconds",
            "runs",
        )
        value: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        value.update({name: getattr(record, name) for name in fields if hasattr(record, name)})
        return json.dumps(sanitize(value, self._secrets), ensure_ascii=True, allow_nan=False)


def _calendar_date(value: str):
    """Adapt strict source-contract date parsing to an argparse validation error."""
    try:
        return parse_date(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def parser() -> argparse.ArgumentParser:
    """Declare supported source and model commands without executing their operations.

    Recent ingestion is the default; explicit start requires full mode in pipeline validation.
    Model pins, report dates and metadata persona selection are operator inputs, not web
    authentication or an arbitrary-SQL interface.
    """
    result = argparse.ArgumentParser(description="Validated local EIA nuclear outage connector")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("ingest", "status", "history", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--data-dir", type=Path, default=Path("data"))
        if name == "ingest":
            command.add_argument("--mode", choices=("full", "recent"), default="recent")
            command.add_argument("--start", type=_calendar_date)
            command.add_argument("--end", type=_calendar_date)
            command.add_argument("--page-size", type=int, default=5000)
            command.add_argument("--workers", type=int, choices=(1, 2, 3), default=1)
            command.add_argument("--record-responses", action="store_true")
            command.add_argument(
                "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"
            )
        if name == "history":
            command.add_argument("--limit", type=int, default=10)
        if name == "verify":
            command.add_argument("--snapshot-id")
    model = commands.add_parser("model", help="Build and inspect the versioned analytical model")
    actions = model.add_subparsers(dest="model_action", required=True)
    for name in ("build", "status", "history", "catalog", "verify", "report"):
        action = actions.add_parser(name)
        action.add_argument("--data-dir", type=Path, default=Path("data"))
        if name in {"status", "catalog", "verify", "report"}:
            action.add_argument("--model-id")
        if name == "catalog":
            action.add_argument(
                "--persona", choices=("viewer", "analyst", "admin"), default="analyst"
            )
        if name == "history":
            action.add_argument("--limit", type=int, default=10)
        if name == "report":
            action.add_argument(
                "--start", type=_calendar_date, default=_calendar_date("2026-09-01")
            )
            action.add_argument("--end", type=_calendar_date, default=_calendar_date("2026-09-30"))
            action.add_argument("--output", type=Path)
        if name == "build":
            action.add_argument("--log-level", choices=("INFO", "WARNING", "ERROR"), default="INFO")
    return result


def _model_command(args: argparse.Namespace) -> dict[str, Any]:
    """Dispatch one parsed model action and return its JSON-serializable result.

    Build mutates model publication; other actions inspect/verify or run fixed trusted reports
    over a selected model. Report output must be outside the immutable data directory. Local
    inspection needs no upstream API key; no user SQL is accepted.
    """
    from outage_explorer.analytics.model_store import (
        analytical_catalog,
        rebuild_model,
        resolve_model,
        verify_model,
    )

    root = args.data_dir.resolve()
    if args.model_action == "build":
        model = rebuild_model(root)
        return {
            "model_id": model.id,
            "source_snapshot_id": model.manifest["source_snapshot_id"],
            "model_version": model.manifest["model_version"],
            "datasets": {key: value["rows"] for key, value in model.manifest["datasets"].items()},
        }
    if args.model_action == "catalog":
        return analytical_catalog(root, args.persona, model_id=args.model_id)
    if args.model_action == "history":
        with Catalog(root) as catalog:
            return {"builds": catalog.model_history(args.limit)}
    if args.model_action == "report":
        from outage_explorer.analytics.reports import findings_report
        from outage_explorer.data.pipeline import write_json

        report = findings_report(root, args.start, args.end, model_id=args.model_id)
        if args.output:
            target = args.output.resolve()
            if target.is_relative_to(root):
                raise ValueError("Write reports outside the immutable data directory.")
            write_json(target, report)
            return {
                "model_id": report["model_id"],
                "source_snapshot_id": report["source_snapshot_id"],
                "output": str(target),
                "queries": {key: value["row_count"] for key, value in report["queries"].items()},
            }
        return report
    if args.model_action == "status":
        with Catalog(root) as catalog:
            model = catalog.model(args.model_id) if args.model_id else catalog.current_model()
        return {
            "current_model": model.id if model else None,
            "manifest": model.manifest if model else None,
        }
    model = resolve_model(root, args.model_id)
    with Catalog(root) as catalog:
        catalog.verify(model.manifest["source_snapshot_id"])
    return verify_model(model)


def main(argv: list[str] | None = None) -> int:
    """Parse argv, configure safe logs and execute one operation with stable exit codes.

    Use EIA_API_KEY only from the process environment. Return 0 on success, 2 for
    configuration, 3 for upstream failure, 4 for validation/storage/internal failure, 5 for
    writer contention and 130 for interruption. Catch unexpected failures without printing raw
    transport/filesystem details; output artifacts remain the responsibility of the delegated
    pipeline/catalog/report operations.
    """
    args = parser().parse_args(argv)
    key = os.environ.get("EIA_API_KEY", "").strip()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLogFormatter(key))
    logger = logging.getLogger("outage_explorer")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(getattr(logging, getattr(args, "log_level", "WARNING")))
    try:
        if args.command == "model":
            result = _model_command(args)
        elif args.command == "ingest":
            config = IngestConfig(
                args.data_dir,
                args.mode,
                args.start,
                args.end,
                args.page_size,
                args.workers,
                args.record_responses,
            )
            result = ingest(config, EIAClient(key))
        else:
            with Catalog(args.data_dir.resolve()) as catalog:
                if args.command == "verify":
                    result = catalog.verify(args.snapshot_id)
                elif args.command == "history":
                    if not 1 <= args.limit <= 1000:
                        raise ValueError("History limit must be 1..1000.")
                    result = {"runs": catalog.history(args.limit)}
                else:
                    current = catalog.current()
                    result = {
                        "current_snapshot": current.id if current else None,
                        "routes": current.manifest["routes"] if current else {},
                    }
        print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        error, code = {"code": "interrupted", "message": "Interrupted by the operator."}, 130
    except EIAClientError as failure:
        error = {
            "code": failure.code,
            "message": str(failure),
            "retryable": failure.retryable,
            "retry_at": failure.retry_at.isoformat() if failure.retry_at else None,
        }
        code = 2 if failure.code == "configuration" else 3
    except DataValidationError as failure:
        error, code = {"code": failure.code, "message": str(failure)}, 4
    except StorageError as failure:
        error = {"code": failure.code, "message": str(failure)}
        code = 5 if failure.code == "already_running" else 4
    except ValueError as failure:
        error, code = {"code": "configuration", "message": str(failure)}, 2
    except Exception:
        # Exceptions from SQLite/filesystems must not print nested transport
        # URLs, connection details or untrusted row values as tracebacks.
        error, code = {"code": "internal_error", "message": "Connector operation failed safely."}, 4
    print(json.dumps({"error": sanitize(error, (key,))}, ensure_ascii=True), file=sys.stderr)
    return code
