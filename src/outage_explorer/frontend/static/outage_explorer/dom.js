import { t, getLocale, getLanguage } from './i18n.js';

/**
 * @module dom
 * Text-only DOM construction, explicit view states and scalar presentation.
 *
 * Exact API numeric strings are formatted without Number coercion in tables.
 * Calendar dates use UTC arithmetic; numberLabel is for approximate display.
 * These helpers provide presentation, never dataset or SQL authorization.
 */
/**
 * Resolve a DOM binding by its stable template identifier.
 * @param {string} id Element ID.
 * @returns {HTMLElement|null} Matching element, if mounted.
 */
export const byId = (id) => document.getElementById(id);
/**
 * Update an existing binding with text only, avoiding a redundant DOM mutation.
 */
export function setText(id, value) {
  const node = byId(id);
  if (node && node.textContent !== String(value ?? '')) node.textContent = value ?? '';
}
/**
 * Synchronize the hidden attribute for an element or template ID.
 * @param {string|HTMLElement} id Binding or element.
 * @param {boolean} [value=true] Desired visibility.
 */
export function visible(id, value = true) {
  const node = typeof id === 'string' ? byId(id) : id;
  if (node) node.hidden = !value;
}
/**
 * Create a detached HTML element; optional external content is always text.
 * @returns {HTMLElement} Element with trusted tag/class structure.
 */
export function element(tag, className = '', text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null) node.textContent = String(text);
  return node;
}

/**
 * Select one loading/error/empty/content branch and synchronize aria-busy.
 * @param {string} prefix Template state-group prefix.
 * @param {string} kind Desired branch.
 * @param {Error|null} [error] Optional safe error presentation.
 */
export function state(prefix, kind, error = null) {
  for (const value of ['loading', 'error', 'empty', 'content'])
    visible(`${prefix}-${value}`, kind === value);
  const root = byId(`${prefix}-form`);
  if (root) root.setAttribute('aria-busy', kind === 'loading' ? 'true' : 'false');
  if (kind === 'error' && error) showError(`${prefix}-error`, error);
}

/**
 * Render safe denial/busy/failure text, request correlation and optional retry action.
 *
 * Messages become text nodes; raw server HTML is never interpreted.
 */
export function showError(id, error, retry = null) {
  const node = byId(id);
  if (!node) return;
  const denied = error.status === 403;
  const title = denied
    ? t('Not allowed')
    : error.status === 429
      ? t('Workspace is busy')
      : t('Something needs your attention');
  node.replaceChildren(
    element('strong', 'state-title', title),
    element('p', 'state-message', error.message || String(error)),
  );
  if (error.retryAfter)
    node.append(
      element(
        'p',
        'state-message',
        t('Please wait {seconds} seconds before trying again.', { seconds: error.retryAfter }),
      ),
    );
  if (error.requestId)
    node.append(element('p', 'request-reference', t('Reference: {id}', { id: error.requestId })));
  if (retry) {
    const button = element('button', 'button button-secondary', t('Try again'));
    button.type = 'button';
    button.addEventListener('click', retry);
    node.append(button);
  }
  node.hidden = false;
}

const TITLES = {
  national_daily: 'National observations',
  fleet_daily: 'Fleet offline capacity',
  facility_daily: 'Facility observations',
  generator_daily: 'Generator observations',
  dim_date: 'Observation dates',
  dim_facility: 'Facilities',
  dim_generator: 'Generators',
  facility_reconciliation_daily: 'Facility reconciliation',
  national_reconciliation_daily: 'National reconciliation',
  data_quality_events: 'Data quality events',
};
/**
 * Choose a known dataset label without inheriting Object prototype properties.
 */
export const datasetTitle = (id) =>
  Object.hasOwn(TITLES, id) ? t(TITLES[id]) : id.replaceAll('_', ' ');
const COLUMN_TITLES = {
  capacity_mw: 'Capacity (MW)',
  outage_mw: 'Offline (MW)',
  offline_pct: 'Offline share (%)',
  offline_share: 'Offline share',
  reported_outage_pct: 'Reported outage (%)',
  period: 'Date',
  facility_id: 'Facility ID',
  generator_id: 'Generator ID',
  facility_name: 'Facility',
  quality_flags: 'Quality flags',
  source_row_hash: 'Source fingerprint',
};
const MODEL_COLUMN_NAMES = new Set([
  'after_capacity_mw',
  'before_capacity_mw',
  'capacity_abs_gap_mw',
  'capacity_gap_mw',
  'coverage_status',
  'day',
  'details',
  'event_id',
  'facility_capacity_abs_gap_mw',
  'facility_capacity_gap_mw',
  'facility_capacity_mw',
  'facility_count',
  'facility_null_capacity_count',
  'facility_null_outage_count',
  'facility_null_pct_count',
  'facility_outage_abs_gap_mw',
  'facility_outage_gap_mw',
  'facility_outage_mw',
  'facility_present',
  'facility_reported_pct',
  'facility_reported_pct_gap_pp',
  'facility_weighted_reported_pct',
  'first_observed',
  'gap_days',
  'generator_capacity_abs_gap_mw',
  'generator_capacity_gap_mw',
  'generator_capacity_mw',
  'generator_count',
  'generator_null_capacity_count',
  'generator_null_outage_count',
  'generator_null_pct_count',
  'generator_outage_abs_gap_mw',
  'generator_outage_gap_mw',
  'generator_outage_mw',
  'generator_reported_pct_gap_pp',
  'generator_weighted_reported_pct',
  'grain',
  'last_observed',
  'month',
  'mw_comparison_status',
  'national_capacity_mw',
  'national_outage_mw',
  'national_present',
  'national_reported_pct',
  'outage_abs_gap_mw',
  'outage_gap_mw',
  'pct_comparison_status',
  'previous_period',
  'reported_pct_gap_pp',
  'reported_vs_calculated_pct_gap',
  'rule_id',
  'seen_in_facility_route',
  'seen_in_generator_route',
  'severity',
  'year',
]);
/**
 * Choose a known column label while preserving prototype-like valid SQL aliases.
 */
