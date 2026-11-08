"""Normalize structured browsing and compile bound, deterministic seek queries.

This module performs no I/O and accepts no SQL expressions. Dataset/column identifiers and
cast types come exclusively from DATASETS. Authorization, immutable-model selection, cursor
signing and process/resource isolation belong to callers; children must compile plans again.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from outage_explorer.analytics.model_contracts import DATASETS, DatasetSpec

MAX_SEARCH = 256
MAX_FILTERS = 8
MAX_CANONICAL_FILTERS = 12
MAX_SEEK_TEXT_BYTES = 16384
MAX_SEEK_BYTES = 32768
_NULL_OPS = {"is_null", "not_null"}
_COMPARISONS = {"eq": "=", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
_TEXT_OPS = {"eq", "ne", "contains", "not_contains", "starts_with"}
_ORDERED_OPS = {*_COMPARISONS, "between"}
_REQUEST_FIELDS = {
    "search",
    "sort",
    "direction",
    "filters",
    "start",
    "end",
    "facility_id",
    "generator_id",
    "limit",
    "cursor",
    "model_id",
}


class BrowseError(ValueError):
    """Represent a static public validation failure without echoing request values."""

    def __init__(self, code: str = "invalid_browse"):
        """Attach a safe category and static message for transport-layer mapping."""
        self.code = code
        self.message = (
            "The browse cursor is invalid."
            if code == "invalid_cursor"
            else "The structured browse request is invalid."
        )
        super().__init__(self.message)


def _require(condition: bool, code: str = "invalid_browse") -> None:
    """Reject a failed invariant without including the offending value."""
    if not condition:
        raise BrowseError(code)


def _spec(dataset_id: Any) -> DatasetSpec:
    """Resolve only a declared logical dataset; do not resolve files or permissions."""
    _require(isinstance(dataset_id, str) and dataset_id in DATASETS)
    return DATASETS[dataset_id]


def _text(value: Any, maximum: int = 256) -> str:
    """Preserve bounded literal UTF-8 text, excluding NUL and invalid surrogates."""
    _require(isinstance(value, str) and len(value) <= maximum and "\x00" not in value)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise BrowseError from None
    return value


def _scalar(value: Any, kind: str) -> str:
    """Return a JSON string that fits its exact declared type without precision loss."""
    if kind == "VARCHAR":
        return _text(value)
    if kind == "DATE":
        _require(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value))
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise BrowseError from None
    if kind == "BOOLEAN":
        if type(value) is bool:
            return "true" if value else "false"
        _require(isinstance(value, str) and value in {"true", "false"})
        return value
    if kind in {"INTEGER", "BIGINT"}:
        _require(type(value) is int or isinstance(value, str))
        encoded = str(value)
        _require(len(encoded) <= 32 and re.fullmatch(r"[+-]?[0-9]+", encoded))
        number = int(encoded)
        bits = 32 if kind == "INTEGER" else 64
        _require(-(1 << (bits - 1)) <= number < (1 << (bits - 1)))
        return str(number)
    decimal = re.fullmatch(r"DECIMAL\(([0-9]+),([0-9]+)\)", kind)
    _require(decimal is not None)
    _require(isinstance(value, str) and len(value) <= 128)
    _require(re.fullmatch(r"[+-]?[0-9]+(?:\.[0-9]+)?", value) is not None)
    precision, scale = map(int, decimal.groups())
    try:
        with localcontext() as context:
            context.prec = max(50, precision + scale + 2)
            number = Decimal(value)
            _require(number.is_finite() and abs(number) < Decimal(10) ** (precision - scale))
            quantized = number.quantize(Decimal(1).scaleb(-scale))
            _require(quantized == number)
            return format(abs(quantized) if quantized.is_zero() else quantized, f".{scale}f")
    except (InvalidOperation, ValueError):
        raise BrowseError from None


def _filter(value: Any, columns: dict[str, str]) -> dict[str, Any]:
    """Validate a closed operator object and canonicalize each required operand."""
    _require(isinstance(value, dict) and {"column", "op"} <= set(value))
    column, operation = value["column"], value["op"]
    _require(isinstance(column, str) and column in columns and isinstance(operation, str))
    kind = columns[column]
    allowed = (
        _TEXT_OPS if kind == "VARCHAR" else {"eq", "ne"} if kind == "BOOLEAN" else _ORDERED_OPS
    )
    _require(operation in allowed | _NULL_OPS)
    fields = {"column", "op"}
    if operation not in _NULL_OPS:
        fields.add("value")
    if operation == "between":
        fields.add("upper")
    _require(set(value) == fields)
    result = {"column": column, "op": operation}
    if "value" in fields:
        result["value"] = _scalar(value["value"], kind)
    if "upper" in fields:
        result["upper"] = _scalar(value["upper"], kind)
        lower = result["value"] if kind == "DATE" else Decimal(result["value"])
        upper = result["upper"] if kind == "DATE" else Decimal(result["upper"])
        _require(lower <= upper)
    return result


def _canonical_filters(filters: list, columns: dict[str, str]) -> list[dict]:
    """Deduplicate equivalent predicates and order them independently of request order."""
    normalized = [_filter(item, columns) for item in filters]
    encoded = {json.dumps(item, sort_keys=True, ensure_ascii=False): item for item in normalized}
    return sorted(
        encoded.values(),
        key=lambda item: (
            item["column"],
            item["op"],
            json.dumps(item.get("value"), ensure_ascii=False),
            json.dumps(item.get("upper"), ensure_ascii=False),
        ),
    )


def _strict_object(pairs):
    """Reject duplicate JSON object fields before they can overwrite validated meaning."""
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _reject_constant(_value):
    """Reject Python JSON extensions for NaN and infinities."""
    raise BrowseError


def _order(spec: DatasetSpec, column: str, direction: str) -> list[dict[str, str]]:
    """Append every remaining primary key in ascending order as deterministic ties."""
    _require(column in dict(spec.columns) and direction in {"asc", "desc"})
    return [{"column": column, "direction": direction}] + [
        {"column": key, "direction": "asc"} for key in spec.primary_key if key != column
    ]


def normalize_query(dataset_id: str, parameters: dict) -> dict[str, Any]:
    """Canonicalize request fields, AND filters and sort order without model access.

    Pagination/model parameters are accepted but excluded from the plan. Text operands stay
    literal and case-sensitive; global search uses database lower() over each exposed value.
    Up to eight user filters coexist with four legacy exact/date constraints. Identical
    predicates are deduplicated; distinct predicates, including contradictions, remain ANDed.
    """
    spec = _spec(dataset_id)
    columns = dict(spec.columns)
    _require(isinstance(parameters, dict) and set(parameters) <= _REQUEST_FIELDS)
    search = _text(parameters.get("search", ""), MAX_SEARCH)
    sort = parameters.get("sort", spec.primary_key[0])
    direction = parameters.get("direction", "asc")
    _require(isinstance(sort, str) and isinstance(direction, str))
    order = _order(spec, sort, direction)
    filters = parameters.get("filters", [])
    if isinstance(filters, str):
        _require(len(filters) <= 16384)
        try:
            filters = json.loads(
                filters,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (ValueError, RecursionError):
            raise BrowseError from None
    _require(isinstance(filters, list) and len(filters) <= MAX_FILTERS)
    filters = list(filters)
    for name, column, operation in (
        ("start", "period", "gte"),
        ("end", "period", "lte"),
        ("facility_id", "facility_id", "eq"),
        ("generator_id", "generator_id", "eq"),
    ):
        if name not in parameters or parameters[name] is None:
            continue
        _require(column in columns, "unsupported_filter")
        raw = parameters[name]
        if name in {"start", "end"}:
            try:
                raw = _scalar(raw, "DATE")
            except BrowseError:
                raise BrowseError("invalid_date") from None
        else:
            _require(bool(_text(raw, 64)))
        filters.append({"column": column, "op": operation, "value": raw})
    if parameters.get("start") is not None and parameters.get("end") is not None:
        _require(parameters["start"] <= parameters["end"], "invalid_date_range")
    return {"search": search, "filters": _canonical_filters(filters, columns), "order": order}


def _validated_plan(spec: DatasetSpec, plan: Any) -> dict[str, Any]:
    """Require the complete canonical form again at the isolated worker boundary."""
    _require(isinstance(plan, dict) and set(plan) == {"search", "filters", "order"})
    _text(plan["search"], MAX_SEARCH)
    filters = plan["filters"]
    _require(isinstance(filters, list) and len(filters) <= MAX_CANONICAL_FILTERS)
    _require(filters == _canonical_filters(filters, dict(spec.columns)))
    order = plan["order"]
    _require(isinstance(order, list) and bool(order))
    _require(all(isinstance(item, dict) and set(item) == {"column", "direction"} for item in order))
    first = order[0]
    _require(isinstance(first["column"], str) and isinstance(first["direction"], str))
    _require(order == _order(spec, first["column"], first["direction"]))
    return plan


def compile_browse(dataset_id: str, normalized_query: dict, limit: int, last=None):
    """Compile one visible page plus a sentinel using bound values and total seek order.

    Return SQL, JSON-compatible parameters, and ordered key-column names. Non-null seek
    values must be strings in their declared types; nulls sort last in either direction.
    No OFFSET, count scan, filesystem reference, user SQL or arbitrary identifier is accepted.
    """
    spec = _spec(dataset_id)
    plan = _validated_plan(spec, normalized_query)
    _require(type(limit) is int and 1 <= limit <= 1000)
    columns = dict(spec.columns)
    parameters = []
    predicates = []

    def identifier(name):
        """Quote a name already selected from the application-owned schema."""
        return '"' + name + '"'

    def bound(value, kind):
        """Bind a scalar and cast using only an application-owned declared type."""
        parameters.append(value)
        return f"CAST(? AS {kind})"

    for item in plan["filters"]:
        column, operation = item["column"], item["op"]
        field, kind = identifier(column), columns[column]
        if operation in _NULL_OPS:
            predicates.append(f"{field} IS {'NOT ' if operation == 'not_null' else ''}NULL")
        elif operation in _COMPARISONS:
            predicates.append(f"{field} {_COMPARISONS[operation]} {bound(item['value'], kind)}")
        elif operation == "between":
            predicates.append(
                f"{field} BETWEEN {bound(item['value'], kind)} AND {bound(item['upper'], kind)}"
            )
        else:
            function = "starts_with" if operation == "starts_with" else "contains"
            prefix = "NOT " if operation == "not_contains" else ""
            predicates.append(f"{prefix}{function}({field}, {bound(item['value'], kind)})")
    if plan["search"]:
        parts = []
        for column in columns:
            parameters.append(plan["search"])
            parts.append(f"contains(lower(CAST({identifier(column)} AS VARCHAR)), lower(?))")
        predicates.append("(" + " OR ".join(parts) + ")")
    order_columns = [item["column"] for item in plan["order"]]
    if last is not None:
        try:
            _require(isinstance(last, list) and len(last) == len(order_columns))
            _require(all(value is None or isinstance(value, str) for value in last))
            _require(len(json.dumps(last, ensure_ascii=False).encode("utf-8")) <= MAX_SEEK_BYTES)
            values = [
                None
                if value is None
                else (
                    _text(value, MAX_SEEK_TEXT_BYTES)
                    if columns[column] == "VARCHAR"
                    else _scalar(value, columns[column])
                )
                for column, value in zip(order_columns, last, strict=True)
            ]
            _require(
                all(
                    value is None
                    or columns[column] != "VARCHAR"
                    or len(value.encode("utf-8")) <= MAX_SEEK_TEXT_BYTES
                    for column, value in zip(order_columns, values, strict=True)
                )
            )
        except (BrowseError, UnicodeError):
            raise BrowseError("invalid_cursor") from None
        alternatives = []
        for index, item in enumerate(plan["order"]):
            if values[index] is None:
                continue
            equal = [
                f"{identifier(order_columns[position])} IS NOT DISTINCT FROM {bound(values[position], columns[order_columns[position]])}"
                for position in range(index)
            ]
            comparison = ">" if item["direction"] == "asc" else "<"
            field = identifier(item["column"])
            after = f"({field} {comparison} {bound(values[index], columns[item['column']])} OR {field} IS NULL)"
            alternatives.append("(" + " AND ".join([*equal, after]) + ")")
        predicates.append("(" + " OR ".join(alternatives) + ")" if alternatives else "FALSE")
    query = f"SELECT {', '.join(map(identifier, columns))} FROM {identifier(spec.id)}"
    if predicates:
        query += " WHERE " + " AND ".join(f"({predicate})" for predicate in predicates)
    query += " ORDER BY " + ", ".join(
        f"{identifier(item['column'])} {item['direction'].upper()} NULLS LAST"
        for item in plan["order"]
    )
    parameters.append(limit + 1)
    return query + " LIMIT ?", parameters, order_columns
