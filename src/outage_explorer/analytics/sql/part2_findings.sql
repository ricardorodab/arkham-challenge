-- Execution contract: register one caller-selected immutable model before use.
-- Snapshot identifiers in the historical notes below describe retained evidence;
-- they do not pin execution. Statements run in trusted native DuckDB, outside
-- the user-SQL subset. Comment changes alter future full-text report hashes;
-- historical receipts and previously recorded hashes remain unchanged.

-- Independent read-only evidence queries over model version 2 base facts.
-- Source snapshot: 981a8f4ed5214ef6be4ad3cc5b4eee01.
-- Names and flags are preserved source/model observations, not physical causes.

-- name: zero_capacity_summary
-- Purpose/grain: one composite generator identity with at least one reported
-- zero-capacity day; summarize its denominator anomaly and reported percentages.
-- Inputs/dates: generator_daily across the entire registered model, not $start/$end.
-- Clauses: equality selects zero capacity, then GROUP BY facility/generator keeps
-- locally scoped generator IDs distinct; MIN/MAX dates bound the observed subset.
-- Null/decimal policy: zero differs from null, so null capacity is not selected.
-- COUNT FILTER excludes null reported percentages; MIN/MAX ignore them. DECIMAL
-- percentages remain source observations and no derived division is attempted.
-- Output/limits: first/last selected date, day count, nonzero count and percentage
-- bounds, ordered by identity. No SQL LIMIT. MIN/MAX alone do not prove one
-- continuous episode and do not establish the physical reason for zero capacity.
-- F1: zero-denominator episode and its independently reported percentages.
SELECT facility_id, generator_id, min(period) AS first_zero_capacity,
    max(period) AS last_zero_capacity, count(*) AS zero_capacity_days,
    count(*) FILTER (WHERE reported_outage_pct <> 0) AS nonzero_reported_pct_days,
    min(reported_outage_pct) AS min_reported_pct, max(reported_outage_pct) AS max_reported_pct
FROM generator_daily WHERE capacity_mw = 0
GROUP BY facility_id, generator_id ORDER BY facility_id, generator_id;

-- name: indian_point_evidence
-- Purpose/grain: selected generator/day source rows around Indian Point 2's
-- zero-denominator episode, retaining the independently published parent percent.
-- Inputs/dates: generator_daily left-joined to facility_daily for facility 2497,
-- generator 2 on literal 2019-12-31, 2020-01-01, 2020-03-30 and 2020-03-31.
-- Clauses: composite date/facility join avoids mixing a parent's observation date;
-- LEFT JOIN preserves the generator if a facility-day observation is absent.
-- Null/decimal policy: source DECIMAL values/flags are unchanged. CASE derives a
-- display percentage only for positive capacity; zero/negative/null denominator
-- or null outage yields null. Parent percentage can independently be null.
-- Output/limits: dated operands, both published percentages, derived display value
-- and flags. No SQL LIMIT; literal dates override report-window scope. These rows
-- cannot prove a retirement, missing capacity value or physical causal account.
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
-- Purpose/grain: one summary of presence over River Bend's 120-day audit calendar.
-- Inputs/dates: facility_daily and generator_daily checked for facility 6462 and
-- generator 1 over literal 2013-01-01 through 2013-04-30, inclusive.
-- Clauses: generate_series creates an evidence scaffold; correlated EXISTS tests
-- each grain independently without multiplying dates or adding fact observations.
-- Null/decimal policy: presence booleans and missing counts describe row absence,
-- not null MW, zero outage or inferred capacity. No decimal arithmetic is used.
-- Output/limits: calendar start/end/size and each grain's absent-day count; no SQL
-- LIMIT and no report-window parameters. The calendar is scoped evidence, not an
-- authoritative commissioning/retirement roster or explanation of the omission.
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
-- Purpose/grain: four selected generator/day observations around the River Bend
-- gap, showing the actual boundary capacity/outage without estimating missing days.
-- Inputs/dates: generator_daily, composite identity 6462/1, literal 2012-12-30,
-- 2012-12-31, 2013-05-01 and 2013-05-02; report $start/$end are unused.
-- Clauses: exact identity/date predicates followed by chronological ordering; no
-- join or generated calendar can introduce a replacement observation.
-- Null/decimal policy: source DECIMAL measures and nulls pass through unchanged;
-- no division, forward fill, interpolation or zero substitution is performed.
-- Output/limits: observed identity/name/date and capacity/outage/percentage. No
-- SQL LIMIT; fewer rows can mean the selected source dates are themselves absent.
-- Boundary measurements do not determine values or operating status inside a gap.
-- F4: boundary rows surrounding that gap; no forward fill across the interval.
SELECT period, facility_id, generator_id, facility_name,
    capacity_mw, outage_mw, reported_outage_pct