export const columnTitle = (name) =>
  Object.hasOwn(COLUMN_TITLES, name)
    ? t(COLUMN_TITLES[name])
    : MODEL_COLUMN_NAMES.has(name)
      ? t(name.replaceAll('_', ' '))
      : name.replaceAll('_', ' ');

/**
 * Validate an ISO calendar date by UTC round-trip, rejecting overflowed dates.
 * @returns {Date|null} UTC midnight or null; no local-zone date conversion.
 */
export function isoDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value ? date : null;
}
/**
 * Shift an ISO date using UTC calendar arithmetic across DST and month boundaries.
 * @returns {string} Shifted ISO date.
 * @throws {Error} Invalid starting calendar date.
 */
export function addDays(value, days) {
  const date = isoDate(value);
  if (!date) throw new Error(t('Choose a valid calendar date.'));
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}
/**
 * Return the inclusive UTC day count for a valid ascending date range.
 * @throws {Error} Invalid dates or reversed bounds.
 */
export function dayCount(start, end) {
  const first = isoDate(start),
    last = isoDate(end);
  if (!first || !last || first > last)
    throw new Error(t('Choose a valid date range with the start on or before the end.'));
  return Math.round((last - first) / 86400000) + 1;
}
/**
 * Format a validated observation date in UTC; invalid/missing dates use a placeholder.
 */
export function dateLabel(value, options = {}) {
  const date = isoDate(value);
  return date
    ? new Intl.DateTimeFormat(getLocale(), {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        timeZone: 'UTC',
        ...options,
      }).format(date)
    : '—';
}
/**
 * Format an approximate presentation number for cards/coordinates, not canonical storage.
 * @param {*} value Scalar convertible to a finite Number.
 * @param {number} [digits=1] Fixed displayed fractional digits.
 */
export const numberLabel = (value, digits = 1) =>
  value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
    ? new Intl.NumberFormat(getLocale(), {
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      }).format(Number(value))
    : '—';
/**
 * Classify supported SQL numeric type names for alignment and scalar presentation.
 */
export const numericType = (type) =>
  /^(?:U?(?:TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|INT\d*|DECIMAL|NUMERIC|DOUBLE|FLOAT|REAL)/i.test(
    type,
  );

/**
 * Format exact numeric strings by string operations, retaining their mathematical value.
 *
 * Trailing zeros/group separators are presentation only. Never coerce a database
 * decimal or 64-bit integer string to Number for table/export processing.
 */
export function scalarLabel(value, type = '') {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'boolean') return value ? t('True') : t('False');
  if (numericType(type) && typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value)) {
    const [integer, fraction] = value.split('.');
    const group = getLanguage() === 'es' ? '.' : ',';
    const separator = getLanguage() === 'es' ? ',' : '.';
    const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, group);
    const decimals = fraction?.replace(/0+$/, '');
    return grouped + (decimals ? `${separator}${decimals}` : '');
  }
  return String(value);
}

/**
 * Populate choices solely from the authorized catalog and retain a permitted selection.
 */
export function fillDatasets(select, datasets, selected) {
  select.replaceChildren();
  for (const dataset of datasets) {
    const option = element('option', '', datasetTitle(dataset.id));
    option.value = dataset.id;
    select.append(option);
  }
  if (datasets.some((dataset) => dataset.id === selected)) select.value = selected;
  select.disabled = !datasets.length;
}

/**
 * Render column/type/key/unit metadata with optional quoted-identifier insertion.
 *
 * This callback assists editing; it neither parses SQL nor grants permissions.
 */
export function schema(container, dataset, onInsert = null) {
  container.replaceChildren();
  for (const [name, type] of dataset.schema) {
    const row = element('div', 'schema-field');
    if (onInsert) {
      const button = element('button', 'metadata-chip', name);
      button.type = 'button';
      button.setAttribute('aria-label', t('Insert column {name}', { name }));
      button.addEventListener('click', () => onInsert(name));
      row.append(button);
    } else row.append(element('span', 'schema-name', name));
    row.append(element('span', 'schema-type', type));
    if (dataset.primary_key.includes(name)) row.append(element('span', 'schema-key', t('PK')));
    if (dataset.units[name]) row.append(element('span', 'schema-unit', t(dataset.units[name])));
    container.append(row);
  }
}
