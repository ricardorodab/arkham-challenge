"""Extract all three EIA grains and publish a coherent raw/model generation.

Route work stages typed files and replaces refresh intervals without editing
published snapshots. The writer lock spans recovery through candidate preparation;
one data-catalog transaction advances raw/model references after validation. Live
EIA pagination and the separate application's refresh-job transaction are outside
that atomic publication boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from . import __version__
from .domain import (
    ROUTES,
    SCHEMA_VERSION,
    DataValidationError,
    RouteSpec,
    canonical_json,
    identifier,
    validate_row,
)
from .eia import EIAClient, EIAClientError, RouteMetadata
from .storage import Catalog, SnapshotRef, StorageError, writer_lock

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestConfig:
    """Define an immutable extraction scope and local execution policy.

    ``recent`` uses stored route watermarks with a 30-day correction overlap; bounded
    ``full`` replaces only its interval, while unbounded ``full`` replaces history.
    ``workers`` overlaps routes, never pages within one route. ``record_responses``
    enables sanitized evidence under the run directory.
    """

    data_dir: Path = Path("data")
    mode: str = "recent"
    start: date | None = None
    end: date | None = None
    page_size: int = 5000
    workers: int = 1
    record_responses: bool = False

    def __post_init__(self) -> None:
        """Reject invalid modes, date combinations, page sizes or worker counts.

        An explicit start requires full mode; the route metadata later bounds actual
        intervals. Raise ``ValueError`` before any extraction starts.
        """
        if self.mode not in {"full", "recent"}:
            raise ValueError("Mode must be full or recent.")
        if self.start and self.mode != "full":
            raise ValueError("An explicit start requires --mode full.")
        if self.start and self.end and self.start > self.end:
            raise ValueError("Start must not be after end.")
        if not 1 <= self.page_size <= 5000 or not 1 <= self.workers <= 3:
            raise ValueError("Page size must be 1..5000 and workers 1..3.")


def utc_now() -> str:
    """Return an aware UTC ISO timestamp for operational records."""
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: Any) -> None:
    """Write canonical UTF-8 JSON with a newline, flush and fsync the file.

    Create parent directories as needed and overwrite the target. The caller must
    supply sanitized, JSON-native content and a suitable staging/diagnostic path.
    This is a durable file write, not an atomic publication or rename operation;
    serialization and filesystem errors propagate.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    """Fsync a directory on POSIX; perform no directory sync on Windows.

    Call after staging writes or rename to strengthen filesystem durability where
    supported. Filesystem errors propagate; no universal power-loss guarantee is
    implied for platforms without portable directory synchronization.
    """
    # Windows does not expose POSIX directory fsync. SQLite FULL still protects
    # the publication transaction; power-loss guarantees are OS/filesystem-specific.
    if os.name != "nt":
        handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)


