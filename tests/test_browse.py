"""Structured browse plans retain exact types and seek through complete immutable tables."""

from __future__ import annotations

import json
import random
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.query.browse import BrowseError, compile_browse, normalize_query


@pytest.fixture
def connection():
    schema = DATASETS["generator_daily"]
    with duckdb.connect(config={"threads": 1}) as con:
        con.execute(
            "CREATE TABLE generator_daily("
            + ",".join(f'"{name}" {kind}' for name, kind in schema.columns)
            + ")"
        )
        names = ("Énergie '50%_ Á", "ordinary50XXY", "alpha", "Énergie different")
        rows = []
        for index in range(1327):
            rows.append(
                (
                    date(2026, 1, 1) + timedelta(days=index // 21),
                    f"{(index // 7) % 3:03d}",
                    f"{index % 7:02d}",
                    names[index % 4],
                    None if index % 8 == 0 else Decimal(f"{index % 11}.123456789"),
                    None if index % 9 == 0 else Decimal(f"-{index % 7}.000000001"),
                    Decimal("10.000000000"),
                    "[]",
                    f"{index:064x}",
                )
            )
        con.executemany("INSERT INTO generator_daily VALUES(?,?,?,?,?,?,?,?,?)", rows[::-1])
        yield con


def seek_values(row, columns):
    positions = {name: index for index, (name, _) in enumerate(DATASETS["generator_daily"].columns)}
    result = []
    for column in columns:
        value = row[positions[column]]
        result.append(
            None if value is None else value.isoformat() if isinstance(value, date) else str(value)
        )
    return result


@pytest.mark.parametrize(
    "parameters,ordering",
    [
        ({}, '"period" ASC,"facility_id" ASC,"generator_id" ASC'),
        ({"direction": "desc"}, '"period" DESC,"facility_id" ASC,"generator_id" ASC'),
        (
            {"sort": "capacity_mw"},
            '"capacity_mw" ASC NULLS LAST,"period" ASC,"facility_id" ASC,"generator_id" ASC',
        ),
        (
            {"sort": "capacity_mw", "direction": "desc"},
            '"capacity_mw" DESC NULLS LAST,"period" ASC,"facility_id" ASC,"generator_id" ASC',
        ),
        (
            {"sort": "facility_name", "direction": "desc"},
            '"facility_name" DESC NULLS LAST,"period" ASC,"facility_id" ASC,"generator_id" ASC',
        ),
        (
            {"sort": "facility_id", "direction": "desc"},
            '"facility_id" DESC NULLS LAST,"period" ASC,"generator_id" ASC',
        ),
    ],
)
def test_seek_visits_more_than_thousand_rows_once_across_duplicate_and_null_sort_keys(
    connection, parameters, ordering
):
    plan = normalize_query("generator_daily", parameters)
    expected = connection.execute("SELECT * FROM generator_daily ORDER BY " + ordering).fetchall()
    actual, last, pages = [], None, 0
    while True:
        sql, values, columns = compile_browse("generator_daily", plan, 113, last)
        assert "OFFSET" not in sql and values[-1] == 114
        batch = connection.execute(sql, values).fetchall()
        actual.extend(batch[:113])
        pages += 1
        assert pages < 30
        if len(batch) <= 113:
            break
        last = seek_values(batch[112], columns)
    assert pages >= 5 and len(actual) == 1327 > 1000
    assert actual == expected
    assert len({row[:3] for row in actual}) == len(actual)


def test_combined_typed_filters_legacy_ids_and_literal_unicode_search(connection):
    filters = [
        {"column": "capacity_mw", "op": "gte", "value": "1.123456789"},
        {"column": "outage_mw", "op": "not_null"},
        {"column": "facility_name", "op": "contains", "value": "Énergie"},
    ]
    parameters = {
        "search": "éNERGIE '50%_",
        "start": "2026-01-03",
        "end": "2026-02-10",
        "facility_id": "001",
        "generator_id": "01",
        "filters": json.dumps(filters),
        "sort": "capacity_mw",
        "direction": "desc",
    }
    plan = normalize_query("generator_daily", parameters)
    sql, values, _ = compile_browse("generator_daily", plan, 1000)
    assert parameters["search"] not in sql and " LIKE " not in sql
    actual = connection.execute(sql, values).fetchall()
    expected = connection.execute(
        """SELECT * FROM generator_daily
        WHERE period BETWEEN DATE '2026-01-03' AND DATE '2026-02-10'
          AND facility_id='001' AND generator_id='01' AND capacity_mw>=1.123456789
          AND outage_mw IS NOT NULL AND contains(facility_name,'Énergie')
          AND contains(lower(facility_name), lower(?))
        ORDER BY capacity_mw DESC NULLS LAST,period,facility_id,generator_id""",
        [parameters["search"]],
    ).fetchall()
    assert actual and actual == expected
    assert all(row[1:3] == ("001", "01") for row in actual)


@pytest.mark.parametrize("search,expected", [("%_", 332), ("2026-01-02", 21)])
def test_global_literal_search_covers_text_and_dates_without_wildcards(
    connection, search, expected
):
    sql, values, _ = compile_browse(
        "generator_daily", normalize_query("generator_daily", {"search": search}), 1000
    )
    assert len(connection.execute(sql, values).fetchall()) == expected


def test_decimal38_filter_and_sort_do_not_round_or_compare_as_floats():
    schema = DATASETS["facility_reconciliation_daily"]
    with duckdb.connect(config={"threads": 1}) as con:
        con.execute(
            "CREATE TABLE facility_reconciliation_daily("
            + ",".join(f'"{name}" {kind}' for name, kind in schema.columns)
            + ")"
        )
        prefix = "12345678901234567890123456789.12345678"
        for index in range(3):
            con.execute(
                "INSERT INTO facility_reconciliation_daily(period,facility_id,generator_capacity_mw) VALUES(DATE '2026-01-01',?,CAST(? AS DECIMAL(38,9)))",
                [f"{index:03}", prefix + str(index)],
            )
        plan = normalize_query(
            "facility_reconciliation_daily",
            {
                "sort": "generator_capacity_mw",
                "filters": [
                    {
                        "column": "generator_capacity_mw",
                        "op": "between",
                        "value": prefix + "1",
                        "upper": prefix + "2",
                    }
                ],
            },
        )
        sql, values, order = compile_browse("facility_reconciliation_daily", plan, 1)
        rows = con.execute(sql, values).fetchall()
        position = dict((name, index) for index, (name, _) in enumerate(schema.columns))
        assert str(rows[0][position["generator_capacity_mw"]]) == prefix + "1"
        last = [str(rows[0][position[column]]) for column in order]
        last[1] = rows[0][position[order[1]]].isoformat()
        sql, values, _ = compile_browse("facility_reconciliation_daily", plan, 1, last)
        rows = con.execute(sql, values).fetchall()
        assert len(rows) == 1 and str(rows[0][position["generator_capacity_mw"]]) == prefix + "2"


def test_normalization_is_serializable_order_independent_and_preserves_literal_ids():
    predicates = [
        {"column": "capacity_mw", "op": "between", "value": "+1.20", "upper": "3"},
        {"column": "facility_id", "op": "eq", "value": "001"},
    ]
    first = normalize_query(
        "generator_daily",
        {"filters": predicates * 2, "cursor": "ignored", "limit": "10", "model_id": "ignored"},
    )
    second = normalize_query("generator_daily", {"filters": predicates[::-1]})
    assert first == second == json.loads(json.dumps(first))
    assert first["search"] == "" and set(first) == {"search", "filters", "order"}
    assert first["filters"][0]["value"] == "1.200000000"
    assert first["filters"][1]["value"] == "001"


@pytest.mark.parametrize(
    "parameters",
    [
        {"search": "x" * 257},
        {"search": "x\x00y"},
        {"search": "\ud800"},
        {"sort": "capacity_mw; DROP TABLE generator_daily"},
        {"sort": ["period"]},
        {"direction": "DESC NULLS FIRST"},
        {"sql": "SELECT 1"},
        {"filters": '[{"column":"period","op":"eq","op":"lt","value":"2026-01-01"}]'},
        {"filters": [{"column": "capacity_mw", "op": "eq", "value": float("nan")}]},
        {"filters": [{"column": "capacity_mw", "op": "eq", "value": "NaN"}]},
        {"filters": [{"column": "capacity_mw", "op": "eq", "value": 1.25}]},
        {"filters": [{"column": "capacity_mw", "op": "eq", "value": "1000000000000000"}]},
        {"filters": [{"column": "capacity_mw", "op": "eq", "value": "0.0000000001"}]},
        {"filters": [{"column": "period", "op": "eq", "value": "20260101"}]},
        {"filters": [{"column": "period", "op": "eq", "value": "2026-02-30"}]},
        {
            "filters": [
                {"column": "period", "op": "between", "value": "2026-01-02", "upper": "2026-01-01"}
            ]
        },
        {"filters": [{"column": "period", "op": "is_null", "value": None}]},
        {"filters": [{"column": "facility_id", "op": "eq", "value": 1}]},
        {"filters": [{"column": "facility_id", "op": "eq", "value": "001", "sql": "OR TRUE"}]},
        {"filters": [{"column": "period", "op": "is_null"}] * 9},
    ],
)
def test_invalid_or_injected_requests_fail_with_static_errors(parameters):
    with pytest.raises(BrowseError) as caught:
        normalize_query("generator_daily", parameters)
    assert str(caught.value) == "The structured browse request is invalid."


def test_untrusted_values_are_bound_and_child_rejects_noncanonical_or_changed_plan(connection):
    hostile = "' OR TRUE; -- %_ É"
    plan = normalize_query(
        "generator_daily",
        {"filters": [{"column": "facility_name", "op": "contains", "value": hostile}]},
    )
    sql, values, _ = compile_browse("generator_daily", plan, 100)
    assert hostile in values and hostile not in sql
    assert connection.execute(sql, values).fetchall() == []
    for changed in (
        {**plan, "sql": "SELECT 1"},
        {**plan, "order": []},
        {**plan, "filters": [{"column": "period", "op": "eq", "value": "20260101"}]},
    ):
        with pytest.raises(BrowseError):
            compile_browse("generator_daily", changed, 10)
    assert connection.execute("SELECT count(*) FROM generator_daily").fetchone()[0] == 1327


def test_seeded_literal_payloads_never_change_the_compiled_sql_structure():
    generator = random.Random(571)
    structure = None
    for _ in range(75):
        payload = "FUZZ_ONLY_" + "".join(generator.choices("'\";-%_\\\nÉ🔥()= abc012", k=40))
        plan = normalize_query(
            "generator_daily",
            {
                "search": payload,
                "filters": [
                    {"column": "facility_name", "op": "eq", "value": payload},
                ],
            },
        )
        sql, parameters, _ = compile_browse("generator_daily", plan, 30)
        if structure is None:
            structure = sql
        assert sql == structure and payload not in sql
        assert all(value == payload for value in parameters[:-1]) and parameters[-1] == 31


@pytest.mark.parametrize(
    "last",
    [
        [],
        ["1"],
        [1, "2026-01-01", "001", "01"],
        ["NaN", "2026-01-01", "001", "01"],
        ["1", "2026-02-30", "001", "01"],
    ],
)
def test_cursor_key_count_and_full_declared_types_are_revalidated(last):
    plan = normalize_query("generator_daily", {"sort": "capacity_mw"})
    with pytest.raises(BrowseError) as caught:
        compile_browse("generator_daily", plan, 10, last)
    assert caught.value.code == "invalid_cursor"


def test_seek_text_can_exceed_operand_limit_without_truncation_and_has_byte_bounds():
    plan = normalize_query("generator_daily", {"sort": "facility_name"})
    value = "É" * 1000
    _, values, _ = compile_browse("generator_daily", plan, 10, [value, "2026-01-01", "001", "01"])
    assert value in values
    for invalid in ("É" * 8193, "x\x00y"):
        with pytest.raises(BrowseError):
            compile_browse("generator_daily", plan, 10, [invalid, "2026-01-01", "001", "01"])
    changed = deepcopy(plan)
    changed["order"][-1]["direction"] = "desc"
    with pytest.raises(BrowseError):
        compile_browse("generator_daily", changed, 10)


def test_integer_boolean_and_null_filters_obey_declared_types():
    plan = normalize_query(
        "national_reconciliation_daily",
        {
            "filters": [
                {"column": "facility_count", "op": "gte", "value": "0002"},
                {"column": "national_present", "op": "eq", "value": True},
                {"column": "national_capacity_mw", "op": "is_null"},
            ]
        },
    )
    assert {item.get("value") for item in plan["filters"]} == {"2", "true", None}
    for value in (True, 2**31, "1.5"):
        with pytest.raises(BrowseError):
            normalize_query(
                "national_reconciliation_daily",
                {"filters": [{"column": "facility_count", "op": "eq", "value": value}]},
            )


@pytest.mark.parametrize(
    "column,direction",
    [
        ("national_present", "asc"),
        ("national_present", "desc"),
        ("facility_count", "asc"),
        ("facility_count", "desc"),
    ],
)
def test_boolean_and_integer_seek_pages_keep_nullable_exact_keys(column, direction):
    spec = DATASETS["national_reconciliation_daily"]
    positions = {name: index for index, (name, _) in enumerate(spec.columns)}
    with duckdb.connect(config={"threads": 1}) as con:
        con.execute(
            "CREATE TABLE national_reconciliation_daily("
            + ",".join(f'"{name}" {kind}' for name, kind in spec.columns)
            + ")"
        )
        for index in range(23):
            con.execute(
                "INSERT INTO national_reconciliation_daily(period,national_present,facility_count) VALUES(?,?,?)",
                [
                    date(2026, 1, 1) + timedelta(days=index),
                    (None, False, True)[index % 3],
                    None if index % 4 == 0 else index % 4 - 2,
                ],
            )
        expected = con.execute(
            f'SELECT * FROM national_reconciliation_daily ORDER BY "{column}" {direction} NULLS LAST,period ASC'
        ).fetchall()
        plan = normalize_query(spec.id, {"sort": column, "direction": direction})
        actual, last = [], None
        while True:
            sql, values, order = compile_browse(spec.id, plan, 3, last)
            batch = con.execute(sql, values).fetchall()
            actual.extend(batch[:3])
            if len(batch) <= 3:
                break
            last = []
            for name in order:
                value = batch[2][positions[name]]
                last.append(
                    None
                    if value is None
                    else value.isoformat()
                    if isinstance(value, date)
                    else "true"
                    if value is True
                    else "false"
                    if value is False
                    else str(value)
                )
        assert actual == expected


@pytest.mark.parametrize(
    "operation,value,reference",
    [
        ("eq", "alpha", "facility_name='alpha'"),
        ("ne", "alpha", "facility_name!='alpha'"),
        ("contains", "'50%_", "contains(facility_name,'''50%_')"),
        ("starts_with", "Énergie", "starts_with(facility_name,'Énergie')"),
        ("not_contains", "Énergie", "NOT contains(facility_name,'Énergie')"),
    ],
)
def test_text_filter_operators_use_literal_comparison_semantics(
    connection, operation, value, reference
):
    plan = normalize_query(
        "generator_daily",
        {"filters": [{"column": "facility_name", "op": operation, "value": value}]},
    )
    sql, values, _ = compile_browse("generator_daily", plan, 1000)
    actual = connection.execute(sql, values).fetchall()
    expected = connection.execute(
        "SELECT * FROM generator_daily WHERE "
        + reference
        + " ORDER BY period,facility_id,generator_id"
    ).fetchall()
    assert actual == expected


@pytest.mark.parametrize(
    "parameters,code",
    [
        ({"start": "20260101"}, "invalid_date"),
        ({"start": "2026-01-02", "end": "2026-01-01"}, "invalid_date_range"),
    ],
)
def test_legacy_date_errors_keep_their_safe_categories(parameters, code):
    with pytest.raises(BrowseError) as caught:
        normalize_query("generator_daily", parameters)
    assert caught.value.code == code


def test_legacy_scope_filters_are_rejected_when_the_dataset_has_no_such_field():
    with pytest.raises(BrowseError) as caught:
        normalize_query("fleet_daily", {"facility_id": "001"})
    assert caught.value.code == "unsupported_filter"
