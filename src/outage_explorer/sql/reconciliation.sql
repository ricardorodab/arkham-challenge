-- Part 2, source snapshot 981a8f4ed5214ef6be4ad3cc5b4eee01, model version 2.
-- Run after the model catalog registers national_daily, facility_daily and
-- generator_daily in DuckDB. Each SELECT is independently executable.
-- Gaps below are CHILD SUM MINUS PUBLISHED PARENT, in MW. NULL is not zero.
-- Division is for display only; exact DECIMAL / HUGEINT arithmetic determines
-- equality and rounding compatibility. $start/$end define the audit window.

-- name: daily_national_reconciliation
-- R1: every calendar day, national versus both child grains (30 rows for Sep 2026).
WITH calendar AS (
    SELECT CAST(day AS DATE) AS period
    FROM generate_series(CAST($start AS DATE), CAST($end AS DATE), INTERVAL 1 DAY) AS t(day)
), facilities AS (
    SELECT period, count(*) AS facility_count,
        count(*) - count(capacity_mw) AS null_capacity_count,
        count(*) - count(outage_mw) AS null_outage_count,
        CASE WHEN count(capacity_mw) = count(*) THEN sum(capacity_mw) END AS capacity_mw,
        CASE WHEN count(outage_mw) = count(*) THEN sum(outage_mw) END AS outage_mw
    FROM facility_daily
    WHERE period BETWEEN CAST($start AS DATE) AND CAST($end AS DATE)
    GROUP BY period
), generators AS (
    SELECT period, count(*) AS generator_count,
        count(*) - count(capacity_mw) AS null_capacity_count,
        count(*) - count(outage_mw) AS null_outage_count,
        CASE WHEN count(capacity_mw) = count(*) THEN sum(capacity_mw) END AS capacity_mw,
        CASE WHEN count(outage_mw) = count(*) THEN sum(outage_mw) END AS outage_mw
    FROM generator_daily
    WHERE period BETWEEN CAST($start AS DATE) AND CAST($end AS DATE)
    GROUP BY period
)
SELECT c.period, n.period IS NOT NULL AS national_present,
    f.facility_count, g.generator_count,
    f.null_capacity_count AS facility_null_capacity_count,
    f.null_outage_count AS facility_null_outage_count,
    g.null_capacity_count AS generator_null_capacity_count,
    g.null_outage_count AS generator_null_outage_count,
    n.capacity_mw AS national_capacity_mw, f.capacity_mw AS facility_capacity_mw,
    g.capacity_mw AS generator_capacity_mw,
    n.outage_mw AS national_outage_mw, f.outage_mw AS facility_outage_mw,
    g.outage_mw AS generator_outage_mw,
    f.capacity_mw - n.capacity_mw AS facility_capacity_gap_mw,
    g.capacity_mw - n.capacity_mw AS generator_capacity_gap_mw,
    f.outage_mw - n.outage_mw AS facility_outage_gap_mw,
    g.outage_mw - n.outage_mw AS generator_outage_gap_mw
FROM calendar c
LEFT JOIN national_daily n ON n.period = c.period
LEFT JOIN facilities f ON f.period = c.period
LEFT JOIN generators g ON g.period = c.period
ORDER BY c.period;

