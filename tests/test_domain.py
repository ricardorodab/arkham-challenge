from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal

import pytest

from outage_explorer.domain import (
    METRICS,
    ROUTE_BY_NAME,
    UNITS,
    DataValidationError,
    canonical_json,
    parse_date,
    validate_row,
)

START = date(2026, 9, 1)
END = date(2026, 9, 30)


def source_row(grain: str = "national") -> dict:
    row = {
        "period": "2026-09-01",
        "capacity": "97620.5",
        "outage": "2714.245",
        "percentOutage": "2.78",
    }
    row.update({f"{metric}-units": unit for metric, unit in UNITS.items()})
    if grain in {"facility", "generator"}:
        row.update({"facility": "0046", "facilityName": "Browns Ferry"})
    if grain == "generator":
        row["generator"] = "01"
    return row


def validated_values(grain: str, row: dict):
    spec = ROUTE_BY_NAME[grain]
    result = validate_row(spec, row, START, END)
    return result, dict(zip((name for name, _ in spec.columns), result.values, strict=True))


@pytest.mark.parametrize("grain", ["national", "facility", "generator"])
def test_normal_row_has_typed_date_exact_decimals_and_no_quality_flags(grain):
    result, values = validated_values(grain, source_row(grain))
    assert values["period"] == START
    assert values["capacity"] == Decimal("97620.500000000")
    assert values["outage"] == Decimal("2714.245000000")
    assert values["percentOutage"] == Decimal("2.780000000")
    assert all(isinstance(values[metric], Decimal) for metric in METRICS)
    assert result.flags == ()
    assert json.loads(values["_quality_flags"]) == []
    assert result.extra_fields == ()


@pytest.mark.parametrize(
    "missing",
    [
        "period",
        "capacity",
        "outage",
        "percentOutage",
        "capacity-units",
        "outage-units",
        "percentOutage-units",
    ],
)
def test_required_observation_fields_are_not_silently_defaulted(missing):
    row = source_row()
    row.pop(missing)
    with pytest.raises(DataValidationError, match="missing required fields") as caught:
        validate_row(ROUTE_BY_NAME["national"], row, START, END)
    assert missing in str(caught.value)


@pytest.mark.parametrize(
    "grain,missing",
    [("facility", "facility"), ("generator", "facility"), ("generator", "generator")],
)
def test_facility_and_generator_keys_are_required(grain, missing):
    row = source_row(grain)
    row.pop(missing)
    with pytest.raises(DataValidationError, match="missing required fields"):
        validate_row(ROUTE_BY_NAME[grain], row, START, END)


@pytest.mark.parametrize("period", ["2026-09-01", "2026-09-30"])
def test_requested_date_bounds_are_inclusive(period):
    row = source_row()
    row["period"] = period
    _, values = validated_values("national", row)
    assert values["period"] == date.fromisoformat(period)


@pytest.mark.parametrize("period", ["2026-08-31", "2026-10-01"])
def test_out_of_interval_observations_are_rejected(period):
    row = source_row()
    row["period"] = period
    with pytest.raises(DataValidationError, match="outside requested interval"):
        validated_values("national", row)


@pytest.mark.parametrize(
    "period",
    [
        "20260901",
        "2026-9-01",
        "2026-09-1",
        "2026-02-30",
        "2026-09-01T00:00:00Z",
        " 2026-09-01",
        None,
        20260901,
    ],
)
def test_only_calendar_iso_dates_are_accepted(period):
    row = source_row()
    row["period"] = period
    with pytest.raises(DataValidationError, match="invalid period"):
        validated_values("national", row)


def test_parse_date_rejects_compact_iso_even_when_python_accepts_it():
    assert date.fromisoformat("20260901") == START
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_date("20260901")


def test_identifiers_keep_leading_zeroes_and_generator_key_is_scoped_to_facility():
    first, values = validated_values("generator", source_row("generator"))
    assert values["facility"] == "0046"
    assert values["generator"] == "01"
    assert first.key == (START, "0046", "01")
    other_row = source_row("generator")
    other_row["facility"] = "0053"
    other, _ = validated_values("generator", other_row)
    assert first.key != other.key


@pytest.mark.parametrize("field", ["facility", "generator"])
@pytest.mark.parametrize("value", [None, "", " 01", "01 ", 1, True, "x" * 129])
def test_identifiers_must_be_nonempty_trimmed_text(field, value):
    row = source_row("generator")
    row[field] = value
    with pytest.raises(DataValidationError, match="invalid text identifier"):
        validated_values("generator", row)


@pytest.mark.parametrize("metric", METRICS)
@pytest.mark.parametrize("value", [None, ""])
def test_null_and_empty_measurements_are_preserved_and_flagged(metric, value):
    row = source_row()
    row[metric] = value
    result, values = validated_values("national", row)
    assert values[metric] is None
    assert f"null_{metric}" in result.flags
    assert json.loads(values["_source_row"])[metric] == value


@pytest.mark.parametrize("metric", METRICS)
def test_measurement_units_cannot_be_silently_converted(metric):
    row = source_row()
    row[f"{metric}-units"] = "kilowatts"
    with pytest.raises(DataValidationError, match="unexpected units"):
        validated_values("national", row)


