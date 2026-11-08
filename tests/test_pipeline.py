from __future__ import annotations

import json
from copy import deepcopy
from datetime import date

import duckdb
import pytest

from outage_explorer.data import pipeline
from outage_explorer.data.domain import ROUTES, DataValidationError
from outage_explorer.data.eia import EIAClientError
from outage_explorer.data.pipeline import IngestConfig, ingest
from outage_explorer.data.storage import Catalog, StorageError
from tests.helpers import SECRET, FakeClient, source_rows


def config(root, **changes):
    return IngestConfig(data_dir=root, mode="full", page_size=2, **changes)


def test_full_run_publishes_all_routes_as_typed_verified_parquet(tmp_path):
    client = FakeClient()
    manifest = ingest(config(tmp_path, workers=3), client)
    assert [manifest["routes"][spec.name]["rows"] for spec in ROUTES] == [3, 6, 12]
    assert client.pages == 11
    assert [manifest["routes"][spec.name]["added"] for spec in ROUTES] == [3, 6, 12]
    with Catalog(tmp_path) as catalog:
        assert catalog.current().id == manifest["snapshot_id"]
        assert catalog.verify()["verified"]
        assert catalog.history()[0]["status"] == "succeeded"
    with duckdb.connect(config={"threads": 1}) as con:
        path = tmp_path / "snapshots" / manifest["snapshot_id"] / "generator.parquet"
        assert con.execute(
            "SELECT DISTINCT facility FROM read_parquet(?) ORDER BY facility", [str(path)]
        ).fetchall() == [("001",), ("002",)]


def test_rerunning_same_source_changes_no_logical_rows(tmp_path):
    first = ingest(config(tmp_path), FakeClient())
    second = ingest(config(tmp_path), FakeClient())
    assert first["snapshot_id"] != second["snapshot_id"]
    assert second["parent_snapshot_id"] == first["snapshot_id"]
    for name in first["routes"]:
        assert second["routes"][name]["logical_sha256"] == first["routes"][name]["logical_sha256"]
        assert (
            second["routes"][name]["added"],
            second["routes"][name]["changed"],
            second["routes"][name]["deleted"],
        ) == (0, 0, 0)


def test_recent_uses_each_stored_watermark_and_replaces_deletions(tmp_path):
    ingest(config(tmp_path), FakeClient())
    rows = source_rows()
    rows[ROUTES[2].route].pop(0)
    rows[ROUTES[0].route][0]["outage"] = "5"
    client = FakeClient(rows)
    manifest = ingest(IngestConfig(tmp_path, "recent", page_size=2), client)
    assert manifest["routes"]["generator"]["deleted"] == 1
    assert manifest["routes"]["generator"]["deletions_confirmed"]
    assert manifest["routes"]["national"]["changed"] == 1
    assert all(start == date(2026, 9, 1) for _, start, _ in client.calls)
    assert sum(route == ROUTES[2].route for route, _, _ in client.calls) == 2


def test_recent_first_run_backfills_from_metadata(tmp_path):
    manifest = ingest(IngestConfig(tmp_path, "recent"), FakeClient())
    assert manifest["routes"]["generator"]["rows"] == 12


def test_bounded_full_preserves_data_outside_replaced_interval(tmp_path):
    ingest(config(tmp_path), FakeClient())
    rows = source_rows()
    rows[ROUTES[0].route][1]["outage"] = "0"
    manifest = ingest(
        config(tmp_path, start=date(2026, 9, 2), end=date(2026, 9, 2)), FakeClient(rows)
    )
    assert manifest["routes"]["national"]["rows"] == 3
    assert manifest["routes"]["national"]["changed"] == 1


@pytest.mark.parametrize("failure", ["network", "bad_data", "interrupted"])
def test_route_failure_never_replaces_current_snapshot(tmp_path, failure):
    baseline = ingest(config(tmp_path), FakeClient())
    client = FakeClient()
    expected = EIAClientError
    if failure == "bad_data":
        del client.rows[ROUTES[2].route][1]["capacity"]
        expected = DataValidationError
    else:
        client.fail_route = ROUTES[2].route
        client.fail_after_pages = 1
        if failure == "interrupted":
            client.raised_error = KeyboardInterrupt()
            expected = KeyboardInterrupt
    with pytest.raises(expected):
        ingest(config(tmp_path), client)
    with Catalog(tmp_path) as catalog:
        assert catalog.current().id == baseline["snapshot_id"]
        assert catalog.verify()["verified"]
        status = "interrupted" if failure == "interrupted" else "failed"
        assert catalog.history()[0]["status"] == status


