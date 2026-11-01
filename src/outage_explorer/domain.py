"""The intentionally small, explicit EIA source contract (not an analytical model)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

SCHEMA_VERSION = 1
METRICS = ("capacity", "outage", "percentOutage")
UNITS = {"capacity": "megawatts", "outage": "megawatts", "percentOutage": "percent"}
QUANTUM = Decimal("0.000000001")


class DataValidationError(Exception):
    """Contract errors contain field names, never untrusted values or credentials."""

    code = "invalid_data"


@dataclass(frozen=True)
class RouteSpec:
    name: str
    route: str
    key_fields: tuple[str, ...]

    @property
    def columns(self) -> tuple[tuple[str, str], ...]:
        columns = [("period", "DATE")]
        if "facility" in self.key_fields:
            columns.extend((("facility", "VARCHAR"), ("facilityName", "VARCHAR")))
        if "generator" in self.key_fields:
            columns.append(("generator", "VARCHAR"))
        for metric in METRICS:
            columns.extend(((metric, "DECIMAL(24,9)"), (f"{metric}-units", "VARCHAR")))
        columns.extend(
            (("_source_row", "VARCHAR"), ("_row_hash", "VARCHAR"), ("_quality_flags", "VARCHAR"))
        )
        return tuple(columns)


ROUTES = (
    RouteSpec("national", "us-nuclear-outages", ("period",)),
    RouteSpec("facility", "facility-nuclear-outages", ("period", "facility")),
    RouteSpec("generator", "generator-nuclear-outages", ("period", "facility", "generator")),
)
ROUTE_BY_NAME = {spec.name: spec for spec in ROUTES}


def identifier(name: str) -> str:
    """Quote only server-owned column identifiers; never accepts a user SQL fragment."""
    return '"' + name.replace('"', '""') + '"'


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def parse_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected a calendar date in YYYY-MM-DD format.")
    return date.fromisoformat(value)


def _measure(value: Any, field: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise DataValidationError(f"{field}: expected a decimal string, integer or null.")
    if len(str(value)) > 128:
        raise DataValidationError(f"{field}: numeric value is too long.")
    try:
        with localcontext() as context:
            context.prec = 50
            number = Decimal(value)
            if not number.is_finite() or abs(number) >= Decimal(10) ** 15:
                raise InvalidOperation
            typed = number.quantize(QUANTUM)
            if typed != number:
                raise DataValidationError(f"{field}: conversion would lose decimal precision.")
            return typed
    except (InvalidOperation, ValueError):
        raise DataValidationError(f"{field}: not a finite DECIMAL(24,9) value.") from None


@dataclass(frozen=True)
class ValidatedRow:
    values: tuple[Any, ...]
    key: tuple[Any, ...]
    flags: tuple[str, ...]
    extra_fields: tuple[str, ...]


def validate_row(spec: RouteSpec, row: dict[str, Any], start: date, end: date) -> ValidatedRow:
    required = set(spec.key_fields) | set(METRICS) | {f"{m}-units" for m in METRICS}
    missing = required - row.keys()
    if missing:
        raise DataValidationError(
            f"{spec.name}: missing required fields: {', '.join(sorted(missing))}."
        )
    try:
        period = parse_date(row["period"])
    except (TypeError, ValueError):
        raise DataValidationError(f"{spec.name}: invalid period.") from None
    if not start <= period <= end:
        raise DataValidationError(f"{spec.name}: period outside requested interval.")
    normalized: dict[str, Any] = {"period": period}
    flags: list[str] = []
    for field in spec.key_fields[1:]:
        value = row[field]
        if not isinstance(value, str) or not value or value != value.strip() or len(value) > 128:
            raise DataValidationError(f"{spec.name}: invalid text identifier {field}.")
        normalized[field] = value
    if "facility" in spec.key_fields:
        name = row.get("facilityName")
        if name is not None and not isinstance(name, str):
            raise DataValidationError(f"{spec.name}: facilityName must be text or null.")
        normalized["facilityName"] = name
        if not name:
            flags.append("missing_facility_name")
    for metric in METRICS:
        if row[f"{metric}-units"] != UNITS[metric]:
            raise DataValidationError(f"{spec.name}: unexpected units for {metric}.")
        normalized[metric] = _measure(row[metric], metric)
        normalized[f"{metric}-units"] = UNITS[metric]
        if normalized[metric] is None:
            flags.append(f"null_{metric}")
    capacity, outage, percent = (normalized[m] for m in METRICS)
    if capacity is not None and capacity <= 0:
        flags.append("capacity_non_positive")
    if outage is not None and outage < 0:
        flags.append("outage_negative")
    if capacity is not None and outage is not None and outage > capacity:
        flags.append("outage_above_capacity")
    if percent is not None and not Decimal(0) <= percent <= Decimal(100):
        flags.append("percent_out_of_range")
    try:
        source = canonical_json(row)
    except (TypeError, ValueError):
        raise DataValidationError(f"{spec.name}: source row is not valid JSON.") from None
    normalized["_source_row"] = source
    normalized["_row_hash"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
    normalized["_quality_flags"] = canonical_json(flags)
    known = {column for column, _ in spec.columns if not column.startswith("_")}
    extras = tuple(sorted(set(row) - known))
    return ValidatedRow(
        tuple(normalized[name] for name, _ in spec.columns),
        tuple(normalized[name] for name in spec.key_fields),
        tuple(flags),
        extras,
    )
