"""Analytical and raw publication are one atomic version transition."""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest

from outage_explorer.analytics import model_store
from outage_explorer.analytics.model_store import (
    open_model_connection,
    prepare_model,
    rebuild_model,
    verify_model,
)
from outage_explorer.analytics.modeling import ModelError
from outage_explorer.data.domain import ROUTES, validate_row
from outage_explorer.data.pipeline import IngestConfig, ingest
from outage_explorer.data.storage import Catalog, StorageError, writer_lock
from tests.helpers import FakeClient, source_rows
from tests.test_model_store import rewrite_manifest, seed
from tests.test_storage import make_snapshot


def test_repeated_build_preserves_logical_data_and_historical_models(tmp_path):
    first = seed(tmp_path)
    second = rebuild_model(tmp_path)
    assert first.id != second.id
    assert first.manifest["source_snapshot_id"] == second.manifest["source_snapshot_id"]
    for dataset in first.manifest["datasets"]:
        assert (
            first.manifest["datasets"][dataset]["logical_sha256"]
            == second.manifest["datasets"][dataset]["logical_sha256"]
        )
    with Catalog(tmp_path) as catalog:
        assert catalog.current_model().id == second.id
        assert catalog.model(first.id).id == first.id
        assert [entry["status"] for entry in catalog.model_history()] == ["succeeded", "succeeded"]
    assert verify_model(first)["verified"]


def test_new_ingest_publishes_matching_model_and_keeps_old_model_queryable(tmp_path):
    first = seed(tmp_path)
    source_before = first.manifest["source_snapshot_id"]
    fingerprints = {p.name: p.read_bytes() for p in first.path.glob("*.parquet")}
    rows = source_rows()
    # FakeClient data uses EIA route identifiers rather than short model names.
    rows["us-nuclear-outages"][0]["outage"] = "0"
    ingest(IngestConfig(tmp_path, "recent"), FakeClient(rows))
    with Catalog(tmp_path) as catalog:
        second = catalog.current_model()
        assert second is not None
        assert second.id != first.id
        assert second.manifest["source_snapshot_id"] == catalog.current().id != source_before
    with open_model_connection(tmp_path, ["fleet_daily"], model_id=first.id) as (con, historical):
        assert historical.id == first.id
        assert con.execute(
            "SELECT offline_share FROM fleet_daily ORDER BY period LIMIT 1"
        ).fetchone()[0] == Decimal("0.100000000000")
    with open_model_connection(tmp_path, ["fleet_daily"]) as (con, _):
        assert con.execute(
            "SELECT offline_share FROM fleet_daily ORDER BY period LIMIT 1"
        ).fetchone()[0] == Decimal(0)
    assert all((first.path / name).read_bytes() == value for name, value in fingerprints.items())


def test_model_construction_failure_preserves_both_publication_pointers(tmp_path, monkeypatch):
    before = seed(tmp_path)

    def fail_build(*args, **kwargs):
        raise ModelError("Injected analytical contract failure.")

    monkeypatch.setattr(model_store, "build_model", fail_build)
    with pytest.raises(ModelError, match="Injected"):
        ingest(IngestConfig(tmp_path, "recent"), FakeClient())
    with Catalog(tmp_path) as catalog:
        assert catalog.current_model().id == before.id
        assert catalog.current().id == before.manifest["source_snapshot_id"]
        assert catalog.history()[0]["status"] == "failed"
        assert catalog.model_history()[0]["status"] == "failed"


@pytest.mark.parametrize(
    "outage",
    [
        pytest.param("10000", id="derived-share-exceeds-storage-envelope"),
        pytest.param("100000000000000", id="ratio-exceeds-decimal-quantize-envelope"),
    ],
)
def test_derived_numeric_overflow_preserves_both_published_versions(tmp_path, outage):
    before = seed(tmp_path)
    source_before = before.manifest["source_snapshot_id"]
    rows = source_rows()
    candidate = rows["us-nuclear-outages"][0]
    candidate.update(capacity="0.000000001", outage=outage, percentOutage="100")
    # Both finite operands fit the raw DECIMAL(24,9) contract. Their ratio
    # exceeds a derived numeric envelope; this is a model failure, not raw
    # rejection or permission to clip the observation into a valid-looking ratio.
    national = next(spec for spec in ROUTES if spec.name == "national")
    admitted = validate_row(national, candidate, date(2026, 9, 1), date(2026, 9, 3))
    assert "outage_above_capacity" in admitted.flags
    with Catalog(tmp_path) as catalog:
        raw_count = catalog._db.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        model_count = catalog._db.execute("SELECT count(*) FROM model_snapshots").fetchone()[0]

    with pytest.raises(ModelError) as error:
        ingest(IngestConfig(tmp_path, "recent"), FakeClient(rows))

    assert error.value.code == "invalid_model"
    with Catalog(tmp_path) as catalog:
        assert catalog.current().id == source_before
        assert catalog.current_model().id == before.id
        assert catalog.snapshot(source_before).id == source_before
        assert catalog.model(before.id).manifest == before.manifest
        assert catalog._db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == raw_count
        assert (
            catalog._db.execute("SELECT count(*) FROM model_snapshots").fetchone()[0] == model_count
        )
        raw_history = catalog.history()
        assert raw_history[0]["status"] == "failed"
        assert raw_history[0]["error_code"] == "invalid_model"
        assert raw_history[1]["status"] == "succeeded"
        assert raw_history[1]["snapshot_id"] == source_before
        model_history = catalog.model_history()
        assert model_history[0]["status"] == "failed"
        assert model_history[1]["status"] == "succeeded"
        assert model_history[1]["model_id"] == before.id
    assert verify_model(before)["verified"]


