"""Implement authenticated catalog, preview and restricted-SQL application services.

HTTP views validate transport shape, then delegate here with a server-derived Actor. Resolve
immutable models and authorize every named dataset before analytical I/O. Structured browse
plans run in a bounded native reader; user SQL retains the compiler/WASM sandbox. Public
pagination reads saved results; EIA download pagination is separate. Recheck identity before
caching or delivering data.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.utils import timezone

from outage_explorer.analytics.model_store import analytical_catalog, resolve_model
from outage_explorer.data.storage import StorageError
from outage_explorer.query.browse import BrowseError, compile_browse, normalize_query
from outage_explorer.query.browse_runtime import BrowseRuntimeError, run_browse
from outage_explorer.query.query_sandbox import SandboxError, run_query
from outage_explorer.query.sql_compiler import compile_sql

from .admission import query_slot, remaining_query_seconds
from .errors import APIError
from .models import QueryCacheEntry
from .policy import Actor, authorize_dataset, recheck

# Public domain separation for Django signing, not a secret or encryption key.
CURSOR_SALT = "outage-api-v1-cursor"


def integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    """Parse a canonical integer representation within inclusive minimum/maximum.

    Reject booleans, fractional values, padded/whitespace forms and strings that do not
    round-trip through int and str. Invalid input raises a 400 APIError; this avoids Python's
    permissive bool-as-int and float truncation semantics.
    """
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
    """Require nonempty UTF-8-encodable text within maximum characters.

    Reject null bytes, invalid surrogate code points and nonstrings with a safe 400 APIError.
    This validates request values without including their content in errors.
    """
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise APIError(400, "invalid_parameter", f"{name} is invalid.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise APIError(400, "invalid_parameter", f"{name} is invalid.") from None
    return value


def _date(value: str | None, name: str) -> str | None:
    """Return None or an exact ISO calendar-date string; reject alternate date forms.

    date.fromisoformat accepts more spellings than the API contract, so the round-trip
    comparison requires YYYY-MM-DD. Invalid input raises invalid_date.
    """
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
    """Create a compressed, signed, timestamped pagination token from payload.

    CURSOR_SALT is a public signing-purpose namespace; Django's configured secret provides
    authenticity. Signing protects integrity, not confidentiality.
    """
    return signing.dumps(payload, salt=CURSOR_SALT, compress=True)


def _read_cursor(value: str | None, context: dict, *, maximum: int = 4096) -> dict | None:
    """Return None or verify a token's signature, 15-minute age and bound context.

    Every context field must match the decoded payload, including purpose and Actor binding.
    Callers additionally validate page/model fields. Signature, expiry, decoded-shape
    and context failures become invalid_cursor. Earlier text validation failures
    retain invalid_parameter, including nonstring, empty or oversized token input.
    """
    if value is None:
        return None
    try:
        decoded = signing.loads(text(value, "cursor", maximum), salt=CURSOR_SALT, max_age=900)
        if not isinstance(decoded, dict) or any(decoded.get(k) != v for k, v in context.items()):
            raise ValueError
        return decoded
    except (signing.BadSignature, ValueError, TypeError):
        raise APIError(
            400, "invalid_cursor", "The cursor expired or does not match this request."
        ) from None


def _bundle(model_id: str | None):
    """Resolve a requested saved model, or the current publication when ID is None.

    Require a lowercase 32-hex identifier before model lookup. Convert unavailable or invalid
    catalog/model state to safe API errors without exposing filesystem paths. The returned
    bundle supplies the immutable manifest and model directory.
    """
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
    """Execute validated SQL against exactly one selected model file in the sandbox.

    Require the manifest filename, directory containment and nonsymlink path to agree.
    Reenter/reuse admission, recheck identity and pass the expected checksum, remaining
    deadline, row sentinel and output cap to run_query. Map sandbox failure categories to safe
    API errors; the engine verifies/transfers bytes and enforces process limits rather than
    receiving unrestricted native DuckDB access.
    """
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
    """Compile SQL in the bounded Python child and authorize every discovered table.

    Use at most two seconds within the shared admitted budget. Authorize discovered names even
    when the compiler reports an unsupported query shape, before any analytical I/O. Return
    the regenerated query only on successful policy checking; compiler availability/timeout
    and SQL-policy errors retain distinct API categories.
    """
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
    """Quote one SQL text value by doubling embedded single quotes.

    Use only for validated filter/key values in server-generated SQL, not identifiers or
    arbitrary SQL fragments. It does not grant permissions or replace validation.
    """
    return "'" + value.replace("'", "''") + "'"


def catalog(request, actor: Actor, model_id: str | None):
    """Return persona-filtered metadata for one pinned saved model.

    The trusted model adapter removes unavailable datasets/relationships. Recheck the Actor
    after metadata lookup so changed access is rejected before delivery.
    """
    bundle = _bundle(model_id)
    try:
        result = analytical_catalog(settings.OUTAGE_DATA_DIR, actor.role, model_id=bundle.id)
    except StorageError:
        raise APIError(503, "data_unavailable", "The analytical catalog is unavailable.") from None
    recheck(request, actor)
    return result


def preview(request, actor: Actor, dataset_id: str, parameters: dict):
    """Authorize a dataset and admit one filtered-preview operation.

    The wrapper establishes account/host limits and rechecks identity before the internal
    keyset-paging implementation can access analytical files.
    """
    authorize_dataset(actor, dataset_id)
    with query_slot(actor.user_id):
        recheck(request, actor)
        return _preview(request, actor, dataset_id, parameters)


def _preview(request, actor: Actor, dataset_id: str, parameters: dict):
    """Execute one full-dataset structured query with a stable model-pinned seek.

    Normalize only declared columns, typed operands and allowlisted operations before
    analytical I/O. Tokens bind the canonical query, page size, actor and immutable
    source/model publication. A new current publication does not redirect an existing
    investigation. Sorting includes unique primary-key ties and NULLS LAST; the cursor
    retains exact strings rather than lossy binary floating-point values.

    The isolated native reader accepts this structured plan, never user SQL. It reads
    Parquet with bounded resources and returns at most one page plus a sentinel. No
    full result count, deep OFFSET or first-1000-row search is performed.
    """
    authorize_dataset(actor, dataset_id)
    limit = integer(parameters.get("limit", 100), "limit", 1, 1000)
    cursor_limit = 16384 if request.method == "POST" else 4096
    if request.method == "GET" and len(urlencode(parameters)) > 8192:
        raise APIError(413, "browse_request_too_large", "Use a JSON POST for this browse request.")
    try:
        plan = normalize_query(dataset_id, parameters)
    except BrowseError as error:
        raise APIError(
            400, error.code, "Choose supported columns and valid typed filters."
        ) from None
    context = {
        "purpose": "browse-v1",
        "actor": actor.binding,
        "dataset": dataset_id,
        "limit": limit,
        "query": hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    cursor = _read_cursor(parameters.get("cursor"), context, maximum=cursor_limit)
    model_id = parameters.get("model_id")
    if cursor:
        if model_id is not None and model_id != cursor.get("model"):
            raise APIError(400, "invalid_cursor", "The cursor does not match this model.")
        model_id = cursor.get("model")
    last = cursor.get("last") if cursor else None
    if cursor and not isinstance(last, list):
        raise APIError(400, "invalid_cursor", "The cursor contains an invalid continuation.")
    try:
        _, _, keys = compile_browse(dataset_id, plan, limit, last)
    except BrowseError:
        raise APIError(
            400, "invalid_cursor", "The cursor contains an invalid continuation."
        ) from None
    bundle = _bundle(model_id)
    source = bundle.manifest["source_snapshot_id"]
    if cursor and cursor.get("source") != source:
        raise APIError(400, "invalid_cursor", "The cursor does not match this publication.")
    data = _run_browse(request, actor, bundle, dataset_id, plan, limit, last)
    rows = data["rows"][:limit]
    more = len(data["rows"]) > limit
    next_cursor = None
    if more and rows:
        names = [column["name"] for column in data["columns"]]
        last = [
            None
            if (value := rows[-1][names.index(key)]) is None
            else "true"
            if value is True
            else "false"
            if value is False
            else str(value)
            for key in keys
        ]
        try:
            # Compression can hide an oversized exact key. Check the reader's
            # scalar/vector bounds before publishing any continuation token.
            compile_browse(dataset_id, plan, limit, last)
        except BrowseError:
            raise APIError(
                413,
                "browse_cursor_too_large",
                "Sort values exceed the continuation size limit. Choose another sort column.",
            ) from None
        next_cursor = _cursor({**context, "model": bundle.id, "source": source, "last": last})
        if len(next_cursor) > cursor_limit or (
            request.method == "GET" and len(urlencode({**parameters, "cursor": next_cursor})) > 8192
        ):
            raise APIError(
                413,
                "browse_cursor_too_large",
                "This sort value exceeds the continuation limit. Use JSON POST or another sort column.",
            )
    recheck(request, actor)
    return {
        "dataset_id": dataset_id,
        "model_id": bundle.id,
        "source_snapshot_id": bundle.manifest["source_snapshot_id"],
        "columns": data["columns"],
        "rows": rows,
        "returned_rows": len(rows),
        "next_cursor": next_cursor,
        "filters": {
            name: parameters.get(name) for name in ("start", "end", "facility_id", "generator_id")
        },
        "query": plan,
    }


def _run_browse(request, actor, bundle, dataset_id, plan, limit, last):
    """Authorize and execute one generated plan against an integrity-verified file.

    Keep this entry separate from the arbitrary-SQL sandbox. The native child
    recompiles the closed structured plan and receives only its approved file.
    """
    authorize_dataset(actor, dataset_id)
    info = bundle.manifest["datasets"][dataset_id]
    path = bundle.path / f"{dataset_id}.parquet"
    if (
        info.get("file") != path.name
        or path.is_symlink()
        or path.resolve().parent != bundle.path.resolve()
    ):
        raise APIError(503, "data_integrity", "The selected dataset failed integrity validation.")
    recheck(request, actor)
    try:
        result = run_browse(
            path,
            dataset_id,
            plan,
            limit=limit,
            last=last,
            expected_sha256=info["sha256"],
            timeout_seconds=remaining_query_seconds(),
            max_output_bytes=settings.OUTAGE_QUERY_MAX_BYTES,
        )
    except BrowseRuntimeError as error:
        if error.code == "query_timeout":
            raise APIError(
                504, "query_timeout", "The browse request exceeded its execution deadline."
            ) from None
        if error.code == "result_too_large":
            raise APIError(
                413, "result_too_large", "The page exceeds the response size limit."
            ) from None
        if error.code == "data_integrity":
            raise APIError(
                503, "data_integrity", "The selected dataset failed integrity validation."
            ) from None
        if error.code == "runtime_unavailable":
            raise APIError(
                503, "browse_unavailable", "The structured browse reader is unavailable."
            ) from None
        raise APIError(
            422, "query_failed", "The page could not be read within its resource limits."
        ) from None
    recheck(request, actor)
    return result


def query(request, actor: Actor, body: dict):
    """Authorize the selected dataset and admit one user-SQL request.

    The internal path validates all parsed table references as well; authorizing the selection
    alone cannot protect against a hidden forbidden reference.
    """
    dataset_id = text(body.get("dataset_id"), "dataset_id")
    authorize_dataset(actor, dataset_id)
    with query_slot(actor.user_id):
        recheck(request, actor)
        return _query(request, actor, body)


def _query(request, actor: Actor, body: dict):
    """Return one page of a bounded deterministic SQL result, optionally from cache.

    Bind tokens/cache keys to Actor session/role, dataset, regenerated SQL, row/page limits
    and pinned model. Also bind cache reuse to the current publication even for an old model
    pin. Retain at most cap rows plus a truncation indicator; signed offset cursors page only
    that retained result. Entries expire after 60 seconds, hits do not renew expiry, and
    writes prune to eight entries per user. Recheck identity before cache insertion and every
    delivery, including cache hits.
    """
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
    # Old model cursors remain reproducible, but any new current publication
    # retires prior cache generations even if physical cache cleanup fails.
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