@pytest.mark.parametrize(
    "value",
    [
        0.5,
        float("nan"),
        float("inf"),
        True,
        False,
        "NaN",
        "Infinity",
        "-Infinity",
        [],
        {},
        "not-a-number",
    ],
)
def test_nonfinite_and_unsupported_measurement_types_are_rejected(value):
    row = source_row()
    row["outage"] = value
    with pytest.raises(DataValidationError):
        validated_values("national", row)


def test_integer_measurements_are_exactly_supported():
    row = source_row()
    row["outage"] = 12
    _, values = validated_values("national", row)
    assert values["outage"] == Decimal("12.000000000")


def test_conversion_that_loses_more_than_nine_fractional_digits_is_rejected():
    row = source_row()
    row["outage"] = "1.0000000001"
    with pytest.raises(DataValidationError, match="lose decimal precision"):
        validated_values("national", row)


def test_trailing_zeroes_do_not_count_as_precision_loss():
    row = source_row()
    row["outage"] = "1.0000000000"
    _, values = validated_values("national", row)
    assert values["outage"] == Decimal(1)


@pytest.mark.parametrize("value", ["1000000000000000", "-1000000000000000", "9" * 129])
def test_measurement_precision_and_input_length_limits_are_enforced(value):
    row = source_row()
    row["capacity"] = value
    with pytest.raises(DataValidationError):
        validated_values("national", row)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"capacity": "0"}, {"capacity_non_positive", "outage_above_capacity"}),
        ({"capacity": "-1"}, {"capacity_non_positive", "outage_above_capacity"}),
        ({"outage": "-0.1"}, {"outage_negative"}),
        ({"outage": "100000"}, {"outage_above_capacity"}),
        ({"percentOutage": "100.01"}, {"percent_out_of_range"}),
        ({"percentOutage": "-0.01"}, {"percent_out_of_range"}),
    ],
)
def test_range_outliers_are_retained_with_quality_flags(changes, expected):
    row = source_row()
    row.update(changes)
    result, values = validated_values("national", row)
    assert expected.issubset(result.flags)
    for metric, original in changes.items():
        assert values[metric] == Decimal(original)
        assert json.loads(values["_source_row"])[metric] == original


@pytest.mark.parametrize("percent", ["0", "100"])
def test_percent_range_endpoints_are_valid(percent):
    row = source_row()
    row["percentOutage"] = percent
    result, _ = validated_values("national", row)
    assert "percent_out_of_range" not in result.flags


def test_additive_schema_fields_are_preserved_without_changing_typed_columns():
    row = source_row("facility")
    row["newSourceField"] = {"nested": ["observed", None, True, 2]}
    result, values = validated_values("facility", row)
    assert result.extra_fields == ("newSourceField",)
    assert json.loads(values["_source_row"])["newSourceField"] == row["newSourceField"]
    assert "newSourceField" not in values


def test_equivalent_key_order_has_stable_source_hash_for_duplicate_detection():
    row = source_row("generator")
    reversed_row = dict(reversed(list(row.items())))
    first, values = validated_values("generator", row)
    second, reordered = validated_values("generator", reversed_row)
    assert first.key == second.key
    assert values["_row_hash"] == reordered["_row_hash"]
    assert values["_source_row"] == reordered["_source_row"]
    assert values["_row_hash"] == hashlib.sha256(values["_source_row"].encode()).hexdigest()
    changed = dict(row, outage="2715.245")
    third, changed_values = validated_values("generator", changed)
    assert third.key == first.key
    assert changed_values["_row_hash"] != values["_row_hash"]


@pytest.mark.parametrize("value", [1, True, [], {}])
def test_invalid_facility_names_fail_with_safe_field_only_messages(value):
    row = source_row("facility")
    row["facilityName"] = value
    with pytest.raises(DataValidationError, match="facilityName must be text or null"):
        validated_values("facility", row)


@pytest.mark.parametrize("mode", ["absent", "null", "empty"])
def test_missing_facility_name_is_optional_and_flagged(mode):
    row = source_row("facility")
    if mode == "absent":
        row.pop("facilityName")
    else:
        row["facilityName"] = None if mode == "null" else ""
    result, values = validated_values("facility", row)
    assert "missing_facility_name" in result.flags
    assert values["facilityName"] in {None, ""}


def test_non_json_additional_source_values_are_rejected():
    row = source_row()
    row["newSourceField"] = float("nan")
    with pytest.raises(DataValidationError, match="source row is not valid JSON"):
        validated_values("national", row)


def test_canonical_json_preserves_unicode_without_allowing_nan():
    assert json.loads(canonical_json({"name": "Instalación"})) == {"name": "Instalación"}
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})


def test_untrusted_values_never_enter_validation_error_messages():
    canary = "TEST_ONLY_ERROR_VALUE_MUST_NOT_BE_LOGGED"
    row = source_row()
    row["outage"] = canary
    with pytest.raises(DataValidationError) as caught:
        validated_values("national", row)
    assert canary not in str(caught.value)