def test_conflicting_duplicate_fails_but_identical_collapses(tmp_path):
    rows = source_rows()
    rows[ROUTES[0].route].append(deepcopy(rows[ROUTES[0].route][0]))
    manifest = ingest(config(tmp_path), FakeClient(rows))
    assert manifest["routes"]["national"]["rows"] == 3
    assert manifest["routes"]["national"]["quality"]["identical_duplicates_collapsed"] == 1
    rows[ROUTES[0].route][-1]["outage"] = "99"
    with pytest.raises(DataValidationError, match="conflicting duplicate"):
        ingest(config(tmp_path), FakeClient(rows))


def test_source_additions_nulls_and_outliers_are_preserved_and_flagged(tmp_path):
    rows = source_rows()
    row = rows[ROUTES[0].route][0]
    row.update({"capacity": None, "outage": "-1", "new_measure": {"api_key": SECRET}})
    manifest = ingest(config(tmp_path, record_responses=True), FakeClient(rows))
    info = manifest["routes"]["national"]
    assert "new_measure" in info["extra_fields"]
    assert info["quality"]["flags"] == {"null_capacity": 1, "outage_negative": 1}
    for path in tmp_path.rglob("*.json"):
        assert SECRET not in path.read_text(encoding="utf-8")
    with duckdb.connect(config={"threads": 1}) as con:
        path = tmp_path / "snapshots" / manifest["snapshot_id"] / "national.parquet"
        raw = con.execute(
            "SELECT _source_row FROM read_parquet(?) ORDER BY period LIMIT 1", [str(path)]
        ).fetchone()[0]
        assert SECRET not in raw
        assert json.loads(raw)["new_measure"]["api_key"] == "[REDACTED]"


def test_parent_corruption_is_rejected_before_new_source_requests(tmp_path):
    baseline = ingest(config(tmp_path), FakeClient())
    path = tmp_path / "snapshots" / baseline["snapshot_id"] / "national.parquet"
    with path.open("ab") as stream:
        stream.write(b"tamper")
    client = FakeClient()
    with pytest.raises(StorageError, match="integrity"):
        ingest(config(tmp_path), client)
    assert client.requests == 0


def test_deletion_confirmation_must_match_first_extraction(tmp_path):
    ingest(config(tmp_path), FakeClient())
    rows = source_rows()
    rows[ROUTES[2].route].pop(0)
    client = FakeClient(rows)
    client.confirmation_mutation = lambda route, data: data[:-1]
    with pytest.raises(DataValidationError, match="confirmation changed"):
        ingest(IngestConfig(tmp_path, "recent"), client)


def test_failure_reporting_io_does_not_mask_original_error(tmp_path, monkeypatch):
    client = FakeClient()
    client.fail_route = ROUTES[0].route
    original = pipeline.write_json

    def full_disk(path, value):
        if path.name == "failure.json":
            raise OSError("simulated full disk")
        return original(path, value)

    monkeypatch.setattr(pipeline, "write_json", full_disk)
    with pytest.raises(EIAClientError, match="Simulated network"):
        ingest(config(tmp_path), client)
    with Catalog(tmp_path) as catalog:
        assert catalog.history()[0]["status"] == "failed"


def test_missing_requested_boundary_dates_are_reported(tmp_path):
    rows = source_rows()
    # Keep metadata bounds via other rows but omit an entire interior day.
    rows[ROUTES[1].route] = [row for row in rows[ROUTES[1].route] if row["period"] != "2026-09-02"]
    manifest = ingest(config(tmp_path), FakeClient(rows))
    assert manifest["routes"]["facility"]["quality"]["missing_requested_days"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"page_size": 0},
        {"page_size": 5001},
        {"workers": 4},
        {"mode": "unknown"},
        {"mode": "recent", "start": date(2026, 1, 1)},
        {"start": date(2026, 9, 3), "end": date(2026, 9, 1)},
    ],
)
def test_invalid_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        IngestConfig(**changes)
