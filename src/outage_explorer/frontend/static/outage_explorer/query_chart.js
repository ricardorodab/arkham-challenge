/**
 * @module query_chart
 * Validate and render complete retained SQL results as bounded time-series charts.
 *
 * Queries, permissions and result collection remain server/controller concerns.
 * Chart coordinates use approximate Numbers; exact original scalars remain in
 * point metadata and the caller's semantic table. No aggregation is inferred.
 */

const NS = 'http://www.w3.org/2000/svg';
const DAY = 86400000;
export const MAX_CHART_ROWS = 1000;
export const MAX_CHART_SERIES = 8;
const NUMBER = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
const NUMERIC_TYPE =
  /^(?:(?:U)?(?:TINYINT|SMALLINT|INTEGER|INT|BIGINT|HUGEINT)|FLOAT|DOUBLE(?: PRECISION)?|REAL|DECIMAL(?:\(\d+,\s*\d+\))?|NUMERIC(?:\(\d+,\s*\d+\))?)$/i;
const DATE_TYPE = /^DATE$/i;
const TIMESTAMP_TYPE = /^TIMESTAMP(?:$|_|\s)/i;
// Dark strokes remain visible on white; repeated dash patterns supplement color.
const COLORS = [
  '#2458c6',
  '#007f73',
  '#7c3fb0',
  '#ab5300',
  '#006c8e',
  '#b33168',
  '#4b6079',
  '#6b6c1a',
];
let sequence = 0;

/** Identify an actionable chart configuration or result-shape failure. */
export class QueryChartError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'QueryChartError';
    this.code = code;
  }
}

/** Return unique, typed output names; duplicate aliases cannot be selected safely. */
function outputColumns(packet) {
  if (!packet || !Array.isArray(packet.columns)) return [];
  const counts = new Map();
  for (const column of packet.columns)
    if (column && typeof column.name === 'string')
      counts.set(column.name, (counts.get(column.name) ?? 0) + 1);
  return packet.columns
    .filter(
      (column) =>
        column &&
        typeof column.name === 'string' &&
        typeof column.type === 'string' &&
        counts.get(column.name) === 1,
    )
    .map((column) => ({ name: column.name, type: column.type }));
}

/** Discover typed time/value/series choices without interpreting text IDs as numbers. */
export function chartOptions(packet) {
  const columns = outputColumns(packet);
  return {
    date_columns: columns.filter(
      (column) => DATE_TYPE.test(column.type) || TIMESTAMP_TYPE.test(column.type),
    ),
    numeric_columns: columns.filter((column) => NUMERIC_TYPE.test(column.type)),
    series_columns: columns.filter(
      (column) => !DATE_TYPE.test(column.type) && !TIMESTAMP_TYPE.test(column.type),
    ),
  };
}

/** Parse a real ISO calendar date in UTC without browser-local calendar conversion. */
function calendar(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const time = Date.parse(`${value}T00:00:00.000Z`);
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value ? time : null;
}

/** Parse typed timestamps as UTC instants while retaining up to nanosecond identity. */
function instant(value, type) {
  if (DATE_TYPE.test(type)) {
    const time = calendar(value);
    return time === null ? null : { time, key: value, period: value };
  }
  if (typeof value !== 'string') return null;
  const match =
    /^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?$/.exec(
      value,
    );
  if (
    !match ||
    calendar(match[1]) === null ||
    Number(match[2]) > 23 ||
    Number(match[3]) > 59 ||
    Number(match[4]) > 59
  )
    return null;
  const zone = match[6] || 'Z';
  if (zone !== 'Z' && (Number(zone.slice(1, 3)) > 23 || Number(zone.slice(4)) > 59)) return null;
  const seconds = Date.parse(`${match[1]}T${match[2]}:${match[3]}:${match[4]}${zone}`);
  if (!Number.isFinite(seconds)) return null;
  const fraction = (match[5] || '').padEnd(9, '0');
  const key = `${seconds}:${fraction}`;
  const period = `${new Date(seconds).toISOString().slice(0, 19)}${match[5] ? `.${match[5]}` : ''}Z`;
  return { time: seconds + Number(`0.${fraction}`) * 1000, key, period };
}

