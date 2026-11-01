/** Rendering helpers accept text, never server-supplied HTML. */
export const byId = (id) => document.getElementById(id);
export function setText(id, value) {
  const node = byId(id);
  if (node && node.textContent !== String(value ?? '')) node.textContent = value ?? '';
}
export function visible(id, value = true) {
  const node = typeof id === 'string' ? byId(id) : id;
  if (node) node.hidden = !value;
}
export function element(tag, className = '', text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null) node.textContent = String(text);
  return node;
}

export function state(prefix, kind, error = null) {
  for (const value of ['loading', 'error', 'empty', 'content'])
    visible(`${prefix}-${value}`, kind === value);
  const root = byId(`${prefix}-form`);
  if (root) root.setAttribute('aria-busy', kind === 'loading' ? 'true' : 'false');
  if (kind === 'error' && error) showError(`${prefix}-error`, error);
}

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
export const columnTitle = (name) =>
  Object.hasOwn(COLUMN_TITLES, name) ? COLUMN_TITLES[name] : name.replaceAll('_', ' ');

export function isoDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value ? date : null;
}
export function addDays(value, days) {
  const date = isoDate(value);
  if (!date) throw new Error('Choose a valid calendar date.');
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}
export function dayCount(start, end) {
  const first = isoDate(start),
    last = isoDate(end);
  if (!first || !last || first > last)
    throw new Error('Choose a valid date range with the start on or before the end.');
  return Math.round((last - first) / 86400000) + 1;
}
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
export const numberLabel = (value, digits = 1) =>
  value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value))
    ? new Intl.NumberFormat('en-US', {
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      }).format(Number(value))
    : '—';
export const numericType = (type) =>
  /^(?:U?(?:TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|INT\d*|DECIMAL|NUMERIC|DOUBLE|FLOAT|REAL)/i.test(
    type,
  );

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
