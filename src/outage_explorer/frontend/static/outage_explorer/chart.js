/**
 * @module chart
 * Render the stored fleet metric as an SVG presentation.
 *
 * Coordinate arithmetic is approximate; packets retain exact scalar strings for
 * tables and export. Missing dates and undefined values split segments, while
 * observed zero remains a plotted value. No authoritative metric is recomputed.
 */
import { t, getLocale } from './i18n.js';
import { dateLabel, isoDate, numberLabel } from './dom.js';

const NS = 'http://www.w3.org/2000/svg';
/**
 * Create an SVG namespace element with attributes and optional text-only content.
 * @returns {SVGElement} Detached element for the trusted chart structure.
 */
function svgNode(tag, attrs = {}, text = null) {
  const node = document.createElementNS(NS, tag);
  for (const [name, value] of Object.entries(attrs)) node.setAttribute(name, value);
  if (text !== null) node.textContent = String(text);
  return node;
}

/**
 * Extract sorted dated metric points while retaining each exact original scalar.
 * @param {Object} data Typed fleet_daily positional packet.
 * @returns {Array<Object>} Calendar points with approximate Number coordinates and exact text.
 */
export function chartPoints(data) {
  const columns = data.columns.map((column) => column.name);
  const period = columns.indexOf('period'),
    value = columns.indexOf('offline_pct');
  return data.rows
    .map((row) => ({
      period: row[period],
      value: row[value] === null ? null : Number(row[value]),
      exact: row[value],
    }))
    .filter((point) => isoDate(point.period))
    .sort((a, b) => a.period.localeCompare(b.period));
}

/**
 * Split a sorted series at missing calendar days and undefined/nonfinite values.
 *
 * Zero is a valid observation, and no missing value is interpolated or filled.
 * @returns {Array<Array<Object>>} Consecutive defined segments.
 */
export function contiguousSegments(points) {
  const groups = [];
  let current = [],
    previous = null;
  for (const point of points) {
    const valid = point.value !== null && Number.isFinite(point.value);
    const gap = previous && (isoDate(point.period) - isoDate(previous)) / 86400000 > 1;
    if (!valid || gap) {
      if (current.length) groups.push(current);
      current = [];
    }
    if (valid) current.push(point);
    previous = point.period;
  }
  if (current.length) groups.push(current);
  return groups;
}

/**
 * Replace the SVG presentation of the stored metric for a complete bounded period.
 *
 * The exact semantic table is maintained separately by the overview controller.
 * The pointer-only tooltip is aria-hidden; title/description describe gaps and
 * the table alternative. Outlying representable values are not clipped to 100%.
 * @param {Element} container Chart mount.
 * @param {Object} data Validated typed metric packet.
 * @param {string} start Inclusive ISO calendar date.
 * @param {string} end Inclusive ISO calendar date.
 */
