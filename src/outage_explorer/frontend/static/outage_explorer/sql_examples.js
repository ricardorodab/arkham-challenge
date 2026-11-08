/**
 * @module sql_examples
 * Supply curated read-only examples for an authorized analytical catalog entry.
 *
 * These templates are starting points, independent of executed-query history. Every
 * statement uses one fixed logical source and the supported SELECT language. The
 * caller owns catalog authorization; the server remains authoritative for execution.
 */
import { t } from './i18n.js';

/** Construct a detached example definition with readable, bounded SQL text. */
const example = (level, title, description, sql) => ({
  id: level,
  level,
  title,
  description,
  sql: sql.trim(),
});

const EXAMPLES = {
  dim_date: {
    columns: ['period', 'year', 'month', 'day'],
    items: [
      example(
        'easy',
        'Browse observed dates',
        'Inspect the latest observed calendar dates.',
        `
SELECT "period", "year", "month", "day"
FROM "dim_date"
ORDER BY "period" DESC
LIMIT 100`,
      ),
      example(
        'medium',
        'Find summer observations',
        'Filter to June through August across the available years.',
        `
SELECT "period", "year", "month"
FROM "dim_date"
WHERE "month" BETWEEN 6 AND 8
ORDER BY "period" DESC
LIMIT 100`,
      ),
      example(
        'hard',
        'Find months with broad date coverage',
        'Summarize months with at least 20 observed dates; this does not establish complete coverage.',
        `
SELECT "year", "month", COUNT(*) AS "observed_dates",
       MIN("period") AS "first_date", MAX("period") AS "last_date"
FROM "dim_date"
GROUP BY "year", "month"
HAVING COUNT(*) >= 20
ORDER BY "year" DESC, "month" DESC
LIMIT 100`,
      ),
    ],
  },
  dim_facility: {
    columns: [
      'facility_id',
      'facility_name',
      'first_observed',
      'last_observed',
      'seen_in_facility_route',
      'seen_in_generator_route',
    ],
    items: [
      example(
        'easy',
        'Browse the facility directory',
        'List facility identities and their latest observed names.',
        `
SELECT "facility_id", "facility_name", "first_observed", "last_observed"
FROM "dim_facility"
ORDER BY "facility_id"
LIMIT 100`,
      ),
      example(
        'medium',
        'Find generator-only facilities',
        'Inspect identities seen in generator data but absent from the facility route.',
        `
SELECT "facility_id", "facility_name", "first_observed", "last_observed"
FROM "dim_facility"
WHERE "seen_in_generator_route" = TRUE AND "seen_in_facility_route" = FALSE
ORDER BY "last_observed" DESC, "facility_id"
LIMIT 100`,
      ),
      example(
        'hard',
        'Compare source coverage groups',
        'Group facilities by which source routes observed them.',
        `
SELECT "seen_in_facility_route", "seen_in_generator_route",
       COUNT(*) AS "facilities", MIN("first_observed") AS "first_seen",
       MAX("last_observed") AS "last_seen"
FROM "dim_facility"
GROUP BY "seen_in_facility_route", "seen_in_generator_route"
ORDER BY "facilities" DESC
LIMIT 100`,
      ),
    ],
  },
  dim_generator: {
    columns: ['facility_id', 'generator_id', 'first_observed', 'last_observed'],
    items: [
      example(
        'easy',
        'Browse generator identities',
        'Generator identity uses both the facility and generator IDs.',
        `
SELECT "facility_id", "generator_id", "first_observed", "last_observed"
FROM "dim_generator"
ORDER BY "facility_id", "generator_id"
LIMIT 100`,
      ),
      example(
        'medium',
        'Find generators observed on multiple dates',
        'Compare each identity’s first and last observed dates.',
        `
SELECT "facility_id", "generator_id", "first_observed", "last_observed"
FROM "dim_generator"
WHERE "last_observed" > "first_observed"
ORDER BY "last_observed" DESC, "facility_id", "generator_id"
LIMIT 100`,
      ),
      example(
        'hard',
        'Count generators by facility',
        'Find facilities with multiple observed generator identities.',
        `
SELECT "facility_id", COUNT(*) AS "generator_count",
       MIN("first_observed") AS "first_seen", MAX("last_observed") AS "last_seen"
FROM "dim_generator"
GROUP BY "facility_id"
HAVING COUNT(*) > 1
ORDER BY "generator_count" DESC, "facility_id"
LIMIT 100`,
      ),
    ],
  },
  national_daily: {
    columns: ['period', 'capacity_mw', 'outage_mw', 'reported_outage_pct'],
    items: [
      example(
        'easy',
        'Inspect recent national observations',
        'Read published capacity and outage measurements by date.',
        `
SELECT "period", "capacity_mw", "outage_mw", "reported_outage_pct"
FROM "national_daily"
ORDER BY "period" DESC
LIMIT 100`,
      ),
      example(
        'medium',
        'Rank days with reported outages',
        'Keep positive reported outage measurements and rank their MW values.',
        `
SELECT "period", "capacity_mw", "outage_mw", "reported_outage_pct"
FROM "national_daily"
WHERE "outage_mw" > 0 AND "capacity_mw" > 0
ORDER BY "outage_mw" DESC, "period" DESC
LIMIT 100`,
      ),
      example(
        'hard',
        'Group days by rounded reported outage',
        'Compare whole-percent reporting bands with at least two observations.',
        `
SELECT ROUND("reported_outage_pct", 0) AS "reported_pct_band",
       COUNT(*) AS "observed_days", COUNT("outage_mw") AS "days_with_outage_mw",
       ROUND(AVG("outage_mw"), 3) AS "average_outage_mw"
FROM "national_daily"
WHERE "reported_outage_pct" IS NOT NULL
GROUP BY ROUND("reported_outage_pct", 0)
HAVING COUNT(*) >= 2
ORDER BY "observed_days" DESC, "reported_pct_band" DESC
LIMIT 100`,
      ),
    ],
  },
  facility_daily: {
    columns: [
      'period',
      'facility_id',
      'facility_name',
      'capacity_mw',
      'outage_mw',
      'reported_outage_pct',
    ],
    items: [
      example(
        'easy',
        'Inspect recent facility observations',
        'Read daily measurements with observation-time facility names.',
        `
SELECT "period", "facility_id", "facility_name", "capacity_mw", "outage_mw"
FROM "facility_daily"
ORDER BY "period" DESC, "facility_id"
LIMIT 100`,
      ),
      example(
        'medium',
        'Find high reported outage observations',
        'Inspect facility-days with reported outages of at least 50 percent.',
        `
SELECT "period", "facility_id", "facility_name", "outage_mw", "reported_outage_pct"
FROM "facility_daily"
WHERE "reported_outage_pct" >= 50
ORDER BY "reported_outage_pct" DESC, "period" DESC
LIMIT 100`,
      ),
      example(
        'hard',
        'Summarize outages by facility',
        'Rank facilities with positive observed outages; averages exclude missing measurements.',
        `
SELECT "facility_id", COUNT(*) AS "observed_days",
       COUNT("outage_mw") AS "days_with_outage_mw",
       ROUND(AVG("outage_mw"), 3) AS "average_outage_mw",
       MAX("outage_mw") AS "peak_outage_mw"
FROM "facility_daily"
GROUP BY "facility_id"
HAVING MAX("outage_mw") > 0
ORDER BY "average_outage_mw" DESC, "facility_id"
LIMIT 100`,
      ),
    ],
  },
  generator_daily: {
    columns: [
      'period',
      'facility_id',
      'generator_id',
      'capacity_mw',
      'outage_mw',
      'reported_outage_pct',
    ],
    items: [
      example(
        'easy',
        'Inspect recent generator observations',
        'Read daily generator measurements using their full identities.',
        `
SELECT "period", "facility_id", "generator_id", "capacity_mw", "outage_mw"
FROM "generator_daily"
ORDER BY "period" DESC, "facility_id", "generator_id"
LIMIT 100`,
      ),
      example(
        'medium',
        'Find fully reported generator outages',
        'Inspect observations reporting at least 100 percent outage, preserving outliers.',
        `
SELECT "period", "facility_id", "generator_id", "outage_mw", "reported_outage_pct"
FROM "generator_daily"
WHERE "reported_outage_pct" >= 100
ORDER BY "reported_outage_pct" DESC, "period" DESC
LIMIT 100`,
      ),
      example(
        'hard',
        'Summarize outages by generator',
        'Group by both identity fields and compare positive observed outage peaks.',
        `
SELECT "facility_id", "generator_id", COUNT(*) AS "observed_days",
       COUNT("outage_mw") AS "days_with_outage_mw",
       ROUND(AVG("outage_mw"), 3) AS "average_outage_mw",
       MAX("outage_mw") AS "peak_outage_mw"
FROM "generator_daily"
GROUP BY "facility_id", "generator_id"
HAVING MAX("outage_mw") > 0
ORDER BY "average_outage_mw" DESC, "facility_id", "generator_id"
LIMIT 100`,
      ),
    ],
  },
  fleet_daily: {
    columns: ['period', 'capacity_mw', 'outage_mw', 'offline_pct'],
    items: [
      example(
        'easy',
        'Inspect recent fleet offline capacity',
        'Read the national fleet’s daily offline capacity and calculated percentage.',
        `
SELECT "period", "capacity_mw", "outage_mw", "offline_pct"
FROM "fleet_daily"
ORDER BY "period" DESC
LIMIT 100`,
      ),
      example(
        'medium',
        'Rank days with at least 10 percent offline',
        'Filter calculated offline percentages before ranking days.',
        `
SELECT "period", "capacity_mw", "outage_mw", "offline_pct"
FROM "fleet_daily"
WHERE "offline_pct" >= 10
ORDER BY "offline_pct" DESC, "period" DESC
LIMIT 100`,
      ),
      example(
        'hard',
        'Compare rounded offline-percentage bands',
        'Count days in whole-percent bands with at least two observations; this is not an average fleet ratio.',
        `
SELECT ROUND("offline_pct", 0) AS "offline_pct_band",
       COUNT(*) AS "observed_days", COUNT("outage_mw") AS "days_with_outage_mw",
       ROUND(AVG("outage_mw"), 3) AS "average_outage_mw"
FROM "fleet_daily"
WHERE "offline_pct" IS NOT NULL
GROUP BY ROUND("offline_pct", 0)
HAVING COUNT(*) >= 2
ORDER BY "observed_days" DESC, "offline_pct_band" DESC
LIMIT 100`,
      ),
    ],
  },
  facility_reconciliation_daily: {
    columns: [
      'period',
      'facility_id',
      'coverage_status',
      'mw_comparison_status',
      'outage_gap_mw',
      'outage_abs_gap_mw',
    ],
    items: [
      example(
        'easy',
        'Inspect recent facility comparisons',
        'Review source coverage and facility-versus-generator outage gaps.',
        `
SELECT "period", "facility_id", "coverage_status", "mw_comparison_status", "outage_gap_mw"
FROM "facility_reconciliation_daily"
ORDER BY "period" DESC, "facility_id"
LIMIT 100`,
      ),
      example(
        'medium',
        'Rank facility outage discrepancies',
        'Find absolute outage gaps greater than the 0.01 MW comparison tolerance.',
        `
SELECT "period", "facility_id", "coverage_status", "outage_gap_mw", "outage_abs_gap_mw"
FROM "facility_reconciliation_daily"
WHERE "outage_abs_gap_mw" > 0.01
ORDER BY "outage_abs_gap_mw" DESC, "period" DESC
LIMIT 100`,
      ),
      example(
        'hard',
        'Summarize facility comparison groups',
        'Compare coverage/status groups with measurable outage gaps; null gaps are not zero.',
        `
SELECT "coverage_status", "mw_comparison_status", COUNT(*) AS "observations",
       COUNT("outage_abs_gap_mw") AS "measurable_gaps",
       ROUND(AVG("outage_abs_gap_mw"), 3) AS "average_absolute_gap_mw",
       MAX("outage_abs_gap_mw") AS "largest_absolute_gap_mw"
FROM "facility_reconciliation_daily"
GROUP BY "coverage_status", "mw_comparison_status"
HAVING COUNT("outage_abs_gap_mw") > 0
ORDER BY "largest_absolute_gap_mw" DESC
LIMIT 100`,
      ),
    ],
  },
  national_reconciliation_daily: {
    columns: [
      'period',
      'coverage_status',
      'mw_comparison_status',
      'facility_outage_gap_mw',
      'generator_outage_gap_mw',
      'facility_outage_abs_gap_mw',
      'generator_outage_abs_gap_mw',
    ],
    items: [
      example(
        'easy',
        'Inspect recent national comparisons',
        'Compare national measurements with separate facility and generator totals.',
        `
SELECT "period", "coverage_status", "mw_comparison_status",
       "facility_outage_gap_mw", "generator_outage_gap_mw"
FROM "national_reconciliation_daily"
ORDER BY "period" DESC
LIMIT 100`,
      ),
      example(
        'medium',
        'Find national outage discrepancies',
        'Keep dates where either absolute outage gap exceeds 0.01 MW.',
        `
SELECT "period", "coverage_status", "facility_outage_abs_gap_mw", "generator_outage_abs_gap_mw"
FROM "national_reconciliation_daily"
WHERE "facility_outage_abs_gap_mw" > 0.01 OR "generator_outage_abs_gap_mw" > 0.01
ORDER BY "generator_outage_abs_gap_mw" DESC, "period" DESC
LIMIT 100`,
      ),
      example(
        'hard',
        'Summarize national comparison groups',
        'Compare status groups with measurable generator gaps and retain separate facility gap statistics.',
        `
SELECT "coverage_status", "mw_comparison_status", COUNT(*) AS "observed_days",
       COUNT("generator_outage_abs_gap_mw") AS "days_with_generator_gap",
       ROUND(AVG("facility_outage_abs_gap_mw"), 3) AS "average_facility_gap_mw",
       MAX("generator_outage_abs_gap_mw") AS "largest_generator_gap_mw"
FROM "national_reconciliation_daily"
GROUP BY "coverage_status", "mw_comparison_status"
HAVING COUNT("generator_outage_abs_gap_mw") > 0
ORDER BY "largest_generator_gap_mw" DESC
LIMIT 100`,
      ),
    ],
  },
  data_quality_events: {
    columns: [
      'event_id',
      'period',
      'grain',
      'facility_id',
      'generator_id',
      'rule_id',
      'severity',
      'gap_days',
      'details',
    ],
    items: [
      example(
        'easy',
        'Inspect recent quality events',
        'Read recorded anomalies without inferring their physical causes.',
        `
SELECT "event_id", "period", "grain", "rule_id", "severity", "details"
FROM "data_quality_events"
ORDER BY "period" DESC, "event_id"
LIMIT 100`,
      ),
      example(
        'medium',
        'Find observation-gap events',
        'Inspect events recording a gap longer than one calendar day.',
        `
SELECT "period", "facility_id", "generator_id", "rule_id", "gap_days", "details"
FROM "data_quality_events"
WHERE "gap_days" > 1
ORDER BY "gap_days" DESC, "period" DESC
LIMIT 100`,
      ),
      example(
        'hard',
        'Find recurring quality-event groups',
        'Find rule, severity, and grain combinations with at least two recorded events.',
        `
SELECT "rule_id", "severity", "grain", COUNT(*) AS "event_count",
       COUNT(DISTINCT "period") AS "affected_dates",
       MIN("period") AS "first_event_date", MAX("period") AS "last_event_date"
FROM "data_quality_events"
GROUP BY "rule_id", "severity", "grain"
HAVING COUNT(*) >= 2
ORDER BY "event_count" DESC, "rule_id"
LIMIT 100`,
      ),
    ],
  },
};

/**
 * Return compatible built-in examples for one caller-authorized catalog dataset.
 *
 * Unknown sources and incomplete/malformed schemas have no examples. Static SQL
 * never interpolates catalog data or user input. Returning copies prevents edits
 * in one workspace from mutating the templates used by another selection.
 * @param {{id: string, schema: Array<[string, string]>}|null} dataset Catalog entry.
 * @returns {Array<{id: string, level: string, title: string, description: string, sql: string}>}
 */
export function sqlExamples(dataset) {
  if (!dataset || typeof dataset.id !== 'string' || !Object.hasOwn(EXAMPLES, dataset.id)) return [];
  if (!Array.isArray(dataset.schema)) return [];
  const names = new Set();
  for (const column of dataset.schema) {
    if (!Array.isArray(column) || typeof column[0] !== 'string' || typeof column[1] !== 'string')
      return [];
    names.add(column[0]);
  }
  const source = EXAMPLES[dataset.id];
  if (!source.columns.every((name) => names.has(name))) return [];
  return source.items.map((item) => ({
    ...item,
    title: t(item.title),
    description: t(item.description),
  }));
}
