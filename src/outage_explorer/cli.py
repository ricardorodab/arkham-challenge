"""Small operational CLI; stdout is JSON, stderr is structured logging."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .domain import DataValidationError, parse_date
from .eia import EIAClient, EIAClientError, sanitize
from .pipeline import IngestConfig, ingest
from .storage import Catalog, StorageError


class JsonLogFormatter(logging.Formatter):
    def __init__(self, secret: str = "") -> None:
        super().__init__()
        self._secrets = (secret,)

    def format(self, record: logging.LogRecord) -> str:
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
    try:
        return parse_date(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def parser() -> argparse.ArgumentParser:
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
    from .model_store import analytical_catalog, rebuild_model, resolve_model, verify_model

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
        from .pipeline import write_json
        from .reports import findings_report

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
