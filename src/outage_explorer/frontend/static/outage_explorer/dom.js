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
    ? 'Not allowed'
    : error.status === 429
      ? 'Workspace is busy'
      : 'Something needs your attention';
  node.replaceChildren(
    element('strong', 'state-title', title),
    element('p', 'state-message', error.message || String(error)),
  );
  if (error.retryAfter)
    node.append(
      element('p', 'state-message', `Please wait ${error.retryAfter} seconds before trying again.`),
    );
  if (error.requestId)
    node.append(element('p', 'request-reference', `Reference: ${error.requestId}`));
  if (retry) {
    const button = element('button', 'button button-secondary', 'Try again');
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
  Object.hasOwn(TITLES, id) ? TITLES[id] : id.replaceAll('_', ' ');
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
/**
 * Choose a known column label while preserving prototype-like valid SQL aliases.
 */
export const columnTitle = (name) =>
  Object.hasOwn(COLUMN_TITLES, name) ? COLUMN_TITLES[name] : name.replaceAll('_', ' ');

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
  if (!date) throw new Error('Choose a valid calendar date.');
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
    throw new Error('Choose a valid date range with the start on or before the end.');
  return Math.round((last - first) / 86400000) + 1;
}
/**
 * Format a validated observation date in UTC; invalid/missing dates use a placeholder.
 */
export function dateLabel(value, options = {}) {
  const date = isoDate(value);
  return date
    ? new Intl.DateTimeFormat('en-US', {
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
    ? new Intl.NumberFormat('en-US', {
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
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (numericType(type) && typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value)) {
    const [integer, fraction] = value.split('.');
    const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    const decimals = fraction?.replace(/0+$/, '');
    return grouped + (decimals ? `.${decimals}` : '');
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
      button.setAttribute('aria-label', `Insert column ${name}`);
      button.addEventListener('click', () => onInsert(name));
      row.append(button);
    } else row.append(element('span', 'schema-name', name));
    row.append(element('span', 'schema-type', type));
    if (dataset.primary_key.includes(name)) row.append(element('span', 'schema-key', 'PK'));
    if (dataset.units[name]) row.append(element('span', 'schema-unit', dataset.units[name]));
    container.append(row);
  }
}