-- name: daily_facility_reconciliation
-- R2: observed facility-day versus its generators (1,650 output rows).
-- FULL JOIN retains a child whose facility-day parent is absent.
WITH generators AS (
    SELECT period, facility_id, count(*) AS generator_count,
        count(*) - count(capacity_mw) AS null_capacity_count,
        count(*) - count(outage_mw) AS null_outage_count,
        count(*) - count(reported_outage_pct) AS null_pct_count,
        CASE WHEN count(capacity_mw) = count(*) THEN sum(capacity_mw) END AS capacity_mw,
        CASE WHEN count(outage_mw) = count(*) THEN sum(outage_mw) END AS outage_mw,
        CASE WHEN count(capacity_mw) = count(*) AND count(reported_outage_pct) = count(*)
             THEN sum(capacity_mw * reported_outage_pct) END AS weighted_pct_numerator
    FROM generator_daily
    WHERE period BETWEEN CAST($start AS DATE) AND CAST($end AS DATE)
    GROUP BY period, facility_id
), facilities AS (
    SELECT * FROM facility_daily
    WHERE period BETWEEN CAST($start AS DATE) AND CAST($end AS DATE)
)
SELECT coalesce(f.period, g.period) AS period,
    coalesce(f.facility_id, g.facility_id) AS facility_id,
    f.facility_name, f.period IS NOT NULL AS facility_present,
    g.generator_count, g.null_capacity_count, g.null_outage_count, g.null_pct_count,
    f.capacity_mw AS facility_capacity_mw, g.capacity_mw AS generator_capacity_mw,
    f.outage_mw AS facility_outage_mw, g.outage_mw AS generator_outage_mw,
    g.capacity_mw - f.capacity_mw AS capacity_gap_mw,
    g.outage_mw - f.outage_mw AS outage_gap_mw,
    f.reported_outage_pct AS facility_reported_pct,
    g.weighted_pct_numerator / nullif(g.capacity_mw, 0) AS generator_weighted_reported_pct,
    g.weighted_pct_numerator / nullif(g.capacity_mw, 0) - f.reported_outage_pct
        AS reported_pct_gap_pp,
    -- This residual is exact and has units MW * percentage points.
    f.capacity_mw * f.reported_outage_pct - 100 * f.outage_mw AS exact_pct_residual_mw_pp
FROM facilities f
FULL JOIN generators g ON f.period = g.period AND f.facility_id = g.facility_id
ORDER BY period, facility_id;

-- name: full_history_mw_summary
-- R3: full-history MW comparison and missing-parent counts, without a tolerance.
WITH generator_facilities AS (
    SELECT period, facility_id, count(*) AS child_count,
        CASE WHEN count(capacity_mw) = count(*) THEN sum(capacity_mw) END AS capacity_mw,
        CASE WHEN count(outage_mw) = count(*) THEN sum(outage_mw) END AS outage_mw
    FROM generator_daily GROUP BY period, facility_id
), facility_comparison AS (
    SELECT f.period AS parent_period, g.period AS child_period,
        g.capacity_mw - f.capacity_mw AS capacity_gap_mw,
        g.outage_mw - f.outage_mw AS outage_gap_mw
    FROM facility_daily f FULL JOIN generator_facilities g
        ON f.period = g.period AND f.facility_id = g.facility_id
), all_facilities AS (
    SELECT period,
        CASE WHEN count(capacity_mw) = count(*) THEN sum(capacity_mw) END AS capacity_mw,
        CASE WHEN count(outage_mw) = count(*) THEN sum(outage_mw) END AS outage_mw
    FROM facility_daily GROUP BY period
), all_generators AS (
    SELECT period,
        CASE WHEN count(capacity_mw) = count(*) THEN sum(capacity_mw) END AS capacity_mw,
        CASE WHEN count(outage_mw) = count(*) THEN sum(outage_mw) END AS outage_mw
    FROM generator_daily GROUP BY period
), national_facilities AS (
    SELECT n.period AS parent_period, f.period AS child_period,
        f.capacity_mw - n.capacity_mw AS capacity_gap_mw,
        f.outage_mw - n.outage_mw AS outage_gap_mw
    FROM national_daily n FULL JOIN all_facilities f ON n.period = f.period
), national_generators AS (
    SELECT n.period AS parent_period, g.period AS child_period,
        g.capacity_mw - n.capacity_mw AS capacity_gap_mw,
        g.outage_mw - n.outage_mw AS outage_gap_mw
    FROM national_daily n FULL JOIN all_generators g ON n.period = g.period
), comparisons AS (
    SELECT 'generator_to_facility' AS comparison, * FROM facility_comparison
    UNION ALL SELECT 'facility_to_national', * FROM national_facilities
    UNION ALL SELECT 'generator_to_national', * FROM national_generators
)
SELECT comparison, count(*) AS compared_keys,
    count(*) FILTER (WHERE parent_period IS NULL) AS missing_parent_keys,
    count(*) FILTER (WHERE child_period IS NULL) AS missing_child_keys,
    count(*) FILTER (WHERE capacity_gap_mw IS NULL OR outage_gap_mw IS NULL) AS uncomparable_keys,
    count(*) FILTER (WHERE capacity_gap_mw <> 0 OR outage_gap_mw <> 0) AS nonzero_mw_gap_keys,
    max(abs(capacity_gap_mw)) AS max_capacity_abs_gap_mw,
    max(abs(outage_gap_mw)) AS max_outage_abs_gap_mw
