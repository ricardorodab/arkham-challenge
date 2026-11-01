-- Independent read-only evidence queries over model version 2 base facts.
-- Source snapshot: 981a8f4ed5214ef6be4ad3cc5b4eee01.
-- Names and flags are preserved source/model observations, not physical causes.

-- name: zero_capacity_summary
-- F1: zero-denominator episode and its independently reported percentages.
SELECT facility_id, generator_id, min(period) AS first_zero_capacity,
    max(period) AS last_zero_capacity, count(*) AS zero_capacity_days,
    count(*) FILTER (WHERE reported_outage_pct <> 0) AS nonzero_reported_pct_days,
    min(reported_outage_pct) AS min_reported_pct, max(reported_outage_pct) AS max_reported_pct
FROM generator_daily WHERE capacity_mw = 0
GROUP BY facility_id, generator_id ORDER BY facility_id, generator_id;

-- name: indian_point_evidence
-- F2: concrete Indian Point rows, including the preceding positive denominator.
SELECT g.period, g.facility_id, g.generator_id, g.facility_name,
    g.capacity_mw, g.outage_mw, g.reported_outage_pct AS generator_reported_pct,
    f.reported_outage_pct AS facility_reported_pct,
    CASE WHEN g.capacity_mw > 0 THEN 100 * g.outage_mw / g.capacity_mw END AS derived_outage_pct,
    g.quality_flags
FROM generator_daily g LEFT JOIN facility_daily f
    ON g.period = f.period AND g.facility_id = f.facility_id
WHERE g.facility_id = '2497' AND g.generator_id = '2'
  AND g.period IN (DATE '2019-12-31', DATE '2020-01-01', DATE '2020-03-30', DATE '2020-03-31')
ORDER BY g.period;

-- name: river_bend_coverage_gap
-- F3: count the 120 calendar dates missing from BOTH River Bend source grains.
-- This calendar is an evidence scaffold; it does not manufacture fact rows.
WITH calendar AS (
    SELECT CAST(day AS DATE) AS period
    FROM generate_series(DATE '2013-01-01', DATE '2013-04-30', INTERVAL 1 DAY) AS t(day)
), presence AS (
    SELECT c.period,
        EXISTS (SELECT 1 FROM facility_daily f WHERE f.period = c.period AND f.facility_id = '6462')
            AS facility_present,
        EXISTS (SELECT 1 FROM generator_daily g WHERE g.period = c.period
                AND g.facility_id = '6462' AND g.generator_id = '1') AS generator_present
    FROM calendar c
)
SELECT min(period) AS start_date, max(period) AS end_date, count(*) AS calendar_days,
    count(*) FILTER (WHERE NOT facility_present) AS missing_facility_days,
    count(*) FILTER (WHERE NOT generator_present) AS missing_generator_days
FROM presence;

-- name: river_bend_boundary_rows
-- F4: boundary rows surrounding that gap; no forward fill across the interval.
SELECT period, facility_id, generator_id, facility_name,
    capacity_mw, outage_mw, reported_outage_pct
FROM generator_daily WHERE facility_id = '6462' AND generator_id = '1'
  AND period IN (DATE '2012-12-30', DATE '2012-12-31', DATE '2013-05-01', DATE '2013-05-02')
ORDER BY period;

-- name: river_bend_aggregate_blind_spot
-- F5: aggregate agreement can coexist with a missing operating entity.
WITH f AS (
    SELECT sum(capacity_mw) AS capacity_mw, sum(outage_mw) AS outage_mw,
        count(*) FILTER (WHERE facility_id = '6462') AS river_bend_rows
    FROM facility_daily WHERE period = DATE '2013-01-15'
), g AS (
    SELECT sum(capacity_mw) AS capacity_mw, sum(outage_mw) AS outage_mw,
        count(*) FILTER (WHERE facility_id = '6462' AND generator_id = '1') AS river_bend_rows
    FROM generator_daily WHERE period = DATE '2013-01-15'
)
SELECT n.period, n.capacity_mw AS national_capacity_mw, n.outage_mw AS national_outage_mw,
    f.river_bend_rows AS facility_river_bend_rows, g.river_bend_rows AS generator_river_bend_rows,
    f.capacity_mw - n.capacity_mw AS facility_capacity_gap_mw,
    g.capacity_mw - n.capacity_mw AS generator_capacity_gap_mw,
    f.outage_mw - n.outage_mw AS facility_outage_gap_mw,
    g.outage_mw - n.outage_mw AS generator_outage_gap_mw
FROM national_daily n CROSS JOIN f CROSS JOIN g WHERE n.period = DATE '2013-01-15';

-- name: fleet_capacity_change
-- F6: fleet denominator change with the SAME generator identity set.
WITH generator_counts AS (
    SELECT period, count(*) AS generator_count FROM generator_daily
    WHERE period IN (DATE '2026-09-30', DATE '2026-10-01') GROUP BY period
), days AS (
    SELECT n.*, g.generator_count,
        n.capacity_mw - lag(n.capacity_mw) OVER (ORDER BY n.period) AS capacity_change_mw
    FROM national_daily n JOIN generator_counts g USING (period)
)
SELECT period, capacity_mw, outage_mw, reported_outage_pct,
    generator_count, capacity_change_mw
FROM days ORDER BY period;

