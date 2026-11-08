"""Independent synthetic cases for the analytical model; not source findings."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from decimal import Decimal

import duckdb
import pytest

from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.analytics.modeling import ModelBuildError, build_model
from outage_explorer.data.domain import ROUTES, UNITS, identifier, validate_row

SOURCE_ID = "a" * 32
MODEL_ID = "b" * 32


def observation(period, capacity, outage, pct, *, facility=None, generator=None, name="Original"):
    row = {
        "period": period,
        "capacity": capacity,
        "outage": outage,
        "percentOutage": pct,
        **{f"{key}-units": unit for key, unit in UNITS.items()},
    }
    if facility is not None:
        row.update(facility=facility, facilityName=name)
    if generator is not None:
        row["generator"] = generator
    return row


def source_rows():
    rows = {spec.name: [] for spec in ROUTES}
    for day in (1, 2, 3):
        period = f"2026-09-{day:02d}"
        rows["national"].append(observation(period, "300", "30", "10"))
        for facility, capacity, outage in (("001", "100", "10"), ("002", "200", "20")):
            rows["facility"].append(observation(period, capacity, outage, "10", facility=facility))
            rows["generator"].append(
                observation(period, capacity, outage, "10", facility=facility, generator="1")
            )
    return rows


def write_sources(root, rows):
    root.mkdir()
    paths = {}
    with duckdb.connect(config={"threads": 1}) as con:
        for spec in ROUTES:
            con.execute("DROP TABLE IF EXISTS raw_data")
            columns = ",".join(f"{identifier(name)} {kind}" for name, kind in spec.columns)
            con.execute(f"CREATE TABLE raw_data({columns})")
            values = [
                validate_row(spec, row, date(2020, 1, 1), date(2030, 12, 31)).values
                for row in rows[spec.name]
            ]
            if values:
                con.executemany(
                    "INSERT INTO raw_data VALUES(" + ",".join("?" for _ in spec.columns) + ")",
                    values,
                )
            paths[spec.name] = root / f"{spec.name}.parquet"
            con.execute("COPY raw_data TO ? (FORMAT PARQUET)", [str(paths[spec.name])])
    return paths


def model(tmp_path, rows=None):
    paths = write_sources(tmp_path / "raw", rows if rows is not None else source_rows())
    return build_model(paths, SOURCE_ID, tmp_path / "model", model_id=MODEL_ID)


def query(bundle, dataset, selection="*", where="true", order_by="period"):
    # Arguments below are test-owned SQL; production exposes catalog descriptors.
    with duckdb.connect(config={"threads": 1}) as con:
        result = con.execute(
            f"SELECT {selection} FROM read_parquet(?) WHERE {where} ORDER BY {order_by}",
            [str(bundle.path / f"{dataset}.parquet")],
        )
        columns = [item[0] for item in result.description]
        return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def test_model_emits_all_typed_datasets_and_source_provenance(tmp_path):
    bundle = model(tmp_path)
    assert bundle.id == MODEL_ID
    assert bundle.manifest["source_snapshot_id"] == SOURCE_ID
    assert set(bundle.manifest["datasets"]) == set(DATASETS)
    with duckdb.connect(config={"threads": 1}) as con:
        for name, spec in DATASETS.items():
            path = bundle.path / f"{name}.parquet"
            assert path.is_file()
            actual = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
            assert [(row[0], row[1]) for row in actual] == list(spec.columns)
            assert bundle.manifest["datasets"][name]["primary_key"] == list(spec.primary_key)


def test_us08_uses_national_numerator_and_denominator_with_decimal_rounding(tmp_path):
    rows = source_rows()
    rows["national"][0].update(capacity="3", outage="1", percentOutage="33.33")
    row = query(model(tmp_path, rows), "fleet_daily")[0]
    assert row["offline_share"] == Decimal("0.333333333333")
    assert row["offline_pct"] == Decimal("33.333333333")
    assert row["capacity_mw"] == Decimal(3)
    assert row["outage_mw"] == Decimal(1)
    assert row["reported_outage_pct"] == Decimal("33.33")


def test_percentage_is_calculated_before_rounding_the_share(tmp_path):
    rows = source_rows()
    rows["national"][0].update(capacity="100000000", outage="0.123456789", percentOutage="0")
    row = query(model(tmp_path, rows), "fleet_daily")[0]
    assert row["offline_share"] == Decimal("0.000000001235")
    assert row["offline_pct"] == Decimal("0.000000123")  # Not 0.000000124 from double rounding.


@pytest.mark.parametrize("capacity,outage", [("0", "0"), (None, "1"), ("-1", "0"), ("100", None)])
def test_invalid_metric_denominator_or_null_measure_is_not_zero_filled(tmp_path, capacity, outage):
    rows = source_rows()
    rows["national"][0].update(capacity=capacity, outage=outage, percentOutage="11")
    row = query(model(tmp_path, rows), "fleet_daily")[0]
    assert row["offline_share"] is None
    assert row["offline_pct"] is None
    assert row["reported_outage_pct"] == Decimal(11)
    assert json.loads(row["quality_flags"])


def test_finite_outlier_metric_remains_visible_without_clipping(tmp_path):
    rows = source_rows()
    rows["national"][0].update(capacity="100", outage="150", percentOutage="150")
    row = query(model(tmp_path, rows), "fleet_daily")[0]
    assert row["offline_share"] == Decimal("1.500000000000")
    assert row["offline_pct"] == Decimal("150.000000000")
    assert json.loads(row["quality_flags"])


def test_generator_identifier_is_scoped_to_facility(tmp_path):
    bundle = model(tmp_path)
    dims = query(bundle, "dim_generator", order_by="facility_id,generator_id")
    assert [(row["facility_id"], row["generator_id"]) for row in dims] == [
        ("001", "1"),
        ("002", "1"),
    ]
    assert len(query(bundle, "generator_daily", order_by="period,facility_id,generator_id")) == 6


def test_missing_facility_parent_preserves_generator_and_union_dimension(tmp_path):
    rows = source_rows()
    rows["generator"].append(
        observation(
            "2026-09-01", "50", "5", "10", facility="404", generator="1", name="Generator only"
        )
    )
    bundle = model(tmp_path, rows)
    dim = query(bundle, "dim_facility", where="facility_id='404'", order_by="facility_id")[0]
    assert dim["seen_in_generator_route"] and not dim["seen_in_facility_route"]
    fact = query(bundle, "generator_daily", where="facility_id='404'")[0]
    assert fact["capacity_mw"] == Decimal(50)
    assert not query(bundle, "facility_daily", where="facility_id='404'")
    recon = query(bundle, "facility_reconciliation_daily", where="facility_id='404'")[0]
    assert recon["coverage_status"] == "missing_parent"
    assert recon["generator_capacity_mw"] == Decimal(50)
    assert recon["facility_capacity_mw"] is None
    assert recon["capacity_gap_mw"] is None


def test_fact_names_keep_observation_history_and_dimension_uses_latest_name(tmp_path):
    rows = source_rows()
    for grain in ("facility", "generator"):
        for row in rows[grain]:
            if row["period"] > "2026-09-01":
                row["facilityName"] = "Renamed"
    bundle = model(tmp_path, rows)
    facts = query(bundle, "facility_daily", where="facility_id='001'")
    assert [row["facility_name"] for row in facts] == ["Original", "Renamed", "Renamed"]
    assert (
        query(bundle, "dim_facility", where="facility_id='001'", order_by="facility_id")[0][
            "facility_name"
        ]
        == "Renamed"
    )


def test_null_child_measure_makes_comparison_unknown_not_a_partial_zero_sum(tmp_path):
    rows = source_rows()
    rows["generator"][0]["outage"] = None
    bundle = model(tmp_path, rows)
    facility = query(bundle, "facility_reconciliation_daily", where="facility_id='001'")[0]
    assert facility["generator_null_outage_count"] == 1
    assert facility["generator_outage_mw"] is None
    assert facility["outage_gap_mw"] is None
    assert facility["coverage_status"] == "null_measure"
    national = query(bundle, "national_reconciliation_daily")[0]
    assert national["generator_outage_mw"] is None
    assert national["generator_outage_gap_mw"] is None
    assert national["facility_outage_gap_mw"] == Decimal(0)


def test_reconciliation_preserves_signed_gaps_without_replacing_source_values(tmp_path):
    rows = source_rows()
    rows["generator"][0].update(capacity="101", outage="11")
    bundle = model(tmp_path, rows)
    facility = query(bundle, "facility_reconciliation_daily", where="facility_id='001'")[0]
    assert facility["capacity_gap_mw"] == Decimal(1)
    assert facility["outage_gap_mw"] == Decimal(1)
    national = query(bundle, "national_reconciliation_daily")[0]
    assert national["generator_capacity_gap_mw"] == Decimal(1)
    assert national["generator_outage_gap_mw"] == Decimal(1)
    assert national["facility_capacity_gap_mw"] == Decimal(0)
    assert query(bundle, "fleet_daily")[0]["offline_share"] == Decimal("0.100000000000")


def test_published_percentages_are_capacity_weighted_and_rounding_is_reported(tmp_path):
    rows = {
        "national": [observation("2026-09-01", "300", "200", "66.67")],
        "facility": [observation("2026-09-01", "300", "200", "66.67", facility="001")],
        "generator": [
            observation("2026-09-01", "100", "0", "0", facility="001", generator="1"),
            observation("2026-09-01", "200", "200", "100", facility="001", generator="2"),
        ],
    }
    row = query(model(tmp_path, rows), "facility_reconciliation_daily")[0]
    assert row["generator_weighted_reported_pct"] == Decimal("66.666666667")
    assert row["generator_weighted_reported_pct"] != Decimal(50)
    assert row["reported_pct_gap_pp"] == Decimal("-0.003333333")
    assert row["capacity_gap_mw"] == row["outage_gap_mw"] == Decimal(0)


def test_zero_capacity_with_positive_reported_pct_generates_reviewable_events(tmp_path):
    rows = source_rows()
    rows["generator"][0].update(capacity="0", outage="0", percentOutage="11")
    bundle = model(tmp_path, rows)
    events = query(
        bundle,
        "data_quality_events",
        where="facility_id='001' AND period='2026-09-01'",
        order_by="rule_id",
    )
    assert {row["rule_id"] for row in events} >= {
        "zero_capacity",
        "zero_capacity_positive_reported_pct",
    }


def test_interior_gap_and_capacity_step_are_detected_without_filling_facts(tmp_path):
    rows = source_rows()
    rows["generator"] = [
        row
        for row in rows["generator"]
        if not (row["facility"] == "001" and row["period"] == "2026-09-02")
    ]
    next(
        row
        for row in rows["generator"]
        if row["facility"] == "001" and row["period"] == "2026-09-03"
    )["capacity"] = "110"
    next(
        row
        for row in rows["generator"]
        if row["facility"] == "002" and row["period"] == "2026-09-02"
    )["capacity"] = "310"
    bundle = model(tmp_path, rows)
    events = query(
        bundle,
        "data_quality_events",
        where="grain='generator' AND facility_id='001'",
        order_by="rule_id",
    )
    gaps = [row for row in events if row["rule_id"] == "entity_coverage_gap"]
    assert len(gaps) == 1 and gaps[0]["gap_days"] == 1
    assert gaps[0]["period"] == date(2026, 9, 3)
    assert gaps[0]["previous_period"] == date(2026, 9, 1)
    assert not any(row["rule_id"] == "capacity_change" for row in events)
    changes = query(
        bundle,
        "data_quality_events",
        where="grain='generator' AND facility_id='002' AND rule_id='capacity_change'",
    )
    assert changes[0]["before_capacity_mw"] == Decimal(200)
    assert changes[0]["after_capacity_mw"] == Decimal(310)
    assert len(query(bundle, "generator_daily", where="facility_id='001'")) == 2
    missing = query(
        bundle, "facility_reconciliation_daily", where="facility_id='001' AND period='2026-09-02'"
    )[0]
    assert missing["coverage_status"] == "missing_children"
    assert missing["generator_capacity_mw"] is None


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_raw_keys_fail_instead_of_fanning_out_dimensions(tmp_path, conflicting):
    rows = source_rows()
    duplicate = deepcopy(rows["generator"][0])
    if conflicting:
        duplicate["outage"] = "99"
    rows["generator"].append(duplicate)
    paths = write_sources(tmp_path / "raw", rows)
    with pytest.raises(ModelBuildError, match="[Dd]uplicate|[Uu]nique"):
        build_model(paths, SOURCE_ID, tmp_path / "model", model_id=MODEL_ID)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("UPDATE malformed SET facility=NULL", "non-null"),
        ("UPDATE malformed SET \"capacity-units\"='kilowatts'", "units"),
        ("ALTER TABLE malformed ALTER capacity TYPE DOUBLE", "schema"),
        ("DELETE FROM malformed", "nonempty"),
    ],
)
def test_structurally_invalid_raw_source_cannot_become_an_analytical_model(
    tmp_path, mutation, reason
):
    paths = write_sources(tmp_path / "raw", source_rows())
    invalid = tmp_path / "invalid.parquet"
    with duckdb.connect(config={"threads": 1}) as con:
        con.execute(
            "CREATE TABLE malformed AS SELECT * FROM read_parquet(?)", [str(paths["generator"])]
        )
        con.execute(mutation)
        con.execute("COPY malformed TO ? (FORMAT PARQUET)", [str(invalid)])
    paths["generator"] = invalid
    with pytest.raises(ModelBuildError, match=reason):
        build_model(paths, SOURCE_ID, tmp_path / "model", model_id=MODEL_ID)


def test_rebuild_has_identical_logical_content_for_every_dataset(tmp_path):
    paths = write_sources(tmp_path / "raw", source_rows())
    first = build_model(paths, SOURCE_ID, tmp_path / "first", model_id=MODEL_ID)
    second = build_model(paths, SOURCE_ID, tmp_path / "second", model_id="c" * 32)
    assert first.id != second.id
    for name in DATASETS:
        assert (
            first.manifest["datasets"][name]["logical_sha256"]
            == second.manifest["datasets"][name]["logical_sha256"]
        )
