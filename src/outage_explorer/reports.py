"""Reproduce findings using fixed, packaged, application-owned SQL assets."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any

import duckdb

from .model_contracts import DATASETS
from .model_store import open_model_connection
from .storage import StorageError


def report_queries() -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for name in ("reconciliation.sql", "part2_findings.sql"):
        text = files("outage_explorer").joinpath(f"sql/{name}").read_text(encoding="utf-8")
        sections = re.split(r"(?m)^-- name:\s*([a-z][a-z0-9_]*)\s*$", text)
        if len(sections) < 3:
            raise StorageError("Report SQL has no named statements.", code="invalid_report")
        for index in range(1, len(sections), 2):
            label, sql = sections[index], sections[index + 1].strip()
            if label in result:
                raise StorageError("Report query names must be unique.", code="invalid_report")
            result[label] = (name, sql)
    return result


def json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def findings_report(
    root: Path, start: date, end: date, *, model_id: str | None = None
) -> dict[str, Any]:
    if start > end or (end - start).days + 1 < 30:
        raise ValueError("A reconciliation report requires at least 30 calendar days.")
    statements = report_queries()
    output: dict[str, Any] = {}
    with open_model_connection(root, list(DATASETS), model_id=model_id) as (con, bundle):
        parameters = {"start": start, "end": end}
        for label, (asset, sql) in statements.items():
            parsed = con.extract_statements(sql)
            if len(parsed) != 1 or parsed[0].type != duckdb.StatementType.SELECT:
                raise StorageError(
                    "Reports support only fixed SELECT statements.", code="invalid_report"
                )
            required = parsed[0].named_parameters
            if not required.issubset(parameters):
                raise StorageError("Report parameters are invalid.", code="invalid_report")
            con.execute(sql, {key: parameters[key] for key in required})
            columns = [description[0] for description in con.description]
            rows = []
            while batch := con.fetchmany(8192):
                rows.extend([json_value(value) for value in row] for row in batch)
            output[label] = {
                "asset": asset,
                "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
            }
        return {
            "model_id": bundle.id,
            "model_version": bundle.manifest["model_version"],
            "source_snapshot_id": bundle.manifest["source_snapshot_id"],
            "created_at": datetime.now(UTC).isoformat(),
            "reconciliation_start": str(start),
            "reconciliation_end": str(end),
            "requested_calendar_days": (end - start).days + 1,
            "scope_note": "Windowed reconciliation uses start/end; historical anomaly probes use their stated dates.",
            "queries": output,
        }