/** Convert a numeric scalar for coordinates, refusing infinity and nonzero underflow. */
function coordinate(value) {
  if (value === null) return null;
  if (!['number', 'string'].includes(typeof value) || !NUMBER.test(String(value)))
    throw new QueryChartError(
      'invalid_value',
      'The selected value column contains an invalid numeric value. Choose another column or refine the query.',
    );
  const number = Number(value);
  const nonzero = /[1-9]/.test(String(value).split(/[eE]/)[0]);
  if (!Number.isFinite(number) || (number === 0 && nonzero))
    throw new QueryChartError(
      'unrepresentable_value',
      'A selected value is outside the chart’s numeric range. Exact values remain available in the table.',
    );
  return number;
}

/** Validate that the supplied rows represent the complete retained query result. */
function completePacket(packet) {
  if (!packet || !Array.isArray(packet.columns) || !Array.isArray(packet.rows))
    throw new QueryChartError('invalid_result', 'Run a query before configuring its chart.');
  if (packet.rows.length > MAX_CHART_ROWS)
    throw new QueryChartError(
      'row_limit',
      `Charts support at most ${MAX_CHART_ROWS.toLocaleString('en-US')} retained rows. Refine the query.`,
    );
  if (
    (packet.result_rows !== undefined &&
      (!Number.isInteger(packet.result_rows) || packet.result_rows < 0)) ||
    (packet.returned_rows !== undefined &&
      (!Number.isInteger(packet.returned_rows) || packet.returned_rows < 0))
  )
    throw new QueryChartError(
      'invalid_result',
      'The result row counts cannot be verified. Run the query again.',
    );
  if (
    packet.next_cursor ||
    (Number.isInteger(packet.result_rows) && packet.result_rows !== packet.rows.length) ||
    (Number.isInteger(packet.returned_rows) && packet.returned_rows !== packet.rows.length)
  )
    throw new QueryChartError(
      'incomplete_result',
      'Load all retained result pages before charting. A single page is not the complete query result.',
    );
  if (!packet.rows.length)
    throw new QueryChartError(
      'empty_result',
      'This query returned no rows to chart. Adjust its filters or show the table.',
    );
  if (packet.rows.some((row) => !Array.isArray(row) || row.length !== packet.columns.length))
    throw new QueryChartError(
      'invalid_result',
      'The result shape cannot be charted. Run the query again.',
    );
}

/** Split lines at null values and missing calendar days, preserving real zero observations. */
function segments(points, daily) {
  const output = [];
  let current = [],
    previous = null;
  for (const point of points) {
    if (point.value === null || (daily && previous !== null && point.time - previous > DAY)) {
      if (current.length) output.push(current);
      current = [];
    }
    if (point.value !== null) current.push(point);
    previous = point.time;
  }
  if (current.length) output.push(current);
  return output;
}

/**
 * Validate explicit axes and normalize a complete retained result without aggregation.
 *
 * Missing time values are excluded with an explicit warning. Non-null invalid
 * dates, duplicate time/series keys, ambiguous aliases and invalid coordinates
 * fail rather than silently changing the question represented by the chart.
 */