def test_sqlite_model_insert_abort_rolls_back_raw_and_model_together(tmp_path):
    before = seed(tmp_path)
    with Catalog(tmp_path) as catalog:
        raw_count = catalog._db.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        model_count = catalog._db.execute("SELECT count(*) FROM model_snapshots").fetchone()[0]
        catalog._db.execute(
            "CREATE TRIGGER injected_model_abort BEFORE INSERT ON model_snapshots "
            "BEGIN SELECT RAISE(ABORT, 'injected publication failure'); END"
        )
        catalog._db.commit()
    with pytest.raises(StorageError) as error:
        ingest(IngestConfig(tmp_path, "recent"), FakeClient())
    assert error.value.code == "publication_failed"
    with Catalog(tmp_path) as catalog:
        assert catalog.current().id == before.manifest["source_snapshot_id"]
        assert catalog.current_model().id == before.id
        assert catalog._db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == raw_count
        assert (
            catalog._db.execute("SELECT count(*) FROM model_snapshots").fetchone()[0] == model_count
        )
        assert catalog.history()[0]["status"] == "failed"
    assert verify_model(before)["verified"]


@pytest.mark.parametrize("changed", ["sha256", "rows"])
def test_model_source_binding_drift_prevents_publication(tmp_path, changed):
    before = seed(tmp_path)
    with writer_lock(tmp_path), Catalog(tmp_path) as catalog:
        candidate = prepare_model(catalog, catalog.current())
        altered = deepcopy(candidate.manifest)
        if changed == "sha256":
            altered["source_files"]["generator"]["sha256"] = "0" * 64
        else:
            altered["source_files"]["generator"]["rows"] += 1
        candidate = rewrite_manifest(candidate, altered)
        with pytest.raises(StorageError) as error:
            catalog.publish_model(candidate)
        assert error.value.code == "corrupt_model"
        assert catalog.current_model().id == before.id


def test_standalone_model_prepared_for_old_source_cannot_replace_new_current_model(tmp_path):
    before = seed(tmp_path)
    with writer_lock(tmp_path), Catalog(tmp_path) as catalog:
        stale = prepare_model(catalog, catalog.current())
    ingest(IngestConfig(tmp_path, "recent"), FakeClient())
    with writer_lock(tmp_path), Catalog(tmp_path) as catalog:
        latest = catalog.current_model()
        with pytest.raises(StorageError) as error:
            catalog.publish_model(stale)
        assert error.value.code == "stale_model"
        assert catalog.current_model().id == latest.id != before.id


def test_legacy_raw_only_publication_clears_current_model_instead_of_serving_stale_data(tmp_path):
    before = seed(tmp_path)
    with writer_lock(tmp_path), Catalog(tmp_path) as catalog:
        run_id = uuid.uuid4().hex
        catalog.start_run(run_id, "full", "2026-09-01", "2026-09-02")
        path, manifest = make_snapshot(catalog, run_id, parent=catalog.current().id)
        catalog.publish(run_id, path, manifest)
        assert catalog.current().id == run_id
        assert catalog.current_model() is None
        assert catalog.model(before.id).id == before.id
    with open_model_connection(tmp_path, ["fleet_daily"], model_id=before.id) as (con, _):
        assert con.execute("SELECT count(*) FROM fleet_daily").fetchone()[0] == 3


@pytest.mark.parametrize("state", ["failed", "missing", "different_source"])
def test_publication_requires_a_matching_active_build(tmp_path, state):
    before = seed(tmp_path)
    with writer_lock(tmp_path), Catalog(tmp_path) as catalog:
        candidate = prepare_model(catalog, catalog.current())
        if state == "failed":
            catalog.fail_model_build(candidate.id, "Build was cancelled before publication.")
        elif state == "missing":
            catalog._db.execute("DELETE FROM model_builds WHERE model_id=?", (candidate.id,))
            catalog._db.commit()
        else:
            catalog._db.execute(
                "UPDATE model_builds SET source_snapshot_id=? WHERE model_id=?",
                ("0" * 32, candidate.id),
            )
            catalog._db.commit()
        with pytest.raises(StorageError) as error:
            catalog.publish_model(candidate)
        assert error.value.code == "invalid_publication"
        assert catalog.current_model().id == before.id
        assert catalog.current().id == before.manifest["source_snapshot_id"]
