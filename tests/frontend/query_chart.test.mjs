import test from 'node:test';
import assert from 'node:assert/strict';
import {
  chartOptions,
  validateChart,
  renderQueryChart,
  MAX_CHART_SERIES,
} from '../../src/outage_explorer/frontend/static/outage_explorer/query_chart.js';

const columns = [
  { name: 'period', type: 'DATE' },
  { name: 'outage', type: 'DECIMAL(24,9)' },
  { name: 'facility_id', type: 'VARCHAR' },
];
const config = { date_column: 'period', value_column: 'outage' };
const packet = (rows, extra = {}) => ({
  columns,
  rows,
  result_rows: rows.length,
  returned_rows: rows.length,
  next_cursor: null,
  ...extra,
});
const code = (expected) => (error) => error.code === expected;

test('chart choices use unique typed outputs rather than guessing text IDs or aliases', () => {
  const options = chartOptions(packet([]));
  assert.deepEqual(options.date_columns, [columns[0]]);
  assert.deepEqual(options.numeric_columns, [columns[1]]);
  assert.deepEqual(options.series_columns, [columns[1], columns[2]]);
  assert.deepEqual(
    chartOptions({ columns: [columns[0], columns[0], columns[1]] }).date_columns,
    [],
  );
  assert.deepEqual(chartOptions(null), {
    date_columns: [],
    numeric_columns: [],
    series_columns: [],
  });
  assert.throws(
    () =>
      validateChart(packet([['2026-01-01', '0', '001']]), {
        ...config,
        value_column: 'facility_id',
      }),
    code('value_column_required'),
  );
});

test('chart normalization sorts time, preserves exact values and zero, and breaks gaps/nulls', () => {
  const result = validateChart(
    packet([
      ['2026-01-06', '3.000000001', '001'],
      ['2026-01-02', null, '001'],
      ['2026-01-04', '-2', '001'],
      ['2026-01-01', '0.000000000', '001'],
      ['2026-01-03', '1', '001'],
    ]),
    config,
  );
  assert.deepEqual(
    result.series[0].points.map((point) => point.period),
    ['2026-01-01', '2026-01-02', '2026-01-03', '2026-01-04', '2026-01-06'],
  );
  assert.deepEqual(
    result.series[0].segments.map((segment) => segment.map((point) => point.value)),
    [[0], [1, -2], [3.000000001]],
  );
  assert.equal(result.series[0].points[0].exact, '0.000000000');
  assert.equal(result.series[0].points.at(-1).exact, '3.000000001');
  assert.equal(result.coverage.null_values, 1);
});

test('charting rejects incomplete pages while clearly labeling complete capped results', () => {
  const rows = [['2026-01-01', '2', '001']];
  assert.throws(
    () => validateChart(packet(rows, { next_cursor: 'bookmark' }), config),
    code('incomplete_result'),
  );
  assert.throws(
    () => validateChart(packet(rows, { result_rows: 2 }), config),
    code('incomplete_result'),
  );
  assert.throws(
    () => validateChart(packet(rows, { returned_rows: 2 }), config),
    code('incomplete_result'),
  );
  const capped = validateChart(packet(rows, { truncated: true }), config);
  assert.equal(capped.coverage.truncated, true);
  assert.match(capped.warnings[0], /Capped query result.*1 retained rows.*not every matching row/);
});

test('ambiguous dates require series and duplicate date-series keys require SQL aggregation', () => {
  const data = packet([
    ['2026-01-01', '2', '001'],
    ['2026-01-01', '3', '002'],
  ]);
  assert.throws(() => validateChart(data, config), code('duplicate_time'));
  const grouped = validateChart(data, { ...config, series_column: 'facility_id' });
  assert.equal(grouped.series.length, 2);
  assert.deepEqual(
    grouped.series.map((series) => series.name),
    ['"001"', '"002"'],
  );
  assert.throws(
    () =>
      validateChart(packet([...data.rows, data.rows[0]]), {
        ...config,
        series_column: 'facility_id',
      }),
    code('duplicate_time'),
  );
  assert.throws(
    () => validateChart(data, { ...config, series_column: 'outage' }),
    code('invalid_series'),
  );
});

test('bounded series distinguish null, literal text and empty identifiers without conflation', () => {
  const grouped = validateChart(
    packet([
      ['2026-01-01', '2', null],
      ['2026-01-01', '3', 'NULL'],
      ['2026-01-01', '4', ''],
    ]),
    { ...config, series_column: 'facility_id' },
  );
  assert.equal(new Set(grouped.series.map((series) => series.name)).size, 3);
  const excessive = Array.from({ length: MAX_CHART_SERIES + 1 }, (_, i) => [
    '2026-01-01',
    '1',
    String(i),
  ]);
  assert.throws(
    () => validateChart(packet(excessive), { ...config, series_column: 'facility_id' }),
    code('series_limit'),
  );
});