FROM generator_daily WHERE facility_id = '6462' AND generator_id = '1'
  AND period IN (DATE '2012-12-30', DATE '2012-12-31', DATE '2013-05-01', DATE '2013-05-02')
ORDER BY period;

-- name: river_bend_aggregate_blind_spot
-- Purpose/grain: one national date showing that matching child sums need not
-- prove expected entity coverage when both children omit the same generator.
-- Inputs/dates: national_daily, facility_daily and generator_daily on literal
-- 2013-01-15; count River Bend facility 6462 and generator 6462/1 explicitly.
-- Clauses: scalar child aggregates CROSS JOIN the selected national row so no
-- many-to-many join can inflate sums. COUNT FILTER exposes entity absence.
-- Null/decimal policy: DECIMAL MW subtraction is exact, but these ordinary SUMs
-- ignore null input; null completeness is assessed by reconciliation queries.
-- No missing entity is assigned zero capacity or inferred into the dataset.
-- Output/limits: national operands, River Bend row counts and signed child-minus-
-- parent gaps. No SQL LIMIT; a missing national row yields no result. Independent
-- operating evidence is required to establish the expected missing membership.
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
-- Purpose/grain: selected national dates with observed generator counts and a
-- lagged national capacity difference across the retained denominator boundary.
-- Inputs/dates: national_daily and generator_daily; child counts restrict the
-- joined dates to literal 2026-09-30 and 2026-10-01, not report $start/$end.
-- Clauses: group generator observations before joining national data; LAG runs
-- over the selected joined dates, so the first output difference is null.
-- Null/decimal policy: exact DECIMAL subtraction preserves capacity deltas; null
-- source capacity remains null. Counts do not prove identical generator members.
-- Output/limits: national measures, count and delta in date order; no SQL LIMIT.
-- Use fleet_identity_change for set equality. This fixed pair is consecutive, but
-- LAG over arbitrary selected dates alone would not establish calendar adjacency.
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
-- Purpose/grain: two set-difference counts proving membership independently from
-- equal row counts around the fleet capacity boundary.
-- Inputs/dates: generator_daily composite (facility_id,generator_id) sets on literal
-- 2026-09-30 and 2026-10-01; report-window parameters do not alter them.
-- Clauses: EXCEPT in each direction identifies removed and added identities;
-- UNION ALL preserves both labeled count rows even when both sets are empty.
-- Null/decimal policy: validated identity keys are non-null text, preserving
-- facility scope/leading zeros. There is no MW/percentage or floating arithmetic.
-- Output/limits: removed/added labels and counts, without SQL LIMIT or an ordering
-- guarantee. Zero in both directions proves equality of observed sets only, not
-- that either set contains every physically eligible reactor.
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
-- Purpose/grain: one full-history summary of observed generator capacity changes,
-- including their concentration on April 1/October 1 and changes across gaps.
-- Inputs/dates: generator_daily across the registered model; no report dates bind.
-- Clauses: LAG partitions by composite generator identity and orders observations
-- by date; the final inequality excludes unchanged/unknown comparisons.
-- Null/decimal policy: DECIMAL capacity comparisons are exact. First observations
-- and null-capacity comparisons are not counted as changes. Observed adjacency is
-- not calendar adjacency; date_diff separately counts changes spanning missing days.
-- Output/limits: total changes, April/October-first count and gap-spanning count.
-- No SQL LIMIT, 100 MW/10 percent threshold or physical-cause classification is
-- applied; seasonal concentration is evidence for review, not a proven API method.
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
-- Purpose/grain: one full-history summary of generator percentage arithmetic.
-- Inputs/dates: generator_daily across the selected model; no $start/$end filter.
-- Clauses: scale capacity/outage/percentage by 10^9, then use filtered counts for
-- cross-product failures, noninteger percentages and zero-capacity exceptions.
-- Null/decimal policy: HUGEINT multiplication/remainder avoids binary division;
-- null comparisons do not increment failure counts, while COUNT(*) still includes
-- their rows. Integer capacity times percentage can match at zero capacity even
-- when the reported percentage is meaningless; the separate zero count exposes it.
-- Output/limits: observed rows and three diagnostic counts. No SQL LIMIT. Bounded
-- HUGEINT arithmetic is not arbitrary precision, and the degenerate identity must
-- not be treated as validation of a derived ratio with a zero denominator.
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
-- Purpose/grain: changed composite generator identities contributing to the
-- selected fleet denominator boundary, including below-threshold changes.
-- Inputs/dates: generator_daily self-joined for literal before 2026-09-30 and after
-- 2026-10-01; $start/$end do not alter the evidence dates.
-- Clauses: join by facility and generator before selecting the two observations;
-- filter exact capacity inequality, then sort largest absolute changes first.
-- Null/decimal policy: DECIMAL subtraction preserves signed MW change; null-capacity
-- comparisons are excluded by SQL unknown, and no missing before/after row is
-- invented. Source outage and published percentages are returned unchanged.
-- Output/limits: identity/name, before/after operands and delta; no SQL LIMIT or
-- 100 MW/10 percent filter. A capacity change is not a new identity or proof of an
-- uprate/restart; use fleet_identity_change to examine observed set membership.
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
-- Purpose/grain: two historical Grand Gulf boundary observations paired with
-- the previous calendar day's observation and the configured threshold diagnostic.
-- Inputs/dates: generator_daily self-join, composite 6072/1, literal after dates
-- 2012-04-01 and 2012-10-01; report-window dates are unused.
-- Clauses: join identity and exact previous day, then select the retained steps;
-- threshold output requires BOTH absolute change >=100 MW and relative >=10 percent.
-- Null/decimal policy: exact DECIMAL differences/products classify the threshold;
-- percentage division is display output. Positive previous capacities are known
-- for these selected rows, not enforced as a general-purpose validator here.
-- Output/limits: before/after source values, signed changes and threshold boolean,
-- ordered by date without SQL LIMIT. Null/missing operands remain unknown/absent;
-- a qualifying step is a review condition rather than a physical explanation.
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
-- Purpose/grain: selected stored generator warning events for the three primary
-- findings; validate that the built model contains events as well as fact evidence.
-- Inputs/dates: data_quality_events, exact identity/date/rule triples for Indian
-- Point 2497/2 on 2020-03-31, River Bend 6462/1 on 2013-05-01 and Grand Gulf 6072/1
-- on 2012-04-01; report $start/$end deliberately do not restrict this probe.
-- Clauses: require generator grain and match the three OR branches, then order
-- chronologically. No raw-fact join or rule regeneration occurs in this statement.
-- Null/decimal policy: retain nullable previous/gap/before/after evidence and exact
-- DECIMAL measures unchanged; different rules may describe related observations.
-- Output/limits: event metadata/evidence and deterministic event_id, no SQL LIMIT.
-- Stored warnings establish rule conditions, not independently verified causes,
-- complete anomaly counts or missing-date facts.
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
