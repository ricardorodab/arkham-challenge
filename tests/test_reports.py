"""Fixed evidence SQL is independently executable against synthetic observations."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest

from outage_explorer.analytics import reports
from outage_explorer.analytics.model_contracts import MODEL_VERSION
from outage_explorer.analytics.reports import findings_report, json_value, report_queries
from outage_explorer.data.domain import ROUTES
from outage_explorer.data.pipeline import IngestConfig, ingest
from outage_explorer.data.storage import Catalog, StorageError
from tests.helpers import FakeClient
from tests.test_modeling import observation


def month_source_rows():
    """31 calendar days, with known absence, nonzero gap, null and orphan cases."""
    rows = {spec.name: [] for spec in ROUTES}
    for offset in range(31):
        period = (date(2026, 9, 1) + timedelta(days=offset)).isoformat()
        if period == "2026-09-12":
            continue  # An absent day is not a zero-outage observation.
        rows["national"].append(observation(period, "300", "30.001", "10"))
        for facility, capacity, outage in (("001", "100", "10.001"), ("002", "200", "20")):
            if not (period == "2026-09-17" and facility == "002"):
                rows["facility"].append(
                    observation(period, capacity, outage, "10", facility=facility)
                )
            pct = "10"
            if period == "2026-09-15" and facility == "001":
                capacity, outage, pct = "101", "11.001", "10.89"
            if period == "2026-09-16" and facility == "002":
                outage, pct = None, None
            rows["generator"].append(
                observation(period, capacity, outage, pct, facility=facility, generator="1")
            )
    return {spec.route: rows[spec.name] for spec in ROUTES}


@pytest.fixture(scope="module")
def published_month(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthetic-report-data")
    ingest(IngestConfig(root, "full"), FakeClient(month_source_rows()))
    with Catalog(root) as catalog:
        bundle = catalog.current_model()
    return root, bundle


def records(report, name):
    table = report["queries"][name]
    return [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]


def test_packaged_report_assets_have_unique_names_single_select_and_only_date_parameters():
    statements = report_queries()
    assert len(statements) == 18
    assert statements["automated_quality_event_evidence"][0] == "part2_findings.sql"
    assert {asset for asset, _ in statements.values()} == {
        "reconciliation.sql",
        "part2_findings.sql",
    }
    with duckdb.connect(config={"threads": 1}) as con:
        for _, sql in statements.values():
            parsed = con.extract_statements(sql)
            assert len(parsed) == 1 and parsed[0].type == duckdb.StatementType.SELECT
            assert parsed[0].named_parameters <= {"start", "end"}
            assert "?" not in sql


def test_month_report_reproduces_exact_signed_gaps_without_zero_filling(published_month):
    root, bundle = published_month
    report = findings_report(root, date(2026, 9, 1), date(2026, 9, 30))
    assert report["model_id"] == bundle.id
    assert report["source_snapshot_id"] == bundle.manifest["source_snapshot_id"]
    assert report["model_version"] == MODEL_VERSION
    assert report["requested_calendar_days"] == 30
    daily = {row["period"]: row for row in records(report, "daily_national_reconciliation")}
    assert len(daily) == 30
    baseline = daily["2026-09-01"]
    assert baseline["national_capacity_mw"] == "300.000000000"
    assert baseline["national_outage_mw"] == "30.001000000"
    assert baseline["facility_capacity_gap_mw"] == "0.000000000"
    assert baseline["generator_outage_gap_mw"] == "0.000000000"
    missing = daily["2026-09-12"]
    assert missing["national_present"] is False
    assert missing["national_outage_mw"] is None
    assert missing["generator_outage_mw"] is None
    assert daily["2026-09-15"]["generator_capacity_gap_mw"] == "1.000000000"
    assert daily["2026-09-15"]["generator_outage_gap_mw"] == "1.000000000"
    null_child = daily["2026-09-16"]
    assert null_child["generator_null_outage_count"] == 1
    assert null_child["generator_outage_mw"] is None
    assert null_child["generator_outage_gap_mw"] is None
    assert daily["2026-09-17"]["facility_capacity_gap_mw"] == "-200.000000000"
    assert daily["2026-09-17"]["facility_outage_gap_mw"] == "-20.000000000"
    facilities = records(report, "daily_facility_reconciliation")
    orphan = next(
        row for row in facilities if row["period"] == "2026-09-17" and row["facility_id"] == "002"
    )
    assert orphan["facility_present"] is False
    assert orphan["generator_count"] == 1 and orphan["capacity_gap_mw"] is None
    summary = {row["comparison"]: row for row in records(report, "full_history_mw_summary")}
    assert summary["generator_to_facility"]["compared_keys"] == 60
    assert summary["generator_to_facility"]["missing_parent_keys"] == 1
    assert summary["generator_to_facility"]["uncomparable_keys"] == 2
    assert summary["generator_to_facility"]["nonzero_mw_gap_keys"] == 1
    assert summary["generator_to_national"]["uncomparable_keys"] == 1
    assert summary["facility_to_national"]["max_capacity_abs_gap_mw"] == "200.000000000"
    assert report["queries"]["indian_point_evidence"]["row_count"] == 0
    assert report["queries"]["grand_gulf_capacity_change"]["row_count"] == 0
    assert report["queries"]["automated_quality_event_evidence"]["row_count"] == 0
    for label, (_, sql) in report_queries().items():
        table = report["queries"][label]
        assert table["row_count"] == len(table["rows"])
        assert table["sql_sha256"] == hashlib.sha256(sql.encode()).hexdigest()
    # Decimal/date conversion occurs before the report reaches CLI/file JSON.
    assert json.loads(json.dumps(report, allow_nan=False))["reconciliation_start"] == "2026-09-01"


@pytest.mark.parametrize(
    "start,end",
    [(date(2026, 9, 1), date(2026, 9, 29)), (date(2026, 9, 30), date(2026, 9, 1))],
)
def test_report_requires_thirty_inclusive_calendar_days_before_storage(
    tmp_path, monkeypatch, start, end
):
    def forbidden(*args, **kwargs):
        pytest.fail("A rejected date range must not open data or load statements.")

    monkeypatch.setattr(reports, "report_queries", forbidden)
    monkeypatch.setattr(reports, "open_model_connection", forbidden)
    with pytest.raises(ValueError, match="30 calendar days"):
        findings_report(tmp_path, start, end)


@pytest.mark.parametrize(
    "sql",
    ["SELECT 1; SELECT 2", "CREATE TABLE forbidden(id INTEGER)", "SELECT $unknown", "SELECT ?"],
)
def test_report_execution_rejects_extra_statements_writes_or_unknown_parameters(
    published_month, monkeypatch, sql
):
    root, _ = published_month
    monkeypatch.setattr(reports, "report_queries", lambda: {"unsafe": ("test.sql", sql)})
    with pytest.raises(StorageError) as caught:
        findings_report(root, date(2026, 9, 1), date(2026, 9, 30))
    assert caught.value.code == "invalid_report"


def test_json_values_preserve_decimal_precision_and_iso_dates():
    assert json_value(Decimal("0.000000000001")) == "0.000000000001"
    assert json_value(Decimal("300.000000000")) == "300.000000000"
    assert json_value(date(2026, 9, 1)) == "2026-09-01"
    assert json_value(datetime(2026, 9, 1, tzinfo=UTC)) == "2026-09-01T00:00:00+00:00"
    assert json_value(None) is None and json_value(True) is True