FROM comparisons GROUP BY comparison ORDER BY comparison;

-- name: exact_percentage_rounding_summary
-- R4: exact source rounding proof for all 30 national + 1,650 facility rows.
-- C, O, P are source capacity, outage and percentage multiplied by 10^9.
-- round(100*O/C, 2) in percentage points, HALF_UP for nonnegative values,
-- is floor((2*10000*O + C)/(2*C)) / 100.
-- Integer division keeps this classification independent of binary floats.
WITH source AS (
    SELECT 'national' AS grain, period, capacity_mw, outage_mw, reported_outage_pct
    FROM national_daily WHERE period BETWEEN CAST($start AS DATE) AND CAST($end AS DATE)
    UNION ALL
    SELECT 'facility', period, capacity_mw, outage_mw, reported_outage_pct
    FROM facility_daily WHERE period BETWEEN CAST($start AS DATE) AND CAST($end AS DATE)
), scaled AS (
    SELECT *, CAST(capacity_mw * 1000000000 AS HUGEINT) AS c,
        CAST(outage_mw * 1000000000 AS HUGEINT) AS o,
        CAST(reported_outage_pct * 1000000000 AS HUGEINT) AS p
    FROM source
)
SELECT grain, count(*) AS observations,
    count(*) FILTER (WHERE c IS NULL OR c <= 0 OR o IS NULL OR o < 0 OR p IS NULL)
        AS uncomparable_observations,
    count(*) FILTER (WHERE c > 0 AND o >= 0
        AND p <> ((2 * 10000 * o + c) // (2 * c)) * 10000000)
        AS two_decimal_half_up_failures,
    count(*) FILTER (WHERE c > 0 AND p * c <> 100 * o * 1000000000)
        AS published_pct_differs_from_exact_ratio
FROM scaled GROUP BY grain ORDER BY grain;

-- name: percentage_gap_example
-- R5: a concrete cross-grain percentage comparison with exact integer residuals.
WITH f AS (
    SELECT period, sum(capacity_mw) AS capacity_mw,
        sum(CAST(capacity_mw * 1000000000 AS HUGEINT)
          * CAST(reported_outage_pct * 1000000000 AS HUGEINT)) AS weighted_p
    FROM facility_daily WHERE period = DATE '2026-09-03' GROUP BY period
), g AS (
    SELECT period, sum(capacity_mw) AS capacity_mw,
        sum(CAST(capacity_mw * 1000000000 AS HUGEINT)
          * CAST(reported_outage_pct * 1000000000 AS HUGEINT)) AS weighted_p
    FROM generator_daily WHERE period = DATE '2026-09-03' GROUP BY period
), n AS (
    SELECT *, CAST(capacity_mw * 1000000000 AS HUGEINT) AS c,
        CAST(outage_mw * 1000000000 AS HUGEINT) AS o,
        CAST(reported_outage_pct * 1000000000 AS HUGEINT) AS p
    FROM national_daily WHERE period = DATE '2026-09-03'
)
SELECT n.period, n.capacity_mw, n.outage_mw, n.reported_outage_pct,
    100 * n.outage_mw / n.capacity_mw AS calculated_outage_pct,
    f.weighted_p / (n.c * 1000000000) AS facility_weighted_reported_pct,
    g.weighted_p / (n.c * 1000000000) AS generator_weighted_reported_pct,
    n.p * n.c - 100 * n.o * 1000000000 AS national_exact_scaled_residual,
    f.weighted_p - g.weighted_p AS child_exact_scaled_residual,
    n.reported_outage_pct - 100 * n.outage_mw / n.capacity_mw AS national_reported_minus_calculated_pp,
    (f.weighted_p - g.weighted_p) / (n.c * 1000000000) AS facility_minus_generator_weighted_pp
FROM n JOIN f USING (period) JOIN g USING (period);

-- name: mw_discrepancies
-- R6: actual nonzero/incomplete MW comparisons in the requested window.
-- The result is empty for September 2026, rather than fabricated examples.
WITH gf AS (
    SELECT period, facility_id,
        CASE WHEN count(capacity_mw) = count(*) THEN sum(capacity_mw) END AS capacity_mw,
        CASE WHEN count(outage_mw) = count(*) THEN sum(outage_mw) END AS outage_mw
    FROM generator_daily WHERE period BETWEEN CAST($start AS DATE) AND CAST($end AS DATE)
    GROUP BY period, facility_id
), f AS (
    SELECT * FROM facility_daily
    WHERE period BETWEEN CAST($start AS DATE) AND CAST($end AS DATE)
), af AS (
    SELECT period,
        CASE WHEN count(capacity_mw) = count(*) THEN sum(capacity_mw) END AS capacity_mw,
        CASE WHEN count(outage_mw) = count(*) THEN sum(outage_mw) END AS outage_mw
    FROM f GROUP BY period
), ag AS (
    SELECT period,
        CASE WHEN count(capacity_mw) = count(*) THEN sum(capacity_mw) END AS capacity_mw,
        CASE WHEN count(outage_mw) = count(*) THEN sum(outage_mw) END AS outage_mw
    FROM generator_daily WHERE period BETWEEN CAST($start AS DATE) AND CAST($end AS DATE)
    GROUP BY period
), n AS (
    SELECT * FROM national_daily
    WHERE period BETWEEN CAST($start AS DATE) AND CAST($end AS DATE)
), comparison AS (
    SELECT 'generator_to_facility' AS grain, coalesce(f.period, gf.period) AS period,
        coalesce(f.facility_id, gf.facility_id) AS facility_id,
        f.capacity_mw AS parent_capacity_mw, gf.capacity_mw AS child_capacity_mw,
        f.outage_mw AS parent_outage_mw, gf.outage_mw AS child_outage_mw
    FROM f FULL JOIN gf ON f.period = gf.period AND f.facility_id = gf.facility_id
    UNION ALL
    SELECT 'facility_to_national', coalesce(n.period, af.period), NULL,
        n.capacity_mw, af.capacity_mw, n.outage_mw, af.outage_mw
    FROM n FULL JOIN af ON n.period = af.period
    UNION ALL
    SELECT 'generator_to_national', coalesce(n.period, ag.period), NULL,
        n.capacity_mw, ag.capacity_mw, n.outage_mw, ag.outage_mw
    FROM n FULL JOIN ag ON n.period = ag.period
)
SELECT *, child_capacity_mw - parent_capacity_mw AS capacity_gap_mw,
    child_outage_mw - parent_outage_mw AS outage_gap_mw
FROM comparison
WHERE parent_capacity_mw IS NULL OR child_capacity_mw IS NULL
   OR parent_outage_mw IS NULL OR child_outage_mw IS NULL
   OR parent_capacity_mw <> child_capacity_mw OR parent_outage_mw <> child_outage_mw
ORDER BY period, grain, facility_id;