export function renderChart(container, data, start, end) {
  container.replaceChildren();
  const points = chartPoints(data),
    valid = points.filter((point) => point.value !== null && Number.isFinite(point.value));
  if (!valid.length) {
    const message = document.createElement('p');
    message.className = 'chart-empty';
    message.textContent = t('No defined offline-capacity values in this range.');
    container.append(message);
    return;
  }
  const width = Math.max(240, Math.min(1000, container.clientWidth || 1000));
  const height = width < 600 ? 240 : 300,
    plot = { left: 58, right: 28, top: 24, bottom: 46 };
  const innerWidth = width - plot.left - plot.right,
    innerHeight = height - plot.top - plot.bottom;
  const minimum = Math.min(0, ...valid.map((point) => point.value));
  const observedMax = Math.max(1, ...valid.map((point) => point.value));
  const step = observedMax > 20 ? 10 : observedMax > 8 ? 5 : observedMax > 3 ? 2 : 1;
  const maximum = Math.ceil(observedMax / step) * step;
  const lower = Math.floor(minimum / step) * step;
  const first = isoDate(start).getTime(),
    last = isoDate(end).getTime();
  // Coordinate closures use the selected calendar period, including dates without observations.
  const x = (point) =>
    plot.left +
    (last === first ? 0.5 : (isoDate(point.period).getTime() - first) / (last - first)) *
      innerWidth;
  const y = (value) => plot.top + innerHeight * (1 - (value - lower) / (maximum - lower));
  const svg = svgNode('svg', {
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-labelledby': 'fleet-chart-title fleet-chart-description',
  });
  svg.append(
    svgNode('title', { id: 'fleet-chart-title' }, t('Daily share of national capacity offline')),
    svgNode(
      'desc',
      { id: 'fleet-chart-description' },
      t(
        '{count} defined daily observations from {start} to {end}. Gaps and undefined values are shown as breaks. Exact observations are available in the chart data table.',
        {
          count: valid.length,
          start: dateLabel(start),
          end: dateLabel(end),
        },
      ),
    ),
  );
  for (let index = 0; index <= 4; index++) {
    const value = lower + ((maximum - lower) * index) / 4,
      top = y(value);
    svg.append(
      svgNode('line', {
        x1: plot.left,
        x2: width - plot.right,
        y1: top,
        y2: top,
        class: 'chart-grid',
      }),
      svgNode(
        'text',
        { x: plot.left - 12, y: top + 4, 'text-anchor': 'end', class: 'chart-label' },
        `${numberLabel(value, 1)}%`,
      ),
    );
  }
  const ticks = last === first ? 0 : width < 600 ? 2 : 4;
  for (let index = 0; index <= ticks; index++) {
    const fraction = ticks ? index / ticks : 0.5;
    const date = new Date(first + (last - first) * fraction).toISOString().slice(0, 10);
    const position = plot.left + innerWidth * fraction;
    svg.append(
      svgNode(
        'text',
        {
          x: position,
          y: height - 12,
          'text-anchor':
            index === 0 && ticks ? 'start' : index === ticks && ticks ? 'end' : 'middle',
          class: 'chart-label',
        },
        dateLabel(date, { month: 'short', day: 'numeric', year: undefined }),
      ),
    );
  }
  const baseline = y(Math.max(lower, 0));
  for (const group of contiguousSegments(points)) {
    const line = group
      .map(
        (point, index) => `${index ? 'L' : 'M'}${x(point).toFixed(2)},${y(point.value).toFixed(2)}`,
      )
      .join(' ');
    if (group.length > 1)
      svg.append(
        svgNode('path', {
          d: `${line} L${x(group.at(-1))},${baseline} L${x(group[0])},${baseline} Z`,
          class: 'chart-area',
        }),
      );
    svg.append(svgNode('path', { d: line, class: 'chart-line' }));
    if (group.length === 1)
      svg.append(
        svgNode('circle', { cx: x(group[0]), cy: y(group[0].value), r: 4, class: 'chart-dot' }),
      );
  }
  const tooltip = svgNode('g', {
    class: 'chart-tooltip',
    visibility: 'hidden',
    'aria-hidden': 'true',
  });
  const marker = svgNode('line', {
    class: 'chart-crosshair',
    y1: plot.top,
    y2: height - plot.bottom,
  });
  const dot = svgNode('circle', { r: 5, class: 'chart-dot' });
  const bubble = svgNode('g');
  const tooltipWidth = Math.min(getLocale() === 'es-ES' ? 230 : 182, width - 4);
  const box = svgNode('rect', { width: tooltipWidth, height: 56, rx: 6 });
  const label = svgNode('text', { x: 12, y: 21 }),
    amount = svgNode('text', { x: 12, y: 43 });
  bubble.append(box, label, amount);
  tooltip.append(marker, dot, bubble);
  svg.append(tooltip);
  // This hover affordance is supplementary; the exact table supplies keyboard-accessible observations.
  svg.addEventListener('pointermove', (event) => {
    const rectangle = svg.getBoundingClientRect(),
      position = ((event.clientX - rectangle.left) / rectangle.width) * width;
    const nearest = valid.reduce((best, point) =>
      Math.abs(x(point) - position) < Math.abs(x(best) - position) ? point : best,
    );
    const px = x(nearest),
      py = y(nearest.value);
    marker.setAttribute('x1', px);
    marker.setAttribute('x2', px);
    dot.setAttribute('cx', px);
    dot.setAttribute('cy', py);
    bubble.setAttribute(
      'transform',
      `translate(${Math.max(2, Math.min(width - tooltipWidth - 2, px + 14))},${Math.max(2, py - 70)})`,
    );
    label.textContent = dateLabel(nearest.period);
    amount.textContent = t('{value}% offline', { value: numberLabel(nearest.value, 3) });
    tooltip.setAttribute('visibility', 'visible');
  });
  svg.addEventListener('pointerleave', () => tooltip.setAttribute('visibility', 'hidden'));
  container.append(svg);
}
