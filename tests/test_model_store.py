"""Integrity and least-data catalog tests; these adapters are not a SQL sandbox."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy

import duckdb
import pytest

from outage_explorer import model_store
from outage_explorer.domain import canonical_json
from outage_explorer.model_contracts import DATASETS, MODEL_VERSION, contract_profile
from outage_explorer.model_store import (
    analytical_catalog,
    open_model_connection,
    prepare_model,
    rebuild_model,
    verify_model,
)
from outage_explorer.modeling import ModelBundle, logical_digest
from outage_explorer.pipeline import IngestConfig, ingest
from outage_explorer.storage import Catalog, StorageError, writer_lock
from tests.helpers import FakeClient
from tests.test_modeling import model


def seed(root):
    ingest(IngestConfig(root, "full"), FakeClient())
    with Catalog(root) as catalog:
        current = catalog.current_model()
    return current or rebuild_model(root)


def rewrite_manifest(bundle, manifest):
    (bundle.path / "manifest.json").write_text(
        canonical_json(manifest) + "\n", encoding="utf-8", newline="\n"
    )
    return ModelBundle(bundle.id, bundle.path, manifest)


def test_viewer_catalog_contains_only_national_metadata_and_metric(tmp_path):
    published = seed(tmp_path)
    result = analytical_catalog(tmp_path, "Viewer")
    assert result["model_id"] == published.id
    assert {entry["id"] for entry in result["datasets"]} == {"national_daily", "fleet_daily"}
    assert all(entry["rows"] == 3 for entry in result["datasets"])
    serialized = json.dumps(result)
    for hidden in set(DATASETS) - {"national_daily", "fleet_daily"}:
        assert hidden not in serialized
    assert "facility_id" not in serialized and "generator_id" not in serialized
    assert ".parquet" not in serialized and str(tmp_path) not in serialized
    assert result["metrics"]["fleet_offline_share"]["source_dataset"] == "national_daily"


@pytest.mark.parametrize("persona", ["Analyst", "Admin"])
def test_analytical_catalog_exposes_all_model_contracts_to_detail_personas(tmp_path, persona):
    seed(tmp_path)
    result = analytical_catalog(tmp_path, persona)
    assert {entry["id"] for entry in result["datasets"]} == set(DATASETS)
    assert all("file" not in entry and "path" not in entry for entry in result["datasets"])


def test_unknown_persona_is_rejected_before_model_resolution(tmp_path, monkeypatch):
    def no_reads(*args, **kwargs):
        pytest.fail("Persona must be checked before resolving the model.")

    monkeypatch.setattr(model_store, "resolve_model", no_reads)
    with pytest.raises(StorageError) as error:
        analytical_catalog(tmp_path, "operator")
    assert error.value.code == "unknown_persona"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "selected", [["generator_daily"], ["fleet_daily", "unknown"], ["../generator.parquet"], []]
)
def test_invalid_or_unauthorized_selection_is_rejected_before_any_views(
    tmp_path, monkeypatch, selected
):
    def no_reads(*args, **kwargs):
        pytest.fail("Dataset permission must be checked before storage or views.")

    monkeypatch.setattr(model_store, "resolve_model", no_reads)
    monkeypatch.setattr(model_store, "register_views", no_reads)
    with (
        pytest.raises(StorageError) as error,
        open_model_connection(tmp_path, selected, persona="viewer"),
    ):
        pytest.fail("An invalid selection must not yield a connection.")
    assert error.value.code == "dataset_forbidden"


def test_connection_registers_only_explicitly_selected_authorized_datasets(tmp_path):
    published = seed(tmp_path)
    with open_model_connection(tmp_path, ["fleet_daily", "fleet_daily"], persona="viewer") as (
        con,
        bundle,
    ):
        assert bundle.id == published.id
        assert con.execute("SHOW TABLES").fetchall() == [("fleet_daily",)]
        assert con.execute("SELECT count(*) FROM fleet_daily").fetchone()[0] == 3
        with pytest.raises(duckdb.CatalogException):
            con.execute("SELECT * FROM generator_daily")


def test_file_and_manifest_tampering_are_rejected(tmp_path):
    bundle = model(tmp_path)
    original = (bundle.path / "manifest.json").read_bytes()
    (bundle.path / "manifest.json").write_bytes(original + b" ")
    # Whitespace alone is semantically harmless for a standalone bundle;
    # changing a semantic value must disagree with the supplied descriptor.
    altered = deepcopy(bundle.manifest)
    altered["model_id"] = "d" * 32
    rewrite_manifest(bundle, altered)
    with pytest.raises(StorageError) as error:
        verify_model(bundle)
    assert error.value.code == "corrupt_model"
    (bundle.path / "manifest.json").write_bytes(original)
    with (bundle.path / "generator_daily.parquet").open("ab") as stream:
        stream.write(b"unpublished-tamper")
    with pytest.raises(StorageError, match="checksum"):
        verify_model(bundle)


@pytest.mark.parametrize(
    "change", ["classification", "personas", "metrics", "units", "description", "source_rows"]
)
def test_coherently_rewritten_manifest_still_must_match_public_contract(tmp_path, change):
    bundle = model(tmp_path)
    altered = deepcopy(bundle.manifest)
    if change == "classification":
        altered["datasets"]["generator_daily"]["classification"] = "national"
    elif change == "personas":
        altered["datasets"]["generator_daily"]["allowed_personas"] = ["Viewer", "Analyst", "Admin"]
    elif change == "metrics":
        altered["metrics"]["fleet_offline_share"]["source_dataset"] = "generator_daily"
    elif change == "units":
        altered["datasets"]["fleet_daily"]["units"]["offline_share"] = "MW"
    elif change == "description":
        altered["datasets"]["generator_daily"]["description"] = "Misleading contract."
    else:
        altered["source_files"]["generator"]["rows"] += 1
    changed = rewrite_manifest(bundle, altered)
    with pytest.raises(StorageError) as error:
        verify_model(changed)
    assert error.value.code == "corrupt_model"


def test_metric_is_independently_checked_after_consistent_checksums_are_forged(tmp_path):
    bundle = model(tmp_path)
    target = bundle.path / "fleet_daily.parquet"
    replacement = bundle.path / "replacement.parquet"
    with duckdb.connect(config={"threads": 1}) as con:
        con.execute("CREATE TABLE forged AS SELECT * FROM read_parquet(?)", [str(target)])
        con.execute("UPDATE forged SET offline_share=0.9")
        con.execute("COPY forged TO ? (FORMAT PARQUET)", [str(replacement)])
        logical = logical_digest(con, "forged", ("period",))
    os.replace(replacement, target)
    altered = deepcopy(bundle.manifest)
    altered["datasets"]["fleet_daily"]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    altered["datasets"]["fleet_daily"]["logical_sha256"] = logical
    changed = rewrite_manifest(bundle, altered)
    with pytest.raises(StorageError, match="[Mm]etric"):
        verify_model(changed)


def test_catalog_bound_manifest_bytes_cannot_change_after_publication(tmp_path):
    bundle = seed(tmp_path)
    path = bundle.path / "manifest.json"
    path.write_bytes(path.read_bytes() + b" ")
    with Catalog(tmp_path) as catalog, pytest.raises(StorageError, match="integrity"):
        catalog.current_model()


def test_selected_file_checksum_is_checked_before_query_connection_is_yielded(tmp_path):
    bundle = seed(tmp_path)
    with (bundle.path / "fleet_daily.parquet").open("ab") as stream:
        stream.write(b"tampered-selected-data")
    with (
        pytest.raises(StorageError, match="checksum"),
        open_model_connection(tmp_path, ["fleet_daily"], persona="viewer"),
    ):
        pytest.fail("A changed selected dataset must not yield a query connection.")


def test_viewer_connection_does_not_read_unselected_detail_files(tmp_path):
    bundle = seed(tmp_path)
    (bundle.path / "generator_daily.parquet").write_bytes(b"unreadable detail data")
    with open_model_connection(tmp_path, ["fleet_daily"], persona="viewer") as (con, _):
        assert con.execute("SELECT count(*) FROM fleet_daily").fetchone()[0] == 3


@pytest.mark.parametrize("version", [MODEL_VERSION + 1, True, str(MODEL_VERSION)])
def test_unknown_or_mistyped_model_version_is_rejected(tmp_path, version):
    bundle = model(tmp_path)
    altered = deepcopy(bundle.manifest)
    altered["model_version"] = version
    changed = rewrite_manifest(bundle, altered)
    with pytest.raises(StorageError, match="version"):
        verify_model(changed)


def test_legacy_contract_is_frozen_and_its_warning_survives_a_new_current_model(tmp_path):
    seed(tmp_path)
    with writer_lock(tmp_path), Catalog(tmp_path) as catalog:
        prepared = prepare_model(catalog, catalog.current())
        historical = deepcopy(prepared.manifest)
        profile = contract_profile(1)
        historical["model_version"] = 1
        historical["metrics"] = deepcopy(profile["metrics"])
        for dataset, definition in profile["datasets"].items():
            for field in ("classification", "allowed_personas", "description", "units"):
                historical["datasets"][dataset][field] = deepcopy(definition[field])
        legacy = rewrite_manifest(prepared, historical)
        assert verify_model(legacy)["verified"]
        catalog.publish_model(legacy)
    current = rebuild_model(tmp_path)
    assert current.manifest["model_version"] == MODEL_VERSION
    visible = analytical_catalog(tmp_path, "viewer", model_id=legacy.id)
    assert visible["model_version"] == 1 and visible["warnings"]
    fleet = next(entry for entry in visible["datasets"] if entry["id"] == "fleet_daily")
    assert fleet["units"]["offline_pct"] == "percentage points"
    assert analytical_catalog(tmp_path, "viewer")["warnings"] == []
    # Historical descriptors must match the frozen v1 profile, even when the
    # replacement would happen to equal the current metric documentation.
    rewritten = deepcopy(legacy.manifest)
    rewritten["metrics"] = deepcopy(contract_profile(MODEL_VERSION)["metrics"])
    with pytest.raises(StorageError, match="contract"):
        verify_model(rewrite_manifest(legacy, rewritten))
