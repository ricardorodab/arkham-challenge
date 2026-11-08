"""Adversarial SQL subset tests execute only regenerated SQL on synthetic tables."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest
import sqlglot

from outage_explorer.analytics.model_contracts import DATASETS
from outage_explorer.data.domain import identifier
from outage_explorer.query import sql_policy
from outage_explorer.query.sql_policy import SQLPolicyError, referenced_dataset_ids, validate_sql


@pytest.fixture
def connection():
    with duckdb.connect(config={"threads": 1}) as con:
        columns = ",".join(
            f"{identifier(name)} {kind}" for name, kind in DATASETS["fleet_daily"].columns
        )
        con.execute(f"CREATE TABLE fleet_daily({columns})")
        # Deliberately insert out of order and include ordering ties and NULLs.
        for offset in (4, 0, 5, 2, 1, 3):
            outage = [10, 20, 10, None, 10, 0][offset]
            con.execute(
                "INSERT INTO fleet_daily VALUES(?,?,?,?,?,?,?,?)",
                [
                    date(2026, 9, 1) + timedelta(days=offset),
                    Decimal(100),
                    Decimal(outage) if outage is not None else None,
                    Decimal(outage) if outage is not None else None,
                    Decimal(outage) / 100 if outage is not None else None,
                    Decimal(outage) if outage is not None else None,
                    Decimal(0) if outage is not None else None,
                    "[]",
                ],
            )
        yield con


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM fleet_daily",
        'SELECT "F".* FROM "FLEET_DAILY" AS "F"',
        "SELECT period, capacity_mw, outage_mw FROM fleet_daily WHERE period >= DATE '2026-09-02'",
        "SELECT period FROM fleet_daily WHERE period < CAST('2026-09-05' AS DATE) ORDER BY period DESC",
        "SELECT capacity_mw+1, capacity_mw-1, capacity_mw*2, capacity_mw/2, capacity_mw//3, capacity_mw%3 FROM fleet_daily",
        "SELECT COUNT(*), COUNT(DISTINCT period), SUM(capacity_mw), AVG(outage_mw), MIN(period), MAX(period) FROM fleet_daily",
        "SELECT outage_mw, COUNT(*) AS n FROM fleet_daily GROUP BY outage_mw HAVING n > 0 ORDER BY n DESC",
        "SELECT ROUND(AVG(outage_mw),3) AS average FROM fleet_daily",
        "SELECT COALESCE(outage_mw,0), ABS(-1), ROUND(capacity_mw,-1) FROM fleet_daily",
        "SELECT CAST(capacity_mw AS DECIMAL(24,9)), CAST(period AS VARCHAR), CAST(1 AS BOOLEAN) FROM fleet_daily",
        "SELECT period FROM fleet_daily WHERE capacity_mw IS NOT NULL AND NOT(capacity_mw<0 OR capacity_mw>1000)",
        "SELECT period FROM fleet_daily WHERE capacity_mw BETWEEN 1 AND 200 AND outage_mw IN(0,10,20)",
        "SELECT period FROM fleet_daily WHERE quality_flags LIKE '%' AND quality_flags ILIKE '%'",
        "SELECT 1, 'literal', NULL, TRUE, FALSE FROM fleet_daily LIMIT 2",
        "SELECT /* ordinary comment */ period FROM fleet_daily; -- trailing comment",
        "SELECT capacity_mw AS capacity FROM fleet_daily GROUP BY capacity ORDER BY capacity",
    ],
)
def test_supported_subset_is_executable_only_after_regeneration(connection, sql):
    accepted = validate_sql(sql, "fleet_daily", max_rows=3)
    assert accepted.dataset_ids == ("fleet_daily",) and accepted.row_limit == 3
    assert accepted.sql != sql
    assert len(connection.execute(accepted.sql).fetchall()) <= 4
    parsed = connection.extract_statements(accepted.sql)
    assert len(parsed) == 1 and parsed[0].type == duckdb.StatementType.SELECT


def test_pagination_has_a_hard_sentinel_cap_and_deterministic_tie_breakers(connection):
    sql = "SELECT period FROM fleet_daily ORDER BY outage_mw ASC"
    first = validate_sql(sql, "fleet_daily", max_rows=2)
    second = validate_sql(sql, "fleet_daily", max_rows=2, offset=2)
    rows = connection.execute(first.sql).fetchall()
    assert rows == [(date(2026, 9, 6),), (date(2026, 9, 1),), (date(2026, 9, 3),)]
    assert connection.execute(second.sql).fetchall() == [
        (date(2026, 9, 3),),
        (date(2026, 9, 5),),
        (date(2026, 9, 2),),
    ]
    assert first.row_limit == 2  # The third row is the caller's has_more sentinel.
    default = validate_sql("SELECT period FROM fleet_daily", "fleet_daily", max_rows=2)
    assert connection.execute(default.sql).fetchall() == [
        (date(2026, 9, 1),),
        (date(2026, 9, 2),),
        (date(2026, 9, 3),),
    ]


def test_user_limit_is_retained_inside_the_outer_server_cap(connection):
    accepted = validate_sql("SELECT period FROM fleet_daily LIMIT 1", "fleet_daily", max_rows=3)
    assert accepted.row_limit == 3
    assert connection.execute(accepted.sql).fetchall() == [(date(2026, 9, 1),)]
    assert (
        connection.execute(
            validate_sql("SELECT * FROM fleet_daily LIMIT 0", "fleet_daily").sql
        ).fetchall()
        == []
    )


def test_quoted_case_insensitive_identifiers_are_canonicalized(connection):
    accepted = validate_sql(
        'SELECT "F"."PERIOD" AS "DAY" FROM "FLEET_DAILY" AS "F"', "FLEET_DAILY", max_rows=1
    )
    assert accepted.dataset_ids == ("fleet_daily",)
    assert '"fleet_daily"' in accepted.sql and '"period"' in accepted.sql
    assert connection.execute(accepted.sql).fetchall() == [(date(2026, 9, 1),), (date(2026, 9, 2),)]
    assert connection.description[0][0] == "day"


def test_literals_and_quoted_aliases_cannot_inject_statements(connection):
    sql = """SELECT 'x; COPY generator_daily TO ''secret.csv''; --' AS "safe; DROP TABLE fleet_daily; --" FROM fleet_daily LIMIT 1"""
    accepted = validate_sql(sql, "fleet_daily")
    assert (
        connection.execute(accepted.sql).fetchone()[0]
        == "x; COPY generator_daily TO 'secret.csv'; --"
    )
    assert connection.execute("SELECT count(*) FROM fleet_daily").fetchone()[0] == 6


def test_normal_comments_are_removed_from_executable_sql(connection):
    accepted = validate_sql(
        "SELECT /* comment */ period FROM fleet_daily -- second comment\n", "fleet_daily"
    )
    assert "comment" not in accepted.sql and "--" not in accepted.sql and "/*" not in accepted.sql
    assert len(connection.execute(accepted.sql).fetchall()) == 6


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM fleet_daily; SELECT * FROM generator_daily",
        "SELECT * FROM fleet_daily; COPY fleet_daily TO 'file.csv'",
        "DELETE FROM fleet_daily",
        "UPDATE fleet_daily SET outage_mw=0",
        "INSERT INTO fleet_daily SELECT * FROM generator_daily",
        "DROP TABLE fleet_daily",
        "ATTACH 'secret.db'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA database_list",
        "SET threads=100",
        "COPY (SELECT * FROM fleet_daily) TO 'file.csv'",
        "EXPLAIN SELECT * FROM fleet_daily",
        "SELECT * FROM read_parquet('D:/private/file.parquet')",
        "SELECT * FROM read_csv_auto('https://example.com/private.csv')",
        "SELECT * FROM parquet_scan('D:/private/*.parquet')",
        "SELECT * FROM sqlite_scan('private.db','secrets')",
        "SELECT * FROM query_table('generator_daily')",
        "SELECT * FROM 'D:/private/data.parquet'",
        "SELECT read_blob('D:/private/file') FROM fleet_daily",
        "SELECT getenv('EIA_API_KEY') FROM fleet_daily",
        "SELECT current_setting('external_access') FROM fleet_daily",
        "SELECT current_database() FROM fleet_daily",
        "SELECT repeat('x',1000000000) FROM fleet_daily",
        "SELECT version() FROM fleet_daily",
        "SELECT * FROM main.fleet_daily",
        "SELECT * FROM memory.main.fleet_daily",
        "SELECT main.fleet_daily.period FROM fleet_daily",
        "SELECT * FROM fleet_daily f JOIN fleet_daily g ON f.period=g.period",
        "SELECT * FROM fleet_daily f JOIN generator_daily g ON f.period=g.period",
        "SELECT * FROM (SELECT * FROM fleet_daily) q",
        "SELECT (SELECT count(*) FROM fleet_daily) FROM fleet_daily",
        "SELECT * FROM fleet_daily WHERE EXISTS(SELECT 1 FROM generator_daily)",
        "WITH hidden AS(SELECT * FROM generator_daily) SELECT * FROM fleet_daily",
        "SELECT * FROM fleet_daily UNION ALL SELECT * FROM fleet_daily",
        "SELECT row_number() OVER(ORDER BY period) FROM fleet_daily",
        "SELECT SUM(outage_mw) FILTER(WHERE capacity_mw>0) FROM fleet_daily",
        "SELECT * FROM fleet_daily USING SAMPLE 5",
        "SELECT * EXCLUDE(quality_flags) FROM fleet_daily",
        "SELECT DISTINCT outage_mw FROM fleet_daily",
        "SELECT * FROM UNNEST([1,2,3])",
        "SELECT CASE WHEN outage_mw>0 THEN 1 ELSE 0 END FROM fleet_daily",
        "SELECT CAST(outage_mw AS STRUCT(secret VARCHAR)) FROM fleet_daily",
        "SELECT CAST(outage_mw AS DECIMAL(39,0)) FROM fleet_daily",
        "SELECT COLUMNS('.*') FROM fleet_daily",
        "SELECT * FROM fleet_daily ORDER BY period COLLATE nocase",
        "SELECT * FROM fleet_daily LIMIT $count",
        "SELECT * FROM fleet_daily LIMIT 2 OFFSET 1",
        "SELECT period FROM fleet_daily FOR UPDATE",
        "SELECT * FROM fleet_daily AS f(a,b,c,d,e,f,g,h)",
        "SELECT no_such_column FROM fleet_daily",
        "SELECT period FROM fleet_daily ORDER BY no_such_column",
        "SELECT f.period FROM fleet_daily AS g",
        "SELECT capacity_mw AS x FROM fleet_daily WHERE x>0",
        "SELECT 1",
    ],
)
def test_unapproved_capabilities_are_rejected_without_data_access(sql):
    with pytest.raises(SQLPolicyError):
        validate_sql(sql, "fleet_daily")


@pytest.mark.parametrize(
    "sql,expected",
    [
        ('SELECT * FROM "FLEET_DAILY"', {"fleet_daily"}),
        (
            "SELECT * FROM fleet_daily WHERE EXISTS(SELECT 1 FROM generator_daily)",
            {"fleet_daily", "generator_daily"},
        ),
        (
            "WITH hidden AS(SELECT * FROM generator_daily) SELECT * FROM hidden JOIN fleet_daily ON true",
            {"fleet_daily", "generator_daily", "hidden"},
        ),
        ("SELECT * FROM main.generator_daily", {"generator_daily"}),
        ("SELECT * FROM unknown_private_table", {"unknown_private_table"}),
        ("SELECT 'generator_daily' FROM fleet_daily", {"fleet_daily"}),
        ("SELECT * FROM read_parquet('private.parquet')", set()),
    ],
)
def test_reference_discovery_includes_all_named_scopes_without_authorizing_them(sql, expected):
    assert set(referenced_dataset_ids(sql)) == expected


def test_nested_cross_dataset_reference_is_rejected_before_structural_validation(monkeypatch):
    def structural_validation_must_not_run(*args):
        pytest.fail("The selected dataset constraint must precede AST feature validation.")

    monkeypatch.setattr(sql_policy, "_validate_nodes", structural_validation_must_not_run)
    with pytest.raises(SQLPolicyError) as error:
        validate_sql(
            "SELECT * FROM fleet_daily WHERE EXISTS(SELECT 1 FROM generator_daily)", "fleet_daily"
        )
    assert error.value.code == "dataset_mismatch"


@pytest.mark.parametrize(
    "options",
    [
        {"max_rows": 0},
        {"max_rows": 1001},
        {"max_rows": True},
        {"offset": -1},
        {"offset": 100001},
        {"offset": True},
    ],
)
def test_server_pagination_bounds_are_strict_and_reject_booleans(options):
    with pytest.raises(SQLPolicyError) as error:
        validate_sql("SELECT * FROM fleet_daily", "fleet_daily", **options)
    assert error.value.code == "invalid_pagination"


@pytest.mark.parametrize(
    "sql,code",
    [
        ("SELECT '" + "λ" * 9000 + "' FROM fleet_daily", "sql_too_large"),
        ("SELECT '" + "a" * 1025 + "' FROM fleet_daily", "sql_too_complex"),
        ("SELECT " + "(" * 33 + "1" + ")" * 33 + " FROM fleet_daily", "sql_too_complex"),
        (
            "SELECT * FROM fleet_daily WHERE outage_mw IN("
            + ",".join("1" for _ in range(1100))
            + ")",
            "sql_too_complex",
        ),
        ("SELECT " + ",".join("1" for _ in range(600)) + " FROM fleet_daily", "sql_too_complex"),
        ("SELECT capacity_mw" + "+1" * 40 + " FROM fleet_daily", "sql_too_complex"),
        ("SELECT 1e999999 FROM fleet_daily", "sql_too_complex"),
        ("SELECT 0e999999 FROM fleet_daily", "sql_too_complex"),
        ("SELECT 1.123456789012345678901234567890123456789 FROM fleet_daily", "sql_too_complex"),
        ("SELECT 1 AS " + "x" * 129 + " FROM fleet_daily", "sql_too_complex"),
        ("SELECT ROUND(capacity_mw,1000000000) FROM fleet_daily", "unsupported_sql"),
    ],
)
def test_resource_abuse_has_bounded_static_policy_errors(sql, code):
    with pytest.raises(SQLPolicyError) as error:
        validate_sql(sql, "fleet_daily")
    assert error.value.code == code
    assert len(str(error.value)) < 150


def test_preparse_bounds_stop_deep_nesting_before_recursive_parser(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Oversized nesting must not reach SQLGlot's recursive parser.")

    monkeypatch.setattr(sqlglot, "parse", forbidden)
    with pytest.raises(SQLPolicyError) as error:
        referenced_dataset_ids("SELECT " + "(" * 1000 + "1" + ")" * 1000 + " FROM fleet_daily")
    assert error.value.code == "sql_too_complex"


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "-- comment only",
        None,
        "SELECT '\ud800' FROM fleet_daily",
        "SELECT * FROM fleet_daily WHERE (",
    ],
)
def test_malformed_input_returns_policy_errors_without_original_text(sql):
    with pytest.raises(SQLPolicyError) as error:
        validate_sql(sql, "fleet_daily")
    assert error.value.code == "invalid_sql"
    assert "fleet_daily" not in str(error.value)


def test_policy_never_returns_or_logs_attacker_sql_in_parse_errors(caplog):
    canary = "attacker-secret-canary"
    with pytest.raises(SQLPolicyError) as error:
        validate_sql(f"SELECT * FROM fleet_daily WHERE ('{canary}'", "fleet_daily")
    assert canary not in str(error.value) + caplog.text


def test_duckdb_ignored_varchar_width_is_erased_from_regenerated_sql(connection):
    accepted = validate_sql(
        "SELECT CAST(capacity_mw AS VARCHAR(1000000000)) FROM fleet_daily", "fleet_daily"
    )
    assert "1000000000" not in accepted.sql
    assert connection.execute(accepted.sql).fetchone()[0] == "100.000000000"


def test_regenerated_sql_is_bounded_independently_of_original_text():
    sql = "SELECT " + ",".join("'" + "x" * 1015 + "'" for _ in range(16)) + " FROM fleet_daily"
    assert len(sql.encode()) < sql_policy.MAX_SQL_BYTES
    with pytest.raises(SQLPolicyError) as error:
        validate_sql(sql, "fleet_daily")
    assert error.value.code == "sql_too_large"


def test_selected_dataset_must_itself_be_a_catalog_id():
    with pytest.raises(SQLPolicyError) as error:
        validate_sql("SELECT * FROM fleet_daily", "D:/private/file.parquet")
    assert error.value.code == "unknown_dataset"