def _sha256(path: Path) -> str:
    """Return the SHA-256 of file bytes, propagating filesystem errors."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json_value(value: Any) -> Any:
    """Normalize dates and Decimals for typed staging JSON without float coercion."""
    return (
        value.isoformat()
        if isinstance(value, date)
        else str(value)
        if isinstance(value, Decimal)
        else value
    )


def _bounds(
    config: IngestConfig, meta: RouteMetadata, prior: dict[str, Any] | None
) -> tuple[date, date]:
    """Calculate one route's inclusive extraction interval from config and metadata.

    Recent runs with prior data start at watermark minus 29 days, bounded by source
    start; the first run backfills. End is the lesser of metadata/requested end.
    Reject coverage regression or an empty overlap with ``EIAClientError`` before
    page requests. A recent window cannot discover corrections older than it.
    """
    start = max(meta.start_period, config.start or meta.start_period)
    if prior:
        watermark = date.fromisoformat(prior["watermark"])
        if meta.end_period < watermark or meta.start_period > date.fromisoformat(
            prior["min_period"]
        ):
            raise EIAClientError(
                "metadata_regressed",
                "EIA advertised coverage shrank relative to the stored snapshot.",
            )
    if config.mode == "recent" and prior:
        start = max(meta.start_period, watermark - timedelta(days=29))
    end = min(meta.end_period, config.end or meta.end_period)
    if start > end:
        raise EIAClientError(
            "configuration", "Requested interval does not overlap all three routes."
        )
    return start, end


def _extract(
    con: duckdb.DuckDBPyConnection,
    spec: RouteSpec,
    client: EIAClient,
    config: IngestConfig,
    start: date,
    end: date,
    table: str,
    stage: Path,
    recording: Path | None,
    cancelled: threading.Event,
) -> dict[str, Any]:
    """Populate typed incoming/clean staging tables from one paginated extraction.

    Validate sanitized rows and stage one page at a time through explicit JSON types
    so text IDs and Decimal precision survive DuckDB loading. Collapse identical
    key/hash duplicates; reject empty extraction or conflicting keys. Return page,
    row, additive-field and observed-date counts. ``table`` and the connection are
    application-owned; the tables remain available to the caller.

    Optional recordings contain sanitized page evidence. The page scratch file is
    removed on exit; created tables and recordings may remain on failure. Cancellation
    is checked between pages, not inside an active HTTP request. Domain, source,
    DuckDB and filesystem errors propagate to the enclosing ingestion run.
    """
    columns = ",".join(f"{identifier(name)} {kind}" for name, kind in spec.columns)
    con.execute(f"CREATE TABLE {table} ({columns})")
    schema = "{" + ",".join(f"'{name}':'{kind}'" for name, kind in spec.columns) + "}"
    page_file = stage / f"{spec.name}-{table}.jsonl"
    count, pages = 0, 0
    extras: set[str] = set()
    iterator = iter(client.iter_pages(spec.route, start, end, config.page_size))
    try:
        while True:
            if cancelled.is_set():
                raise EIAClientError("cancelled", "Another route failed; extraction was cancelled.")
            try:
                page = next(iterator)
            except StopIteration:
                break
            if recording:
                payload = dict(page.metadata)
                payload["response"] = {**payload.get("response", {}), "data": page.rows}
                write_json(recording / f"page-{page.offset:012d}.json", client.sanitize(payload))
            with page_file.open("w", encoding="utf-8", newline="\n") as stream:
                for raw in page.rows:
                    row = validate_row(spec, client.sanitize(raw), start, end)
                    extras.update(row.extra_fields)
                    normalized = {
                        name: _json_value(value)
                        for (name, _), value in zip(spec.columns, row.values, strict=True)
                    }
                    stream.write(canonical_json(normalized) + "\n")
            # The page has already passed the domain contract. Explicit JSON
            # types prevent numeric-looking facility IDs losing leading zeroes.
            con.execute(
                f"INSERT INTO {table} SELECT * FROM read_json(?, columns={schema}, "
                "format='newline_delimited', maximum_object_size=16777216)",
                [str(page_file)],
            )
            count += len(page.rows)
            pages += 1
    finally:
        page_file.unlink(missing_ok=True)
    if not count:
        raise DataValidationError(
            f"{spec.name}: unexpectedly empty extraction; snapshot not published."
        )
    key = ",".join(identifier(name) for name in spec.key_fields)
    conflict = con.execute(
        f"SELECT 1 FROM {table} GROUP BY {key} HAVING count(DISTINCT _row_hash)>1 LIMIT 1"
    ).fetchone()
    if conflict:
        raise DataValidationError(f"{spec.name}: conflicting duplicate natural key.")
    con.execute(
        f"CREATE TABLE {table}_clean AS SELECT * FROM {table} "
        f"QUALIFY row_number() OVER (PARTITION BY {key} ORDER BY _row_hash)=1"
    )
    unique_count = con.execute(f"SELECT count(*) FROM {table}_clean").fetchone()[0]
    observed_days = con.execute(f"SELECT count(DISTINCT period) FROM {table}_clean").fetchone()[0]
    return {
        "extracted_rows": count,
        "pages": pages,
        "identical_duplicates_collapsed": count - unique_count,
        "extra_fields": sorted(extras),
        "requested_days": (end - start).days + 1,
        "observed_days": observed_days,
        "missing_requested_days": (end - start).days + 1 - observed_days,
    }


def _logical_summary(
    con: duckdb.DuckDBPyConnection, table: str, spec: RouteSpec
) -> tuple[str, dict[str, Any]]:
    """Hash source-row fingerprints in key order and count retained quality flags.

    Return the ordered logical SHA-256 and per-flag/flagged-row counts for a trusted
    staging table. Reuse of canonical source hashes makes comparisons include names
    and additive fields, rather than only numeric measures. No table is modified.
    """
    digest = hashlib.sha256()
    counts: Counter[str] = Counter()
    flagged = 0
    key = ",".join(identifier(name) for name in spec.key_fields)
    reader = con.execute(f"SELECT _row_hash,_quality_flags FROM {table} ORDER BY {key}")
    while batch := reader.fetchmany(10_000):
        for row_hash, raw_flags in batch:
            digest.update(row_hash.encode("ascii") + b"\n")
            flags = json.loads(raw_flags)
            counts.update(flags)
            flagged += bool(flags)
    return digest.hexdigest(), {"rows_with_flags": flagged, "flags": dict(sorted(counts.items()))}


def _route(
    spec: RouteSpec,
    meta: RouteMetadata,
    config: IngestConfig,
    client: EIAClient,
    current: SnapshotRef | None,
    stage: Path,
    run_dir: Path,
    cancelled: threading.Event,
) -> dict[str, Any]:
    """Prepare one route's replacement dataset and manifest entry without publishing.

    Use a route-local one-thread DuckDB staging database. Unbounded full extraction
    replaces history; all other scopes replace only the refreshed interval. Confirm
    proposed deletions with a second independently paginated logical digest. Reject
    missing advertised boundaries, but retain interior gaps and semantic flags.

    Write sorted ZSTD Parquet, fsync it, and return counts, hashes, coverage, drift and
    fetch provenance. Remove the temporary DuckDB files best-effort; candidate files
    or sanitized recordings may remain after failure. No catalog references advance
    here, and matching extractions do not establish an upstream transaction.
    """
    started = utc_now()
    prior = current.manifest["routes"][spec.name] if current else None
    start, end = _bounds(config, meta, prior)
    record_dir = run_dir / spec.name if config.record_responses else None
    if record_dir:
        write_json(record_dir / "metadata.json", client.sanitize(meta.raw))
    LOG.info("route_started", extra={"route": spec.route, "start": str(start), "end": str(end)})
    database = stage / f"{spec.name}.duckdb"
    con = duckdb.connect(str(database), config={"threads": 1, "memory_limit": "256MB"})
    try:
        con.execute("SET threads=1")
        con.execute("SET memory_limit='256MB'")
        extracted = _extract(
            con, spec, client, config, start, end, "incoming", stage, record_dir, cancelled
        )
        first_period, last_period = con.execute(
            "SELECT min(period),max(period) FROM incoming_clean"
        ).fetchone()
        if (start == meta.start_period and first_period != start) or (
            end == meta.end_period and last_period != end
        ):
            raise DataValidationError(
                f"{spec.name}: source-advertised boundary is missing; extraction may be truncated."
            )
        if current:
            con.execute(
                "CREATE TABLE previous AS SELECT * FROM read_parquet(?)",
                [str(current.path / prior["file"])],
            )
        else:
            con.execute("CREATE TABLE previous AS SELECT * FROM incoming_clean WHERE false")
        if config.mode == "full" and config.start is None and config.end is None:
            con.execute("CREATE TABLE result AS SELECT * FROM incoming_clean")
        else:
            con.execute(
                "CREATE TABLE result AS SELECT * FROM previous WHERE period NOT BETWEEN ? AND ? "
                "UNION ALL SELECT * FROM incoming_clean",
                [start, end],
            )
        keys = ",".join(identifier(name) for name in spec.key_fields)
        added, deleted, changed = con.execute(
            "SELECT count(*) FILTER (WHERE p._row_hash IS NULL), "
            "count(*) FILTER (WHERE n._row_hash IS NULL), "
            "count(*) FILTER (WHERE p._row_hash <> n._row_hash) "
            f"FROM previous p FULL OUTER JOIN result n USING ({keys})"
        ).fetchone()
        confirmation = False
        if deleted:
            # A missing tail/page must not silently delete prior observations.
            # Confirm deletions with a second independently paginated extraction.
            confirm_record = record_dir / "confirmation" if record_dir else None
            _extract(
                con,
                spec,
                client,
                config,
                start,
                end,
                "confirmation",
                stage,
                confirm_record,
                cancelled,
            )
            first_digest, _ = _logical_summary(con, "incoming_clean", spec)
            second_digest, _ = _logical_summary(con, "confirmation_clean", spec)
            if first_digest != second_digest:
                raise DataValidationError(
                    f"{spec.name}: deletion confirmation changed during extraction."
                )
            confirmation = True
        logical_hash, quality = _logical_summary(con, "result", spec)
        count, min_period, max_period, days = con.execute(
            "SELECT count(*),min(period),max(period),count(DISTINCT period) FROM result"
        ).fetchone()
        parquet = stage / f"{spec.name}.parquet"
        con.execute(
            f"COPY (SELECT * FROM result ORDER BY {keys}) TO ? "
            "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)",
            [str(parquet)],
        )
    finally:
        con.close()
        with suppress(OSError):
            database.unlink(missing_ok=True)
            database.with_suffix(".duckdb.wal").unlink(missing_ok=True)
    # Windows _commit (os.fsync) requires a writable file descriptor. This is
    # still staging: no published immutable file is ever opened for writing.
    with parquet.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    declared = client.statistics.get("declared_totals", {}).get(spec.route)
    quality.update(
        {
            "invalid_rows": 0,
            "identical_duplicates_collapsed": extracted["identical_duplicates_collapsed"],
            "missing_calendar_days": (max_period - min_period).days + 1 - days,
            "missing_requested_days": extracted["missing_requested_days"],
            "declared_total_mismatch": declared is not None
            and declared != extracted["extracted_rows"],
        }
    )
    result = {
        "route": spec.route,
        "file": parquet.name,
        "rows": count,
        "sha256": _sha256(parquet),
        "logical_sha256": logical_hash,
        "min_period": str(min_period),
        "max_period": str(max_period),
        "watermark": str(max_period),
        "schema": [list(column) for column in spec.columns],
        "quality": quality,
        **extracted,
        "source_declared_total": declared,
        "extraction_start": str(start),
        "extraction_end": str(end),
        "fetch_started_at": started,
        "fetch_finished_at": utc_now(),
        "added": added,
        "changed": changed,
        "deleted": deleted,
        "deletions_confirmed": confirmation,
        "metadata": client.sanitize(meta.raw),
    }
    old_extras = set(prior.get("extra_fields", [])) if prior else set()
    new_extras = set(extracted["extra_fields"])
    result["new_extra_fields"] = sorted(new_extras - old_extras)
    result["missing_previous_extra_fields"] = sorted(old_extras - new_extras)
    if prior and not (config.mode == "full" and config.start is None and config.end is None):
        result["extra_fields"] = sorted(old_extras | new_extras)
    LOG.info(
        "route_completed",
        extra={
            "route": spec.route,
            "rows": count,
            "added": added,
            "changed": changed,
            "deleted": deleted,
        },
    )
    return result


def ingest(config: IngestConfig, client: EIAClient) -> dict[str, Any]:
    """Fetch, validate, model and jointly publish all source grains under one lock.

    Recover abandoned run/build records, verify the prior source, validate every
    route's metadata bounds, and stage route work. Only after the raw candidate and
    matching full model are ready does ``Catalog.publish`` commit the raw/model
    references and ingestion/build success records together. Return the sanitized raw
    manifest; its duration ends before modeling, unlike the final success log.

    Before catalog commit, failures retain the prior published pair. Unpublished
    staging or immutable candidates may remain. Persist failure/quality diagnostics
    best-effort without masking the original error or downgrading committed success.
    Source, validation, storage and interruption errors propagate; unexpected failures
    become safe ``StorageError`` values. This function does not complete the separate
    Django application refresh job or make EIA pages transactionally consistent.
    """
    root = config.data_dir.resolve()
    with writer_lock(root), Catalog(root) as catalog:
        recovered = catalog.recover_interrupted()
        if recovered:
            LOG.warning("runs_recovered", extra={"runs": recovered})
        current = catalog.current()
        if current:
            catalog.verify()
            if current.manifest["schema_version"] != SCHEMA_VERSION:
                raise StorageError("Stored schema requires an explicit migration.")
        run_id = uuid.uuid4().hex
        stage = root / ".staging" / run_id
        stage.mkdir()
        run_dir = root / "runs" / run_id
        run_dir.mkdir()
        catalog.start_run(
            run_id,
            config.mode,
            str(config.start) if config.start else None,
            str(config.end) if config.end else None,
        )
        started = utc_now()
        timer = time.monotonic()
        cancelled = threading.Event()
        route_results: dict[str, Any] = {}
        candidate_model = None
        LOG.info("run_started", extra={"run_id": run_id, "mode": config.mode})
        try:
            metadata = {spec.name: client.fetch_metadata(spec.route) for spec in ROUTES}
            # Validate all intervals before starting the first data request.
            for spec in ROUTES:
                prior = current.manifest["routes"][spec.name] if current else None
                _bounds(config, metadata[spec.name], prior)
            with ThreadPoolExecutor(max_workers=config.workers) as pool:
                futures = {
                    pool.submit(
                        _route,
                        spec,
                        metadata[spec.name],
                        config,
                        client,
                        current,
                        stage,
                        run_dir,
                        cancelled,
                    ): spec
                    for spec in ROUTES
                }
                try:
                    for future in as_completed(futures):
                        route_results[futures[future].name] = future.result()
                except BaseException:
                    cancelled.set()
                    for future in futures:
                        future.cancel()
                    raise
            manifest = client.sanitize(
                {
                    "schema_version": SCHEMA_VERSION,
                    "connector_version": __version__,
                    "duckdb_version": duckdb.__version__,
                    "snapshot_id": run_id,
                    "run_id": run_id,
                    "created_at": utc_now(),
                    "started_at": started,
                    "duration_seconds": round(time.monotonic() - timer, 3),
                    "parent_snapshot_id": current.id if current else None,
                    "mode": config.mode,
                    "overlap_days": 30,
                    "source_consistency": "EIA is live; local publication is atomic, source pages are not a transaction.",
                    "routes": {spec.name: route_results[spec.name] for spec in ROUTES},
                    "http": client.statistics,
                }
            )
            write_json(stage / "manifest.json", manifest)
            write_json(
                run_dir / "quality.json",
                {
                    "status": "validated",
                    "publication_authority": "catalog.sqlite3",
                    "routes": {name: value["quality"] for name, value in route_results.items()},
                },
            )
            _sync_directory(stage)
            snapshot = root / "snapshots" / run_id
            os.replace(stage, snapshot)
            _sync_directory(snapshot.parent)
            # Build from closed immutable sources; publish both generations in
            # one catalog transaction only after all transformations validate.
            from .model_store import prepare_model

            candidate_model = prepare_model(catalog, SnapshotRef(run_id, snapshot, manifest))
            catalog.publish(run_id, snapshot, manifest, model=candidate_model)
            LOG.info(
                "run_succeeded",
                extra={"run_id": run_id, "duration_seconds": round(time.monotonic() - timer, 3)},
            )
            return manifest
        except BaseException as error:
            cancelled.set()
            if candidate_model is not None:
                try:
                    catalog.fail_model_build(
                        candidate_model.id,
                        "Combined publication failed.",
                        interrupted=isinstance(error, KeyboardInterrupt),
                    )
                except Exception:
                    LOG.error("model_status_write_failed", extra={"run_id": run_id})
            if isinstance(error, KeyboardInterrupt):
                code, message = "interrupted", "Interrupted by the operator."
            elif isinstance(error, (EIAClientError, DataValidationError, StorageError)):
                code, message = error.code, str(error)
            else:
                code, message = (
                    "internal_error",
                    "Connector failed; the previous snapshot remains current.",
                )
            failure = client.sanitize(
                {
                    "code": code,
                    "message": message,
                    "http": client.statistics,
                    "completed_routes": route_results,
                    "finished_at": utc_now(),
                    "error_type": type(error).__name__,
                    "errno": getattr(error, "errno", None),
                    "retryable": getattr(error, "retryable", False),
                    "retry_at": (
                        error.retry_at.isoformat()
                        if isinstance(error, EIAClientError) and error.retry_at
                        else None
                    ),
                }
            )
            # Reporting must not mask the original failure, especially when the
            # failure itself was a full disk or lost filesystem permissions.
            try:
                catalog.fail_run(run_id, code, failure["message"], failure)
            except Exception:
                LOG.error("run_status_write_failed", extra={"run_id": run_id})
            try:
                write_json(run_dir / "failure.json", failure)
                write_json(
                    run_dir / "quality.json",
                    {
                        "status": "interrupted" if code == "interrupted" else "failed",
                        "error_code": code,
                        "validation_failed": code == "invalid_data",
                        "incomplete_routes": [
                            spec.name for spec in ROUTES if spec.name not in route_results
                        ],
                        "routes": {name: value["quality"] for name, value in route_results.items()},
                    },
                )
            except Exception:
                LOG.error("failure_artifact_write_failed", extra={"run_id": run_id})
            LOG.error("run_failed", extra={"run_id": run_id, "error_code": code})
            if isinstance(
                error, (KeyboardInterrupt, EIAClientError, DataValidationError, StorageError)
            ):
                raise
            raise StorageError(message, code="internal_error") from None