-- name: fleet_identity_change
-- F7: same counts alone do not prove identical members; test set equality too.
WITH before_set AS (
    SELECT facility_id, generator_id FROM generator_daily WHERE period = DATE '2026-09-30'
), after_set AS (
    SELECT facility_id, generator_id FROM generator_daily WHERE period = DATE '2026-10-01'
), removed AS (SELECT * FROM before_set EXCEPT SELECT * FROM after_set),
added AS (SELECT * FROM after_set EXCEPT SELECT * FROM before_set)
SELECT 'removed' AS change, count(*) AS generator_count FROM removed
UNION ALL SELECT 'added', count(*) FROM added;

-- name: capacity_change_calendar
-- F8: quantify the historical concentration of capacity changes at season seams.
WITH previous AS (
    SELECT *, lag(period) OVER entity AS previous_period,
        lag(capacity_mw) OVER entity AS previous_capacity_mw
    FROM generator_daily
    WINDOW entity AS (PARTITION BY facility_id, generator_id ORDER BY period)
)
SELECT count(*) AS observed_capacity_changes,
    count(*) FILTER (WHERE strftime(period, '%m-%d') IN ('04-01', '10-01')) AS april_or_october_first_changes,
    count(*) FILTER (WHERE date_diff('day', previous_period, period) > 1) AS changes_across_missing_dates
FROM previous WHERE capacity_mw <> previous_capacity_mw;

-- name: generator_exact_arithmetic
-- F9: exact generator arithmetic in the full source history.
-- A zero denominator makes this identity degenerate, hence the separate counts.
WITH scaled AS (
    SELECT CAST(capacity_mw * 1000000000 AS HUGEINT) AS c,
        CAST(outage_mw * 1000000000 AS HUGEINT) AS o,
        CAST(reported_outage_pct * 1000000000 AS HUGEINT) AS p
    FROM generator_daily
)
SELECT count(*) AS generator_days,
    count(*) FILTER (WHERE c * p <> 100 * o * 1000000000) AS exact_relation_failures,
    count(*) FILTER (WHERE p % 1000000000 <> 0) AS noninteger_reported_percentages,
    count(*) FILTER (WHERE c = 0 AND p <> 0) AS zero_capacity_nonzero_reported_percentage
FROM scaled;

-- name: generator_capacity_change_evidence
-- F10: actual entity values explaining the fleet denominator change.
SELECT a.period, a.facility_id, a.generator_id, a.facility_name,
    b.capacity_mw AS before_capacity_mw, a.capacity_mw AS after_capacity_mw,
    a.capacity_mw - b.capacity_mw AS capacity_change_mw,
    b.outage_mw AS before_outage_mw, a.outage_mw AS after_outage_mw,
    b.reported_outage_pct AS before_reported_pct, a.reported_outage_pct AS after_reported_pct
FROM generator_daily a JOIN generator_daily b
    ON a.facility_id = b.facility_id AND a.generator_id = b.generator_id
WHERE a.period = DATE '2026-10-01' AND b.period = DATE '2026-09-30'
  AND a.capacity_mw <> b.capacity_mw
ORDER BY abs(a.capacity_mw - b.capacity_mw) DESC, a.facility_id, a.generator_id;

-- name: grand_gulf_capacity_change
-- F11: source step changes that exceed BOTH configured review thresholds.
SELECT a.period, b.period AS previous_period, a.facility_id, a.generator_id, a.facility_name,
    b.capacity_mw AS before_capacity_mw, a.capacity_mw AS after_capacity_mw,
    a.capacity_mw - b.capacity_mw AS capacity_change_mw,
    100 * (a.capacity_mw - b.capacity_mw) / b.capacity_mw AS capacity_change_pct,
    b.outage_mw AS before_outage_mw, a.outage_mw AS after_outage_mw,
    b.reported_outage_pct AS before_reported_pct, a.reported_outage_pct AS after_reported_pct,
    abs(a.capacity_mw - b.capacity_mw) >= 100
      AND 10 * abs(a.capacity_mw - b.capacity_mw) >= b.capacity_mw AS exceeds_review_threshold
FROM generator_daily a JOIN generator_daily b
    ON a.facility_id = b.facility_id AND a.generator_id = b.generator_id
   AND b.period = a.period - INTERVAL 1 DAY
WHERE a.facility_id = '6072' AND a.generator_id = '1'
  AND a.period IN (DATE '2012-04-01', DATE '2012-10-01')
ORDER BY a.period;

-- name: automated_quality_event_evidence
-- F12: actual generated warnings for all three primary findings.
SELECT period, grain, facility_id, generator_id, rule_id, severity,
    previous_period, gap_days, before_capacity_mw, after_capacity_mw,
    outage_mw, reported_outage_pct, event_id
FROM data_quality_events
WHERE grain = 'generator' AND (
    (facility_id = '2497' AND generator_id = '2' AND period = DATE '2020-03-31'
        AND rule_id = 'zero_capacity_positive_reported_pct')
 OR (facility_id = '6462' AND generator_id = '1' AND period = DATE '2013-05-01'
        AND rule_id = 'entity_coverage_gap')
 OR (facility_id = '6072' AND generator_id = '1' AND period = DATE '2012-04-01'
        AND rule_id = 'capacity_change')
)
ORDER BY period;
