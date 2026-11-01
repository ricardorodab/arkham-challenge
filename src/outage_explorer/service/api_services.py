"""Authorize first, pin immutable data, then execute outside the Django process."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any

from django.conf import settings
from django.core import signing
from django.utils import timezone

from outage_explorer.model_contracts import DATASETS
from outage_explorer.model_store import analytical_catalog, resolve_model
from outage_explorer.query_sandbox import SandboxError, run_query
from outage_explorer.sql_compiler import compile_sql
from outage_explorer.storage import StorageError

from .admission import query_slot, remaining_query_seconds
from .errors import APIError
from .models import QueryCacheEntry
from .policy import Actor, authorize_dataset, recheck

CURSOR_SALT = "outage-api-v1-cursor"


def integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise APIError(400, "invalid_parameter", f"{name} must be an integer in its allowed range.")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = minimum - 1
    if str(number) != str(value) or not minimum <= number <= maximum:
        raise APIError(400, "invalid_parameter", f"{name} must be an integer in its allowed range.")
    return number


def text(value: Any, name: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise APIError(400, "invalid_parameter", f"{name} is invalid.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise APIError(400, "invalid_parameter", f"{name} is invalid.") from None
    return value


def _date(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return value
    except (TypeError, ValueError):
        pass
    raise APIError(400, "invalid_date", f"{name} must be an ISO calendar date.")


def _cursor(payload: dict) -> str:
    return signing.dumps(payload, salt=CURSOR_SALT, compress=True)


def _read_cursor(value: str | None, context: dict) -> dict | None:
    if value is None:
        return None
    try:
        decoded = signing.loads(text(value, "cursor", 4096), salt=CURSOR_SALT, max_age=900)
        if not isinstance(decoded, dict) or any(decoded.get(k) != v for k, v in context.items()):
            raise ValueError
        return decoded
    except (signing.BadSignature, ValueError, TypeError):
        raise APIError(
            400, "invalid_cursor", "The cursor expired or does not match this request."
        ) from None


def _bundle(model_id: str | None):
    if model_id is not None and (
        not isinstance(model_id, str) or re.fullmatch(r"[0-9a-f]{32}", model_id) is None
    ):
        raise APIError(400, "invalid_model_id", "The model identifier is invalid.")
    try:
        return resolve_model(settings.OUTAGE_DATA_DIR, model_id)
    except StorageError as error:
        if error.code == "invalid_identifier":
            raise APIError(400, "invalid_model_id", "The model identifier is invalid.") from None
        if error.code == "no_model":
            raise APIError(503, "data_unavailable", "No analytical model is published.") from None
        raise APIError(
            503, "data_unavailable", "The requested analytical version is unavailable."
        ) from None


def _run(request, actor: Actor, bundle, dataset_id: str, validated):
    info = bundle.manifest["datasets"][dataset_id]
    path = bundle.path / f"{dataset_id}.parquet"
    if (
        info.get("file") != path.name
        or path.is_symlink()
        or path.resolve().parent != bundle.path.resolve()
    ):
        raise APIError(503, "data_integrity", "The selected dataset failed integrity validation.")
    try:
        with query_slot(actor.user_id):
            recheck(request, actor)
            return run_query(
                path,
                dataset_id,
                validated.sql,
                expected_sha256=info["sha256"],
                timeout_seconds=remaining_query_seconds(),
                max_rows=validated.row_limit + 1,
                max_output_bytes=settings.OUTAGE_QUERY_MAX_BYTES,
            )
    except SandboxError as error:
        if error.code in {"query_timeout", "timeout"}:
            raise APIError(
                504, "query_timeout", "The query exceeded its execution deadline."
            ) from None
        if error.code in {"result_too_large", "output_limit"}:
            raise APIError(
                413, "result_too_large", "The result exceeds the response size limit."
            ) from None
        if error.code in {"sandbox_unavailable", "runtime_unavailable"}:
            raise APIError(
                503, "sandbox_unavailable", "The isolated query runtime is unavailable."
            ) from None
        if error.code in {"integrity_error", "data_integrity", "source_changed"}:
            raise APIError(
                503, "data_integrity", "The selected dataset failed integrity validation."
            ) from None
        raise APIError(
            422, "query_failed", "The query could not be executed within the supported contract."
        ) from None


def _validate(actor, sql, dataset_id, max_rows):
    try:
        compilation = compile_sql(
            sql, dataset_id, max_rows=max_rows, timeout_seconds=remaining_query_seconds(2)
        )
    except SandboxError as error:
        if error.code == "query_timeout":
            raise APIError(
                504, "query_timeout", "The query exceeded its execution deadline."
            ) from None
        raise APIError(
            503, "compiler_unavailable", "The bounded SQL compiler is unavailable."
        ) from None
    for referenced in compilation.dataset_ids:
        authorize_dataset(actor, referenced)
    if compilation.error is not None:
        raise APIError(422, compilation.error.code, str(compilation.error))
    return compilation.query


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def catalog(request, actor: Actor, model_id: str | None):
    bundle = _bundle(model_id)
    try:
        result = analytical_catalog(settings.OUTAGE_DATA_DIR, actor.role, model_id=bundle.id)
    except StorageError:
        raise APIError(503, "data_unavailable", "The analytical catalog is unavailable.") from None
    recheck(request, actor)
    return result


def preview(request, actor: Actor, dataset_id: str, parameters: dict):
    authorize_dataset(actor, dataset_id)
    with query_slot(actor.user_id):
        recheck(request, actor)
        return _preview(request, actor, dataset_id, parameters)


def _preview(request, actor: Actor, dataset_id: str, parameters: dict):
    authorize_dataset(actor, dataset_id)
    limit = integer(parameters.get("limit", 100), "limit", 1, 1000)
    start, end = _date(parameters.get("start"), "start"), _date(parameters.get("end"), "end")
    if start and end and start > end:
        raise APIError(400, "invalid_date_range", "start must be on or before end.")
    facility = parameters.get("facility_id")
    columns = dict(DATASETS[dataset_id].columns)
    if facility is not None:
        facility = text(facility, "facility_id", 64)
        if "facility_id" not in columns:
            raise APIError(
                400, "unsupported_filter", "This dataset does not support the facility filter."
            )
    if (start or end) and "period" not in columns:
        raise APIError(400, "unsupported_filter", "This dataset does not support date filters.")
    context = {
        "purpose": "preview",
        "actor": actor.binding,
        "dataset": dataset_id,
        "limit": limit,
        "start": start,
        "end": end,
        "facility": facility,
    }
    cursor = _read_cursor(parameters.get("cursor"), context)
    model_id = parameters.get("model_id")
    if cursor:
        if model_id is not None and model_id != cursor.get("model"):
            raise APIError(400, "invalid_cursor", "The cursor does not match this model.")
        model_id = cursor.get("model")
    bundle = _bundle(model_id)
    conditions = []
    for key, operator, value in (
        ("period", ">=", start),
        ("period", "<=", end),
        ("facility_id", "=", facility),
    ):
        if value is not None:
            literal = _literal(value)
            if key == "period":
                literal = f"CAST({literal} AS DATE)"
            conditions.append(f'"{key}" {operator} {literal}')
    keys = DATASETS[dataset_id].primary_key
    if cursor:
        last = cursor.get("last")
        if (
            not isinstance(last, list)
            or len(last) != len(keys)
            or any(not isinstance(v, str) for v in last)
        ):
            raise APIError(400, "invalid_cursor", "The cursor is invalid.")
        alternatives = []
        for index, key in enumerate(keys):
            equals = [
                f'"{k}" = {_literal(v)}' for k, v in zip(keys[:index], last[:index], strict=True)
            ]
            greater = f'"{key}" > {_literal(last[index])}'
            alternatives.append("(" + " AND ".join([*equals, greater]) + ")")
        conditions.append("(" + " OR ".join(alternatives) + ")")
    sql = f'SELECT * FROM "{dataset_id}"'
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    validated = _validate(actor, sql, dataset_id, limit)
    data = _run(request, actor, bundle, dataset_id, validated)
    rows = data["rows"][:limit]
    more = len(data["rows"]) > limit
    next_cursor = None
    if more and rows:
        names = [column["name"] for column in data["columns"]]
        last = [str(rows[-1][names.index(key)]) for key in keys]
        next_cursor = _cursor({**context, "model": bundle.id, "last": last})
    recheck(request, actor)
    return {
        "dataset_id": dataset_id,
        "model_id": bundle.id,
        "source_snapshot_id": bundle.manifest["source_snapshot_id"],
        "columns": data["columns"],
        "rows": rows,
        "returned_rows": len(rows),
        "next_cursor": next_cursor,
        "filters": {"start": start, "end": end, "facility_id": facility},
    }


def query(request, actor: Actor, body: dict):
    dataset_id = text(body.get("dataset_id"), "dataset_id")
    authorize_dataset(actor, dataset_id)
    with query_slot(actor.user_id):
        recheck(request, actor)
        return _query(request, actor, body)


def _query(request, actor: Actor, body: dict):
    dataset_id = text(body.get("dataset_id"), "dataset_id")
    authorize_dataset(actor, dataset_id)
    sql = text(body.get("sql"), "sql", 16384)
    cap = integer(body.get("max_rows", 1000), "max_rows", 1, 1000)
    page_size = integer(body.get("page_size", min(100, cap)), "page_size", 1, cap)
    validated = _validate(actor, sql, dataset_id, cap)
    fingerprint = hashlib.sha256(validated.sql.encode()).hexdigest()
    context = {
        "purpose": "query",
        "actor": actor.binding,
        "dataset": dataset_id,
        "sql": fingerprint,
        "cap": cap,
        "page_size": page_size,
    }
    cursor = _read_cursor(body.get("cursor"), context)
    requested_model = body.get("model_id")
    if cursor:
        if requested_model is not None and requested_model != cursor.get("model"):
            raise APIError(400, "invalid_cursor", "The cursor does not match this model.")
        requested_model = cursor.get("model")
    bundle = _bundle(requested_model)
    publication = _bundle(None).id
    cache_key = hashlib.sha256(
        json.dumps(
            {**context, "model": bundle.id, "publication": publication}, sort_keys=True
        ).encode()
    ).hexdigest()
    entry = QueryCacheEntry.objects.filter(
        key=cache_key,
        owner_id=actor.user_id,
        role=actor.role,
        model_id=bundle.id,
        publication_id=publication,
        expires_at__gt=timezone.now(),
    ).first()
    cached = entry is not None
    if entry:
        payload = entry.payload
    else:
        data = _run(request, actor, bundle, dataset_id, validated)
        payload = {
            "columns": data["columns"],
            "rows": data["rows"][:cap],
            "truncated": len(data["rows"]) > cap,
        }
        recheck(request, actor)
        now = timezone.now()
        QueryCacheEntry.objects.filter(expires_at__lte=now).delete()
        QueryCacheEntry.objects.update_or_create(
            key=cache_key,
            defaults={
                "owner_id": actor.user_id,
                "role": actor.role,
                "model_id": bundle.id,
                "publication_id": publication,
                "expires_at": now + timedelta(seconds=60),
                "payload": payload,
            },
        )
        obsolete = list(
            QueryCacheEntry.objects.filter(owner_id=actor.user_id)
            .order_by("-created_at")
            .values_list("key", flat=True)[8:]
        )
        if obsolete:
            QueryCacheEntry.objects.filter(key__in=obsolete, owner_id=actor.user_id).delete()
    offset = 0 if cursor is None else integer(cursor.get("offset"), "cursor offset", 0, cap)
    rows = payload["rows"][offset : offset + page_size]
    following = offset + len(rows)
    next_cursor = (
        _cursor({**context, "model": bundle.id, "offset": following})
        if following < len(payload["rows"])
        else None
    )
    recheck(request, actor)
    return {
        "dataset_id": dataset_id,
        "model_id": bundle.id,
        "source_snapshot_id": bundle.manifest["source_snapshot_id"],
        "columns": payload["columns"],
        "rows": rows,
        "returned_rows": len(rows),
        "result_rows": len(payload["rows"]),
        "truncated": payload["truncated"],
        "next_cursor": next_cursor,
        "cache_hit": cached,
        "cache_ttl_seconds": 60,
        "max_rows": cap,
    }
