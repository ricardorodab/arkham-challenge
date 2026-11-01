"""Model integrity, immutable publication and a scoped backend catalog adapter.

Connections here run application-owned analytical SQL. They are not a sandbox
for user SQL; the public query service and authentication belong to Part 3.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any

import duckdb

from .domain import ROUTES, canonical_json, identifier
from .model_contracts import DATASETS, contract_profile
from .modeling import ModelBundle, ModelError, build_model, logical_digest
from .storage import Catalog, SnapshotRef, StorageError, writer_lock

LOG = logging.getLogger(__name__)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def register_views(
    con: duckdb.DuckDBPyConnection, bundle: ModelBundle, dataset_ids: Sequence[str]
) -> None:
    """Register only caller-approved datasets, with server-resolved paths."""
    for dataset_id in dataset_ids:
        if dataset_id not in DATASETS:
            raise StorageError("Unknown analytical dataset.", code="unknown_dataset")
        entry = bundle.manifest["datasets"][dataset_id]
        if entry.get("file") != f"{dataset_id}.parquet":
            raise StorageError("Model data path is invalid.", code="corrupt_model")
        source = bundle.path / entry["file"]
        if (
            source.is_symlink()
            or not source.is_file()
            or source.resolve().parent != bundle.path.resolve()
        ):
            raise StorageError("Model data path is unsafe.", code="corrupt_model")
        with source.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        if checksum != entry.get("sha256"):
            raise StorageError("Model file checksum differs.", code="corrupt_model")
        # CREATE VIEW cannot bind a filename parameter. The path is resolved by
        # the manifest contract and escaped as a SQL literal, never interpolated
        # from client SQL. Avoid DuckDB 2 relation/cursor lifetime interactions.
        con.execute(
            f"CREATE VIEW {identifier(dataset_id)} AS SELECT * FROM "
            f"read_parquet({_sql_literal(str(source))}, hive_partitioning=false)"
        ).fetchall()


def verify_model(bundle: ModelBundle) -> dict[str, Any]:
    path = bundle.path
    manifest, _ = Catalog._manifest_file(path)
    try:
        profile = contract_profile(manifest.get("model_version"))
    except ValueError:
        raise StorageError("Unsupported analytical model version.", code="corrupt_model") from None
    if (
        canonical_json(manifest) != canonical_json(bundle.manifest)
        or manifest.get("model_id") != bundle.id
        or re.fullmatch(r"[0-9a-f]{32}", bundle.id) is None
        or re.fullmatch(r"[0-9a-f]{32}", str(manifest.get("source_snapshot_id"))) is None
        or set(manifest.get("datasets", {})) != set(DATASETS)
        or manifest.get("metrics") != profile["metrics"]
    ):
        raise StorageError("Model manifest contract is invalid.", code="corrupt_model")
    results: dict[str, Any] = {}
    con = duckdb.connect(config={"threads": 1, "memory_limit": "256MB"})
    try:
        for dataset_id, spec in DATASETS.items():
            info = manifest["datasets"][dataset_id]
            if info.get("file") != f"{dataset_id}.parquet":
                raise StorageError("Model filename contract is invalid.", code="corrupt_model")
            source = path / info["file"]
            if (
                source.is_symlink()
                or not source.is_file()
                or source.resolve().parent != path.resolve()
            ):
                raise StorageError("Model file is missing or unsafe.", code="corrupt_model")
            with source.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            if checksum != info.get("sha256"):
                raise StorageError("Model file checksum differs.", code="corrupt_model")
            register_views(con, bundle, [dataset_id])
            table = identifier(dataset_id)
            schema = [[row[0], row[1]] for row in con.execute(f"DESCRIBE {table}").fetchall()]
            if schema != [list(column) for column in spec.columns] or schema != info.get("schema"):
                raise StorageError(
                    "Model schema requires an explicit migration.", code="corrupt_model"
                )
            keys = ",".join(identifier(column) for column in spec.primary_key)
            nulls = " OR ".join(f"{identifier(column)} IS NULL" for column in spec.primary_key)
            count, distinct, missing = con.execute(
                f"SELECT count(*),count(DISTINCT ({keys})),count(*) FILTER(WHERE {nulls}) FROM {table}"
            ).fetchone()
            if (
                type(info.get("rows")) is not int
                or count != info["rows"]
                or count != distinct
                or missing
            ):
                raise StorageError("Model row counts or primary keys differ.", code="corrupt_model")
            if spec.catalog_definition()["primary_key"] != info.get("primary_key"):
                raise StorageError("Model key declaration differs.", code="corrupt_model")
            if info.get("foreign_keys") != list(spec.foreign_keys):
                raise StorageError("Model relationship declaration differs.", code="corrupt_model")
            definition = profile["datasets"][dataset_id]
            for field in ("classification", "units", "allowed_personas", "description"):
                if info.get(field) != definition[field]:
                    raise StorageError("Model catalog semantics differ.", code="corrupt_model")
            if info.get("logical_sha256") != logical_digest(con, dataset_id, spec.primary_key):
                raise StorageError("Model logical fingerprint differs.", code="corrupt_model")
            results[dataset_id] = {
                "rows": count,
                "sha256": checksum,
                "logical_sha256": info["logical_sha256"],
            }
        fk_count = 0
        for dataset_id, spec in DATASETS.items():
            for fk in spec.foreign_keys:
                join = " AND ".join(
                    f"c.{identifier(a)}=p.{identifier(b)}"
                    for a, b in zip(fk["columns"], fk["references"], strict=True)
                )
                present = " AND ".join(
                    f"c.{identifier(column)} IS NOT NULL" for column in fk["columns"]
                )
                count = con.execute(
                    f"SELECT count(*) FROM {identifier(dataset_id)} c "
                    f"LEFT JOIN {identifier(fk['dataset'])} p ON {join} "
                    f"WHERE {present} AND p.{identifier(fk['references'][0])} IS NULL"
                ).fetchone()[0]
                if count:
                    raise StorageError("Model foreign-key validation failed.", code="corrupt_model")
                fk_count += 1
        source_bindings = manifest.get("source_files", {})
        for spec in ROUTES:
            info = source_bindings.get(spec.name, {})
            if (
                results[f"{spec.name}_daily"]["rows"] != info.get("rows")
                or re.fullmatch(r"[0-9a-f]{64}", str(info.get("sha256"))) is None
            ):
                raise StorageError("Model source row-count binding differs.", code="corrupt_model")
        # Recompute the official metric independently of the builder's helper.
        rows = con.execute(
            "SELECT n.capacity_mw,n.outage_mw,f.capacity_mw,f.outage_mw,f.offline_share,f.offline_pct "
            "FROM national_daily n FULL JOIN fleet_daily f USING(period) ORDER BY period"
        ).fetchall()
        if (
            len(rows) != results["national_daily"]["rows"]
            or len(rows) != results["fleet_daily"]["rows"]
        ):
            raise StorageError(
                "Fleet metric must cover every national observation.", code="corrupt_model"
            )
        with localcontext() as context:
            context.prec = 28
            context.rounding = ROUND_HALF_UP
            for capacity, outage, f_capacity, f_outage, share, percent in rows:
                expected_share = expected_percent = None
                if capacity is not None and capacity > 0 and outage is not None:
                    expected_share = (outage / capacity).quantize(Decimal("0.000000000001"))
                    expected_percent = (outage * Decimal(100) / capacity).quantize(
                        Decimal("0.000000001")
                    )
                if (
                    (capacity, outage) != (f_capacity, f_outage)
                    or share != expected_share
                    or percent != expected_percent
                ):
                    raise StorageError("Fleet metric verification failed.", code="corrupt_model")
    except (duckdb.Error, OSError, TypeError, ValueError, KeyError) as error:
        raise StorageError("Analytical model verification failed.", code="corrupt_model") from error
    finally:
        con.close()
    return {
        "verified": True,
        "model_id": bundle.id,
        "source_snapshot_id": manifest["source_snapshot_id"],
        "foreign_keys_checked": fk_count,
        "datasets": results,
    }


def prepare_model(
    catalog: Catalog, source: SnapshotRef, *, model_id: str | None = None
) -> ModelBundle:
    """Caller holds writer lock. Build and close files before any publication."""
    model_id = model_id or uuid.uuid4().hex
    if re.fullmatch(r"[0-9a-f]{32}", model_id) is None:
        raise StorageError("Invalid model identifier.", code="invalid_identifier")
    catalog.start_model_build(model_id, source.id)
    LOG.info("model_build_started", extra={"run_id": model_id})
    try:
        sources = {
            spec.name: source.path / source.manifest["routes"][spec.name]["file"] for spec in ROUTES
        }
        stage = catalog.root / ".model-staging" / model_id
        built = build_model(sources, source.id, stage, model_id=model_id)
        verify_model(built)
        destination = catalog.root / "models" / model_id
        os.replace(stage, destination)
        if os.name != "nt":
            handle = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(handle)
            finally:
                os.close(handle)
        LOG.info("model_build_validated", extra={"run_id": model_id})
        return ModelBundle(model_id, destination, built.manifest)
    except BaseException as error:
        try:
            catalog.fail_model_build(
                model_id,
                "Model construction failed before publication.",
                interrupted=isinstance(error, KeyboardInterrupt),
            )
        except Exception:
            LOG.error("model_status_write_failed", extra={"run_id": model_id})
        if isinstance(error, (KeyboardInterrupt, StorageError, ModelError)):
            raise
        raise ModelError("Model construction failed before publication.") from None


def rebuild_model(root: Path) -> ModelBundle:
    with writer_lock(root), Catalog(root) as catalog:
        catalog.recover_interrupted()
        source = catalog.current()
        if source is None:
            raise StorageError("Ingest source data before building the model.", code="no_snapshot")
        catalog.verify(source.id)
        built = prepare_model(catalog, source)
        try:
            catalog.publish_model(built)
        except BaseException:
            catalog.fail_model_build(built.id, "Model publication failed.")
            raise
        return built


def resolve_model(root: Path, model_id: str | None = None) -> ModelBundle:
    with Catalog(root) as catalog:
        bundle = catalog.model(model_id) if model_id else catalog.current_model()
        if bundle is None:
            raise StorageError("Build a model for the current source first.", code="no_model")
        return bundle


def _allowed(persona: str) -> set[str]:
    normalized = persona.lower()
    if normalized not in {"viewer", "analyst", "admin"}:
        raise StorageError("Unknown persona.", code="unknown_persona")
    return {
        key
        for key, spec in DATASETS.items()
        if normalized != "viewer" or spec.classification == "national"
    }


def analytical_catalog(root: Path, persona: str, *, model_id: str | None = None) -> dict[str, Any]:
    permitted = _allowed(persona)
    bundle = resolve_model(root, model_id)
    profile = contract_profile(bundle.manifest["model_version"])
    datasets = []
    for key in DATASETS:
        if key in permitted:
            info = bundle.manifest["datasets"][key]
            entry = dict(profile["datasets"][key])
            entry["foreign_keys"] = [
                fk for fk in entry["foreign_keys"] if fk["dataset"] in permitted
            ]
            entry.update(
                {
                    "rows": info["rows"],
                    "min_period": info.get("min_period"),
                    "max_period": info.get("max_period"),
                }
            )
            datasets.append(entry)
    return {
        "model_id": bundle.id,
        "source_snapshot_id": bundle.manifest["source_snapshot_id"],
        "model_version": bundle.manifest["model_version"],
        "datasets": datasets,
        "metrics": {
            key: value for key, value in profile["metrics"].items() if value["dataset"] in permitted
        },
        "warnings": [profile["warning"]] if "warning" in profile else [],
    }


@contextmanager
def open_model_connection(
    root: Path, dataset_ids: Sequence[str], *, persona: str = "analyst", model_id: str | None = None
) -> Iterator[tuple[duckdb.DuckDBPyConnection, ModelBundle]]:
    permitted = _allowed(persona)
    if not dataset_ids or any(key not in permitted for key in dataset_ids):
        raise StorageError("Dataset access is not permitted.", code="dataset_forbidden")
    bundle = resolve_model(root, model_id)
    con = duckdb.connect(config={"threads": 1, "memory_limit": "256MB"})
    try:
        register_views(con, bundle, tuple(dict.fromkeys(dataset_ids)))
        yield con, bundle
    finally:
        con.close()