export function validateChart(packet, config) {
  completePacket(packet);
  const options = chartOptions(packet);
  const date = options.date_columns.find((column) => column.name === config?.date_column);
  const value = options.numeric_columns.find((column) => column.name === config?.value_column);
  if (!date)
    throw new QueryChartError(
      'date_column_required',
      'Choose a DATE or TIMESTAMP result column for the time axis. Queries without a time column can be saved as tables.',
    );
  if (!value)
    throw new QueryChartError(
      'value_column_required',
      'Choose a numeric result column for the value axis. Text identifiers are not chart measures.',
    );
  const seriesName = config?.series_column || null;
  if (
    seriesName &&
    (seriesName === date.name ||
      seriesName === value.name ||
      !options.series_columns.some((column) => column.name === seriesName))
  )
    throw new QueryChartError(
      'invalid_series',
      'Choose a different result column to identify the series, or select no series.',
    );
  const dateIndex = packet.columns.findIndex((column) => column.name === date.name);
  const valueIndex = packet.columns.findIndex((column) => column.name === value.name);
  const seriesIndex = seriesName
    ? packet.columns.findIndex((column) => column.name === seriesName)
    : -1;
  const groups = new Map(),
    warnings = [];
  let missingDates = 0,
    defined = 0,
    nullValues = 0;
  packet.rows.forEach((row, rowIndex) => {
    if (row[dateIndex] === null) {
      missingDates++;
      return;
    }
    const time = instant(row[dateIndex], date.type);
    if (!time)
      throw new QueryChartError(
        'invalid_date',
        `Row ${rowIndex + 1} has an invalid time value. Refine the query or choose a different time column.`,
      );
    const rawSeries = seriesIndex === -1 ? null : row[seriesIndex];
    if (rawSeries !== null && !['string', 'number', 'boolean'].includes(typeof rawSeries))
      throw new QueryChartError('invalid_series', 'Series values must be scalar result values.');
    const key = seriesIndex === -1 ? 'single' : JSON.stringify([typeof rawSeries, rawSeries]);
    if (!groups.has(key)) {
      if (groups.size >= MAX_CHART_SERIES)
        throw new QueryChartError(
          'series_limit',
          `Choose at most ${MAX_CHART_SERIES} series. Filter or summarize the query first.`,
        );
      groups.set(key, {
        key,
        name:
          seriesIndex === -1
            ? value.name
            : rawSeries === null
              ? 'NULL'
              : typeof rawSeries === 'string'
                ? JSON.stringify(rawSeries)
                : String(rawSeries),
        points: [],
        seen: new Set(),
      });
    }
    const group = groups.get(key);
    if (group.seen.has(time.key))
      throw new QueryChartError(
        'duplicate_time',
        seriesName
          ? 'More than one row has the same time and series. Aggregate those rows in SQL or choose a more specific series.'
          : 'More than one row has the same time. Choose a series column, or aggregate the rows by time in SQL.',
      );
    group.seen.add(time.key);
    const numeric = coordinate(row[valueIndex]);
    if (numeric === null) nullValues++;
    else defined++;
    group.points.push({
      period: time.period,
      time: time.time,
      date_exact: row[dateIndex],
      value: numeric,
      exact: row[valueIndex],
      row_index: rowIndex,
    });
  });
  if (!groups.size)
    throw new QueryChartError(
      'no_dated_rows',
      'No rows have a defined time value. Filter missing dates or show the table.',
    );
  if (!defined)
    throw new QueryChartError(
      'no_defined_values',
      'The selected value column has no defined numeric observations. Missing values are not zero.',
    );
  const output = [...groups.values()]
    .sort((a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
    .map((group) => {
      group.points.sort(
        (a, b) => a.time - b.time || (a.period < b.period ? -1 : a.period > b.period ? 1 : 0),
      );
      return {
        key: group.key,
        name: group.name,
        points: group.points,
        segments: segments(group.points, DATE_TYPE.test(date.type)),
      };
    });
  const points = output.flatMap((group) => group.points);
  const numeric = points.filter((point) => point.value !== null).map((point) => point.value);
  if (packet.truncated)
    warnings.push(
      `Capped query result: this chart shows all ${packet.rows.length.toLocaleString('en-US')} retained rows, not every matching row in the dataset. Refine the query for complete coverage.`,
    );
  if (missingDates)
    warnings.push(
      `${missingDates.toLocaleString('en-US')} row${missingDates === 1 ? '' : 's'} without a time value ${missingDates === 1 ? 'is' : 'are'} excluded from the chart; the exact table retains them.`,
    );
  return {
    config: { date_column: date.name, value_column: value.name, series_column: seriesName },
    time_type: DATE_TYPE.test(date.type) ? 'date' : 'timestamp',
    series: output,
    extent: {
      start: Math.min(...points.map((point) => point.time)),
      end: Math.max(...points.map((point) => point.time)),
      min: Math.min(...numeric),
      max: Math.max(...numeric),
    },
    coverage: {
      rows: packet.rows.length,
      dated_rows: points.length,
      missing_date_rows: missingDates,
      defined_points: defined,
      null_values: nullValues,
      series_count: output.length,
      truncated: !!packet.truncated,
    },
    warnings,
  };
}

/** Construct trusted SVG elements with text-only content and presentation attributes. */
function svgNode(document, tag, attrs = {}, text = null) {
  const node = document.createElementNS(NS, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, String(value));
  if (text !== null) node.textContent = String(text);
  return node;
}

/** Format approximate axis coordinates without changing the original table/tooltip values. */
function axisNumber(value) {
  if (value === 0) return '0';
  return Math.abs(value) >= 1e7 || Math.abs(value) < 0.001
    ? value.toExponential(2)
    : new Intl.NumberFormat('en-US', { maximumSignificantDigits: 5 }).format(value);
}

/** Format UTC positions for the axis, including the year when the range crosses years. */
function axisTime(value, type, includeYear) {
  const options =
    type === 'timestamp'
      ? { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
      : { month: 'short', day: 'numeric' };
  if (includeYear) options.year = 'numeric';
  return new Intl.DateTimeFormat('en-US', { ...options, timeZone: 'UTC' }).format(new Date(value));
}

/**
 * Render an accessible static chart for all retained rows, returning its normalized model.
 *
 * The caller supplies the exact semantic table. Native point titles preserve
 * original scalars, while the figure describes approximate coordinates, gaps
 * and any retained-result cap. No inline styles, animation or network access are
 * required; SVG dimensions and colors use ordinary presentation attributes.
 */
export function renderQueryChart(
  container,
  packet,
  config,
  { title = 'Query results over time', tableId = null } = {},
) {
  const model = validateChart(packet, config);
  const document = container.ownerDocument;
  const figure = document.createElement('figure');
  figure.className = 'query-chart-figure';
  const id = `query-time-chart-${++sequence}`;
  const width = Math.max(280, Math.min(1100, container.clientWidth || 720));
  const height = width < 600 ? 260 : 320;
  const margin = {
    left: width < 600 ? 64 : 78,
    right: 20,
    top: 22,
    bottom: model.time_type === 'timestamp' ? 60 : 44,
  };
  const innerWidth = width - margin.left - margin.right,
    innerHeight = height - margin.top - margin.bottom;
  const low = Math.min(0, model.extent.min),
    high = Math.max(0, model.extent.max);
  const scale = Math.max(Math.abs(low), Math.abs(high)) || 1;
  const lower = low / scale,
    upper = high === low ? lower + 1 : high / scale;
  const x = (time) =>
    margin.left +
    (model.extent.start === model.extent.end
      ? 0.5
      : (time - model.extent.start) / (model.extent.end - model.extent.start)) *
      innerWidth;
  const y = (value) => margin.top + innerHeight * (1 - (value / scale - lower) / (upper - lower));
  const description = `${model.coverage.defined_points} defined observations in ${model.coverage.series_count} series. ${model.time_type === 'date' ? 'Missing calendar days and undefined values break lines.' : 'Undefined values break lines; irregular time spacing is preserved.'} Coordinates are approximate; exact values are available in the result table.${model.warnings.length ? ` ${model.warnings.join(' ')}` : ''}`;
  const svg = svgNode(document, 'svg', {
    width: '100%',
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-labelledby': `${id}-title ${id}-description`,
    preserveAspectRatio: 'xMidYMid meet',
  });
  svg.append(
    svgNode(document, 'title', { id: `${id}-title` }, title),
    svgNode(document, 'desc', { id: `${id}-description` }, description),
  );
  for (let index = 0; index <= 4; index++) {
    const fraction = index / 4,
      value = scale * (lower + (upper - lower) * fraction),
      top = margin.top + innerHeight * (1 - fraction);
    svg.append(
      svgNode(document, 'line', {
        x1: margin.left,
        x2: width - margin.right,
        y1: top,
        y2: top,
        stroke: '#dce3ee',
        'stroke-width': 1,
      }),
      svgNode(
        document,
        'text',
        { x: margin.left - 9, y: top + 4, 'text-anchor': 'end', fill: '#52647b', 'font-size': 12 },
        axisNumber(value),
      ),
    );
  }
  const sameTime = model.extent.start === model.extent.end;
  const ticks = sameTime ? 0 : width < 600 ? 1 : 3;
  const includeYear =
    new Date(model.extent.start).getUTCFullYear() !== new Date(model.extent.end).getUTCFullYear();
  for (let index = 0; index <= ticks; index++) {
    const fraction = ticks ? index / ticks : 0.5;
    const time = model.extent.start + (model.extent.end - model.extent.start) * fraction;
    svg.append(
      svgNode(
        document,
        'text',
        {
          x: margin.left + innerWidth * fraction,
          y: height - (model.time_type === 'timestamp' ? 28 : 14),
          'text-anchor':
            ticks && index === 0 ? 'start' : ticks && index === ticks ? 'end' : 'middle',
          fill: '#52647b',
          'font-size': 12,
        },
        axisTime(time, model.time_type, includeYear),
      ),
    );
  }
  const legend = document.createElement('ul');
  legend.className = 'query-chart-legend';
  legend.setAttribute('aria-label', 'Chart series');
  model.series.forEach((group, index) => {
    const color = COLORS[index];
    for (const segment of group.segments) {
      if (segment.length > 1) {
        const line = segment
          .map(
            (point, at) =>
              `${at ? 'L' : 'M'}${x(point.time).toFixed(3)},${y(point.value).toFixed(3)}`,
          )
          .join(' ');
        svg.append(
          svgNode(document, 'path', {
            d: line,
            fill: 'none',
            stroke: color,
            'stroke-width': 2.5,
            'stroke-dasharray': index % 3 === 1 ? '7 3' : index % 3 === 2 ? '2 3' : 'none',
          }),
        );
      }
      for (const point of segment) {
        const circle = svgNode(document, 'circle', {
          cx: x(point.time),
          cy: y(point.value),
          r: segment.length === 1 ? 3.5 : 2.5,
          fill: color,
        });
        circle.append(
          svgNode(
            document,
            'title',
            {},
            `${model.config.date_column}: ${point.date_exact}\n${group.name}: ${String(point.exact)}`,
          ),
        );
        svg.append(circle);
      }
    }
    const item = document.createElement('li');
    const swatch = svgNode(document, 'svg', {
      width: 24,
      height: 12,
      'aria-hidden': 'true',
      focusable: 'false',
    });
    swatch.append(
      svgNode(document, 'line', {
        x1: 1,
        x2: 22,
        y1: 6,
        y2: 6,
        stroke: color,
        'stroke-width': 3,
        'stroke-dasharray': index % 3 === 1 ? '7 3' : index % 3 === 2 ? '2 3' : 'none',
      }),
    );
    item.append(swatch, document.createTextNode(` ${group.name}`));
    legend.append(item);
  });
  const caption = document.createElement('figcaption');
  caption.className = 'query-chart-caption';
  caption.textContent = `${model.config.value_column} by ${model.config.date_column}${model.time_type === 'timestamp' ? ' (UTC)' : ''}. Chart coordinates are approximate. ${model.time_type === 'date' ? 'Missing days and null values break lines.' : 'Null values break lines; irregular time spacing is preserved.'}`;
  if (tableId) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'button button-quiet button-small';
    button.textContent = 'View exact chart data';
    button.addEventListener('click', () => {
      const table = document.getElementById(String(tableId));
      if (!table) return;
      const details = table.closest('details');
      if (details) details.open = true;
      if (!table.hasAttribute('tabindex')) table.tabIndex = -1;
      table.scrollIntoView({ block: 'nearest', behavior: 'auto' });
      table.focus({ preventScroll: true });
    });
    caption.append(document.createTextNode(' '), button);
  }
  figure.append(svg, legend, caption);
  for (const warning of model.warnings) {
    const message = document.createElement('p');
    message.className = 'notice notice-subtle query-chart-warning';
    message.textContent = warning;
    figure.append(message);
  }
  container.replaceChildren(figure);
  return model;
}
