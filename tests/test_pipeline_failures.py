"""Adversarial integration coverage: failures never become published success."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from outage_explorer import pipeline
from outage_explorer.domain import ROUTES, UNITS, DataValidationError
from outage_explorer.eia import EIAClientError, RouteMetadata
from outage_explorer.pipeline import IngestConfig, ingest
from outage_explorer.storage import Catalog, StorageError, writer_lock
from tests.helpers import SECRET, FakeClient, source_rows


def configuration(root, *, mode="full", **changes):
    return IngestConfig(data_dir=root, mode=mode, page_size=5000, **changes)


class AdvertisedBoundsClient(FakeClient):
    """Keep advertised dates stable even when response rows are missing."""

    def fetch_metadata(self, route):
        self.requests += 1
        spec = next(spec for spec in ROUTES if spec.route == route)
        start, end = date(2026, 9, 1), date(2026, 9, 3)
        raw = {
            "response": {"startPeriod": str(start), "endPeriod": str(end)},
            "request": {"params": {"api_key": SECRET}},
        }
        return RouteMetadata(
            route, start, end, {name: {"units": unit} for name, unit in UNITS.items()},
            raw, ("daily",), spec.key_fields[1:],
        )


def daily_rows(start, ends):
    result = {}
    templates = source_rows()
    for spec in ROUTES:
        daily = [row for row in templates[spec.route] if row["period"] == "2026-09-01"]
        rows = []
        for offset in range((ends[spec.name] - start).days + 1):
            for template in daily:
                row = deepcopy(template)
                row["period"] = str(start + timedelta(days=offset))
                rows.append(row)
        result[spec.route] = rows
    return result


def route_file(root, manifest, name):
    return root / "snapshots" / manifest["snapshot_id"] / f"{name}.parquet"


def read_outage(root, manifest, period):
    with duckdb.connect(config={"threads": 1}) as connection:
        return connection.execute(
            "SELECT outage FROM read_parquet(?) WHERE period=?",
            [str(route_file(root, manifest, "national")), period],
        ).fetchone()[0]


@pytest.mark.parametrize("spec", ROUTES, ids=lambda spec: spec.name)
@pytest.mark.parametrize("missing_period", ["2026-09-01", "2026-09-03"])
def test_source_advertised_boundary_omission_prevents_initial_publication(
    tmp_path, spec, missing_period
):
    rows = source_rows()
    rows[spec.route] = [row for row in rows[spec.route] if row["period"] != missing_period]
    with pytest.raises(DataValidationError, match="source-advertised boundary"):
        ingest(configuration(tmp_path), AdvertisedBoundsClient(rows))
    with Catalog(tmp_path) as catalog:
        assert catalog.current() is None
        assert catalog.history()[0]["status"] == "failed"
        run_id = catalog.history()[0]["run_id"]
    quality = json.loads((tmp_path / "runs" / run_id / "quality.json").read_text("utf-8"))
    assert quality["validation_failed"] is True
    assert quality["error_code"] == "invalid_data"
    assert spec.name in quality["incomplete_routes"]


@pytest.mark.parametrize("spec", ROUTES, ids=lambda spec: spec.name)
def test_empty_route_preserves_current_snapshot(tmp_path, spec):
    baseline = ingest(configuration(tmp_path), FakeClient())
    rows = source_rows()
    rows[spec.route] = []
    with pytest.raises(DataValidationError, match="unexpectedly empty extraction"):
        ingest(configuration(tmp_path), AdvertisedBoundsClient(rows))
    with Catalog(tmp_path) as catalog:
        assert catalog.current().id == baseline["snapshot_id"]
        assert catalog.history()[0]["status"] == "failed"
        assert catalog.verify()["verified"] is True


@pytest.mark.parametrize("boundary", ["rename", "transaction"])
def test_publication_fault_keeps_previous_snapshot_and_failed_quality(
    tmp_path, monkeypatch, boundary
):
    baseline = ingest(configuration(tmp_path), FakeClient())
    if boundary == "rename":
        original_replace = pipeline.os.replace

        def fail_snapshot_rename(source, destination):
            if Path(source).parent == tmp_path / ".staging":
                raise OSError("Simulated filesystem rename failure.")
            return original_replace(source, destination)

        monkeypatch.setattr(pipeline.os, "replace", fail_snapshot_rename)
    else:
        original_publish = Catalog.publish

        def fail_publication_transaction(catalog, run_id, snapshot_dir, manifest):
            catalog._db.execute("""
                CREATE TRIGGER simulated_publication_failure BEFORE UPDATE ON publication
                BEGIN SELECT RAISE(ABORT,'Simulated transaction failure.'); END;
            """)
            return original_publish(catalog, run_id, snapshot_dir, manifest)

        monkeypatch.setattr(Catalog, "publish", fail_publication_transaction)
    with pytest.raises(StorageError) as error:
        ingest(configuration(tmp_path), FakeClient())
    assert error.value.code == ("internal_error" if boundary == "rename" else "publication_failed")
    with Catalog(tmp_path) as catalog:
        assert catalog.current().id == baseline["snapshot_id"]
        assert catalog.verify()["verified"] is True
        latest = catalog.history()[0]
        assert latest["status"] == "failed"
        assert latest["snapshot_id"] is None
        assert catalog._db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
    quality = json.loads(
        (tmp_path / "runs" / latest["run_id"] / "quality.json").read_text("utf-8")
    )
    assert quality["status"] == "failed"
    assert quality["error_code"] == error.value.code


def test_failed_route_records_failure_and_per_run_quality(tmp_path):
    client = FakeClient()
    client.fail_route = ROUTES[2].route
    with pytest.raises(EIAClientError):
        ingest(configuration(tmp_path), client)
    with Catalog(tmp_path) as catalog:
        latest = catalog.history()[0]
        assert latest["status"] == "failed"
    run_path = tmp_path / "runs" / latest["run_id"]
    failure = json.loads((run_path / "failure.json").read_text("utf-8"))
    quality = json.loads((run_path / "quality.json").read_text("utf-8"))
    assert failure["code"] == "network_error"
    assert failure["retryable"] is True
    assert failure["http"]["requests"] > 0
    assert quality["status"] == "failed"
    assert "generator" in quality["incomplete_routes"]
    assert SECRET not in json.dumps({"failure": failure, "quality": quality})


@pytest.mark.parametrize("broken_reporter", ["artifact", "catalog", "both"])
def test_reporting_failures_never_mask_original_source_error(
    tmp_path, monkeypatch, caplog, broken_reporter
):
    client = FakeClient()
    client.fail_route = ROUTES[0].route
    original_error = client.raised_error
    original_write = pipeline.write_json
    if broken_reporter in {"artifact", "both"}:
        def failed_artifact(path, value):
            if path.name in {"failure.json", "quality.json"}:
                raise OSError("Simulated disk exhaustion.")
            return original_write(path, value)

        monkeypatch.setattr(pipeline, "write_json", failed_artifact)
    if broken_reporter in {"catalog", "both"}:
        def failed_status(*args, **kwargs):
            raise OSError("Simulated unavailable catalog.")

        monkeypatch.setattr(Catalog, "fail_run", failed_status)
    with pytest.raises(EIAClientError) as error:
        ingest(configuration(tmp_path), client)
    assert error.value is original_error
    assert error.value.retryable is True
    with writer_lock(tmp_path), Catalog(tmp_path) as catalog:
        assert catalog.current() is None
        if broken_reporter in {"catalog", "both"}:
            assert catalog.history()[0]["status"] == "running"
            assert catalog.recover_interrupted() == 1
            assert catalog.history()[0]["status"] == "interrupted"
        else:
            assert catalog.history()[0]["status"] == "failed"
    if broken_reporter in {"artifact", "both"}:
        assert "failure_artifact_write_failed" in caplog.text
    if broken_reporter in {"catalog", "both"}:
        assert "run_status_write_failed" in caplog.text


def test_recent_bounds_use_each_watermark_minus_29_days_across_month(tmp_path):
    ends = {
        "national": date(2026, 9, 10),
        "facility": date(2026, 9, 8),
        "generator": date(2026, 9, 9),
    }
    baseline = ingest(configuration(tmp_path), FakeClient(daily_rows(date(2026, 8, 1), ends)))
    updated_ends = {name: end + timedelta(days=1) for name, end in ends.items()}
    client = FakeClient(daily_rows(date(2026, 8, 1), updated_ends))
    latest = ingest(configuration(tmp_path, mode="recent"), client)
    assert len(client.calls) == 3
    for spec in ROUTES:
        expected_start = ends[spec.name] - timedelta(days=29)
        assert (spec.route, expected_start, updated_ends[spec.name]) in client.calls
        info = latest["routes"][spec.name]
        assert info["extraction_start"] == str(expected_start)
        assert info["watermark"] == str(updated_ends[spec.name])
        assert info["rows"] > baseline["routes"][spec.name]["rows"]


def test_revision_outside_overlap_waits_for_full_refresh(tmp_path):
    ends = {spec.name: date(2026, 9, 10) for spec in ROUTES}
    original_rows = daily_rows(date(2026, 8, 1), ends)
    baseline = ingest(configuration(tmp_path), FakeClient(original_rows))
    revised_rows = deepcopy(original_rows)
    old_period = date(2026, 8, 2)
    for row in revised_rows[ROUTES[0].route]:
        if row["period"] == str(old_period):
            row["outage"] = "0"
    recent = ingest(configuration(tmp_path, mode="recent"), FakeClient(revised_rows))
    assert recent["routes"]["national"]["changed"] == 0
    assert read_outage(tmp_path, recent, old_period) == read_outage(tmp_path, baseline, old_period)
    full = ingest(configuration(tmp_path), FakeClient(revised_rows))
    assert full["routes"]["national"]["changed"] == 1
    assert read_outage(tmp_path, full, old_period) == Decimal("0")


def test_additive_field_outside_refresh_interval_is_retained_in_snapshot_contract(tmp_path):
    rows = source_rows()
    rows[ROUTES[0].route][0]["future_measure"] = {"value": "original"}
    baseline = ingest(configuration(tmp_path), FakeClient(rows))
    assert baseline["routes"]["national"]["new_extra_fields"] == ["future_measure"]
    fresh_rows = source_rows()
    bounded = ingest(
        configuration(tmp_path, start=date(2026, 9, 2), end=date(2026, 9, 2)),
        FakeClient(fresh_rows),
    )
    assert bounded["routes"]["national"]["extra_fields"] == ["future_measure"]
    with duckdb.connect(config={"threads": 1}) as connection:
        raw = connection.execute(
            "SELECT _source_row FROM read_parquet(?) WHERE period='2026-09-01'",
            [str(route_file(tmp_path, bounded, "national"))],
        ).fetchone()[0]
    assert json.loads(raw)["future_measure"] == {"value": "original"}
    refreshed = ingest(configuration(tmp_path), FakeClient(fresh_rows))
    assert refreshed["routes"]["national"]["extra_fields"] == []
    assert refreshed["routes"]["national"]["missing_previous_extra_fields"] == ["future_measure"]


@pytest.mark.parametrize("mode", ["full", "recent"])
@pytest.mark.parametrize("shrinking_side", ["start", "end"])
def test_source_coverage_regression_never_discards_prior_history(
    tmp_path, mode, shrinking_side
):
    baseline = ingest(configuration(tmp_path), FakeClient())

    class ShrinkingCoverageClient(FakeClient):
        def fetch_metadata(self, route):
            metadata = super().fetch_metadata(route)
            if route != ROUTES[0].route:
                return metadata
            raw = deepcopy(metadata.raw)
            if shrinking_side == "end":
                raw["response"]["endPeriod"] = "2026-09-02"
                return replace(metadata, end_period=date(2026, 9, 2), raw=raw)
            raw["response"]["startPeriod"] = "2026-09-02"
            return replace(metadata, start_period=date(2026, 9, 2), raw=raw)

    client = ShrinkingCoverageClient()
    with pytest.raises(EIAClientError) as error:
        ingest(configuration(tmp_path, mode=mode), client)
    assert error.value.code == "metadata_regressed"
    assert client.calls == [], "Coverage must be checked before requesting any source rows."
    with Catalog(tmp_path) as catalog:
        assert catalog.current().id == baseline["snapshot_id"]
        assert catalog.verify()["verified"] is True
        assert catalog.history()[0]["status"] == "failed"
