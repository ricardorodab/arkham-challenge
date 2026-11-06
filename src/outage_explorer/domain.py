"""Validate and type the three EIA source grains before persistence.

The source contract preserves independent national, facility and generator
observations, exact decimal measures and canonical source-row evidence. Structural
errors fail validation; representable semantic outliers remain with flags. This
module neither fetches data nor sanitizes credentials: callers must sanitize rows
before passing them to ``validate_row``.
"""

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
    """Signal an invalid source contract with a safe, field-oriented message.

    Callers must avoid including untrusted values or credentials in messages. The
    ``invalid_data`` code is consumed by ingestion diagnostics and CLI error mapping.
    """

    code = "invalid_data"


@dataclass(frozen=True)
class RouteSpec:
    """Describe one fixed source route, its grain and ordered natural key.

    ``name`` selects local files, ``route`` selects the allowlisted EIA endpoint, and
    ``key_fields`` establishes identity and deterministic ordering. Instances are
    immutable; the column contract is derived from the key rather than source data.
    """

    name: str
    route: str
    key_fields: tuple[str, ...]

    @property
    def columns(self) -> tuple[tuple[str, str], ...]:
        """Return the ordered raw DuckDB schema for this route.

        Entity columns appear only at their applicable grain. Each measure keeps its
        unit, and evidence columns retain canonical source JSON, its hash and flags.
        The order also defines ``ValidatedRow.values`` and persisted Parquet columns.
        """
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
    """Escape an application-owned identifier for DuckDB SQL.

    Double embedded quotes so the name remains one quoted identifier. This helper
    does not authorize names or validate SQL fragments; callers own those policies.
    """
    return '"' + name.replace('"', '""') + '"'


def canonical_json(value: Any) -> str:
    """Serialize JSON-native values deterministically for evidence and hashing.

    Keys are sorted, separators are compact and Unicode is retained. Unsupported
    objects and nonfinite numbers raise ``TypeError`` or ``ValueError``; dates and
    Decimals must be normalized by the caller.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def parse_date(value: str) -> date:
    """Parse an exact YYYY-MM-DD string as a real calendar date.

    Raise ``ValueError`` for a non-string, a different lexical shape or an invalid
    calendar day. No timestamp or timezone conversion is performed.
    """
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Expected a calendar date in YYYY-MM-DD format.")
    return date.fromisoformat(value)


def _measure(value: Any, field: str) -> Decimal | None:
    """Convert a source measure to exact DECIMAL(24,9), or preserve null.

    Accept strings and integers; null and the empty string mean unknown. Booleans,
    binary floats, nonfinite numbers, excessive input length, storage overflow and
    fractional precision loss raise ``DataValidationError`` naming only ``field``.
    Outlier semantics such as a negative outage are handled separately as flags.
    """
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
    """Carry one validated observation and its reproducibility metadata.

    ``values`` follows ``RouteSpec.columns``; ``key`` follows the natural key.
    ``flags`` records retained semantic anomalies, and ``extra_fields`` lists source
    fields kept in canonical evidence but absent from the stable typed contract.
    """

    values: tuple[Any, ...]
    key: tuple[Any, ...]
    flags: tuple[str, ...]
    extra_fields: tuple[str, ...]


def validate_row(spec: RouteSpec, row: dict[str, Any], start: date, end: date) -> ValidatedRow:
    """Validate an already-sanitized source row within inclusive date bounds.

    Require the route's identity, measures and exact units. Text identifiers must
    already be nonempty and trimmed; they are not numerically coerced. Preserve
    nullable measures, observation-time names and additive fields while flagging
    semantic outliers. Hash the complete canonical source row, so descriptive or
    additive-field changes also change the fingerprint.

    Return a ``ValidatedRow`` in the route's column order. Raise
    ``DataValidationError`` for invalid identity, date, required fields, units,
    measure representation or JSON evidence. No caller mapping is mutated and no
    files are written; credential sanitization is the caller's responsibility.
    """
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