test('missing dates are explicitly counted, invalid dates and undefined-only values are actionable', () => {
  const partial = validateChart(
    packet([
      [null, '99', '001'],
      ['2026-01-01', '0', '001'],
    ]),
    config,
  );
  assert.equal(partial.coverage.missing_date_rows, 1);
  assert.match(partial.warnings[0], /1 row without a time value/);
  assert.throws(
    () => validateChart(packet([['2026-02-30', '1', '001']]), config),
    code('invalid_date'),
  );
  assert.throws(
    () => validateChart(packet([['2026-01-01', null, '001']]), config),
    code('no_defined_values'),
  );
  assert.throws(() => validateChart(packet([[null, '1', '001']]), config), code('no_dated_rows'));
  assert.throws(() => validateChart(packet([]), config), code('empty_result'));
});

test('timestamps normalize timezone identity and retain submillisecond distinctions', () => {
  const timestamp = { ...columns[0], type: 'TIMESTAMP WITH TIME ZONE' };
  const data = packet(
    [
      ['2026-01-01T00:00:00.000002Z', '2', '001'],
      ['2025-12-31T16:00:00.000001-08:00', '1', '001'],
      ['2026-01-03 10:00:00', '3', '001'],
    ],
    { columns: [timestamp, ...columns.slice(1)] },
  );
  const model = validateChart(data, config);
  assert.equal(model.time_type, 'timestamp');
  assert.equal(model.series[0].points[0].period, '2026-01-01T00:00:00.000001Z');
  assert.equal(model.series[0].segments.length, 1);
  assert.throws(
    () =>
      validateChart(
        {
          ...data,
          rows: [data.rows[1], ['2026-01-01T00:00:00.000001Z', '2', '001']],
          result_rows: 2,
          returned_rows: 2,
        },
        config,
      ),
    code('duplicate_time'),
  );
});

test('numeric overflow, nonzero underflow and malformed values cannot become chart coordinates', () => {
  for (const value of ['1e400', '-1e400', '1e-400'])
    assert.throws(
      () => validateChart(packet([['2026-01-01', value, '001']]), config),
      code('unrepresentable_value'),
    );
  for (const value of ['', 'NaN', true, '12x'])
    assert.throws(
      () => validateChart(packet([['2026-01-01', value, '001']]), config),
      code('invalid_value'),
    );
});

/** A minimal DOM records renderer structure without duplicating browser layout behavior. */
class Node {
  constructor(tagName, ownerDocument) {
    this.tagName = tagName;
    this.ownerDocument = ownerDocument;
    this.children = [];
    this.attrs = {};
    this.listeners = {};
    this.clientWidth = 500;
  }
  setAttribute(name, value) {
    this.attrs[name] = String(value);
  }
  append(...nodes) {
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) {
    this.children = nodes;
  }
  addEventListener(name, action) {
    this.listeners[name] = action;
  }
}
class Document {
  createElement(name) {
    return new Node(name, this);
  }
  createElementNS(namespace, name) {
    return new Node(name, this);
  }
  createTextNode(text) {
    const node = new Node('#text', this);
    node.textContent = text;
    return node;
  }
}
const descendants = (node) => [node, ...node.children.flatMap(descendants)];

test('renderer uses unique accessible identities, exact tooltips, cap warnings, and finite extreme coordinates', () => {
  const document = new Document(),
    first = new Node('div', document),
    second = new Node('div', document);
  const data = packet(
    [
      ['2026-01-01', '-1.7e308', '001'],
      ['2026-01-02', '1.7e308', '001'],
    ],
    { truncated: true },
  );
  renderQueryChart(first, data, config, {
    title: '<img src=x> retained measure',
    tableId: 'exact-table',
  });
  renderQueryChart(second, data, config);
  const nodes = descendants(first),
    svg = nodes.find((node) => node.tagName === 'svg');
  assert.equal(svg.attrs.role, 'img');
  assert.notEqual(
    svg.attrs['aria-labelledby'],
    descendants(second).find((node) => node.tagName === 'svg').attrs['aria-labelledby'],
  );
  assert.ok(
    nodes.some(
      (node) => node.tagName === 'title' && node.textContent === '<img src=x> retained measure',
    ),
  );
  assert.ok(
    nodes.some((node) => node.tagName === 'title' && node.textContent?.includes('-1.7e308')),
  );
  assert.ok(nodes.some((node) => node.textContent?.includes('Capped query result')));
  assert.ok(
    nodes.some((node) => node.tagName === 'button' && node.textContent === 'View exact chart data'),
  );
  assert.equal(
    nodes.some((node) => node.tagName === 'img'),
    false,
  );
  for (const node of nodes)
    for (const value of Object.values(node.attrs)) assert.doesNotMatch(value, /NaN|Infinity/);
});

test('renderer gives flat zero and negative series finite domains without inventing observations', () => {
  const document = new Document();
  for (const values of [
    ['0', '0'],
    ['-2', '-2'],
    ['1e-300', '1e-300'],
  ]) {
    const container = new Node('div', document);
    const model = renderQueryChart(
      container,
      packet([
        ['2026-01-01', values[0], '001'],
        ['2026-01-02', values[1], '001'],
      ]),
      config,
    );
    assert.equal(model.coverage.defined_points, 2);
    const nodes = descendants(container);
    assert.equal(nodes.filter((node) => node.tagName === 'circle').length, 2);
    for (const node of nodes)
      for (const value of Object.values(node.attrs)) assert.doesNotMatch(value, /NaN|Infinity/);
  }
});
