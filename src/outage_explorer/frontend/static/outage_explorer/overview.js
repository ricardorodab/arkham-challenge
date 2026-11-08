/**
 * @module overview
 * Load a bounded pinned fleet period and coordinate cards, chart and table.
 *
 * The server-provided stored metric is authoritative. Reject an incomplete page
 * rather than charting a truncated period; retain exact values in the table and
 * use approximate numeric formatting only for graphical/card presentation.
 */
import { t } from './i18n.js';
import { ApiError, StaleRequest, queryString, verifyPage } from './api.js';
import {
  byId,
  setText,
  visible,
  state,
  showError,
  addDays,
  dayCount,
  numberLabel,
  dateLabel,
} from './dom.js';
import { renderTable, exportPage } from './table.js';
import { renderChart, chartPoints } from './chart.js';

/**
 * Coordinate one pinned metric packet, its exact table and responsive SVG presentation.
 */
export class OverviewController {
  /**
   * Bind bounded range selection/export and observe chart size without refetching data.
   */
  constructor(context) {
    this.context = context;
    this.sequence = 0;
    this.packet = null;
    this.loaded = false;
    this.preset = 30;
    byId('overview-form').addEventListener('submit', (event) => {
      event.preventDefault();
      this.preset = null;
      this.load(true);
    });
    for (const button of document.querySelectorAll('[data-range]'))
      button.addEventListener('click', () => {
        this.preset = Number(button.dataset.range);
        this.setRange();
        this.load(true);
      });
    byId('overview-export').addEventListener('click', () =>
      exportPage(this.packet, 'fleet_daily', this.catalog?.model_id),
    );
    byId('overview-export').disabled = true;
    this.observer =
      typeof ResizeObserver === 'function'
        ? new ResizeObserver((entries) => {
            const width = entries[0]?.contentRect.width;
            if (width > 0 && Math.abs(width - (this.lastWidth || 0)) > 1) {
              this.lastWidth = width;
              if (this.packet && !byId('view-overview').hidden) this.draw();
            }
          })
        : null;
    this.observer?.observe(byId('fleet-chart'));
  }
  /**
   * Invalidate earlier model requests and reset chart/export state for the new catalog.
   */
  configure(catalog) {
    this.catalog = catalog;
    this.sequence++;
    this.packet = null;
    this.loaded = false;
    this.dataset = catalog.datasets.find((dataset) => dataset.id === 'fleet_daily');
    if (this.dataset && (this.preset || !byId('overview-start').value)) this.setRange();
    visible('overview-content', false);
    byId('overview-export').disabled = true;
  }
  /**
   * Derive a preset inclusive range from actual source bounds and synchronize pressed buttons.
   */
  setRange() {
    if (!this.dataset?.max_period) return;
    const end = this.dataset.max_period;
    const start = addDays(end, -(this.preset || 30) + 1);
    byId('overview-start').value =
      start < this.dataset.min_period ? this.dataset.min_period : start;
    byId('overview-end').value = end;
    for (const button of document.querySelectorAll('[data-range]')) {
      const active = Number(button.dataset.range) === this.preset;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
  }
  /**
   * Validate at most 366 calendar days and request a complete model-pinned metric page.
   *
   * Reject continuation rather than presenting a partial period. Only a current
   * generation may set cards/table/chart; stored exact percentages remain separate
   * from approximate card/coordinate formatting.
   * @param {boolean} [force=false] Reload despite a previously loaded period.
   */
  async load(force = false) {
    if (!this.catalog || (!force && this.loaded)) return;
    const sequence = ++this.sequence,
      model = this.catalog.model_id;
    // The queue and renderer share this closure so old models/generations cannot update cards or chart.
    const isCurrent = () =>
      this.context.alive() && sequence === this.sequence && this.catalog?.model_id === model;
    const start = byId('overview-start').value,
      end = byId('overview-end').value;
    try {
      if (!this.dataset)
        throw new ApiError(t('The fleet metric is not available for this account.'), {
          status: 403,
        });
      const days = dayCount(start, end);
      if (days > 366)
        throw new Error(
          t('Choose a period of at most 366 days. Use Explore data to browse the full history.'),
        );
    } catch (error) {
      this.packet = null;
      this.loaded = false;
      state('overview', 'error', error);
      byId('overview-export').disabled = true;
      return;
    }
    this.packet = null;
    state('overview', 'loading');
    byId('overview-apply').disabled = true;
    byId('overview-export').disabled = true;
    try {
      const packet = await this.context.queue.run(
        () =>
          this.context.client.request(
            `/data/fleet_daily${queryString({ start, end, limit: 1000, model_id: model })}`,
          ),
        isCurrent,
      );
      if (!isCurrent()) return;
      verifyPage(packet, 'fleet_daily', model);
      if (packet.next_cursor)
        throw new Error(
          t('This period has too many observations for the chart. Choose a shorter period.'),
        );
      this.packet = packet;
      this.range = { start, end };
      this.loaded = true;
      state('overview', packet.rows.length ? 'content' : 'empty');
      if (!packet.rows.length) return;
      const names = packet.columns.map((column) => column.name),
        latest = packet.rows.at(-1);
      // Read the latest row in the selected period; preserve its original exact scalar for titles/table.
      const value = (name) => latest[names.indexOf(name)];
      setText(
        'metric-offline-share',
        value('offline_pct') === null ? '—' : `${numberLabel(value('offline_pct'), 2)}%`,
      );
      byId('metric-offline-share').title =
        value('offline_pct') === null
          ? t('Undefined; no valid denominator.')
          : t('Exact calculated percentage: {value}%', { value: value('offline_pct') });
      setText('metric-outage', numberLabel(value('outage_mw'), 1));
      setText('metric-capacity', numberLabel(value('capacity_mw'), 1));
      setText('metric-date', dateLabel(value('period')));
      const points = chartPoints(packet),
        defined = points.filter(
          (point) => point.value !== null && Number.isFinite(point.value),
        ).length;
      const missing = Math.max(0, dayCount(start, end) - packet.rows.length);
      setText(
        'chart-description',
        t(
          '{count} defined daily observations · {start} – {end}.{missing} Gaps and undefined ratios are not zero. Exact values are available in the table below.',
          {
            count: defined,
            start: dateLabel(start),
            end: dateLabel(end),
            missing: missing
              ? ` ${t(missing === 1 ? '{count} calendar day without observations.' : '{count} calendar days without observations.', { count: missing })}`
              : '',
          },
        ),
      );
      renderTable(
        byId('overview-table'),
        packet,
        t('Exact national offline-capacity observations for the selected period'),
      );
      byId('overview-export').disabled = false;
      this.draw();
    } catch (error) {
      if (!(error instanceof StaleRequest) && isCurrent()) {
        this.loaded = false;
        state('overview', 'error', error);
        showError('overview-error', error, () => this.load(true));
      }
    } finally {
      if (isCurrent()) byId('overview-apply').disabled = false;
    }
  }
  /**
   * Redraw the existing packet for the current container size without an analytical request.
   */
  draw() {
    if (this.packet && this.range)
      renderChart(byId('fleet-chart'), this.packet, this.range.start, this.range.end);
  }
  /**
   * Invalidate pending results, disconnect resize observation and clear metric/table/chart state.
   */
  dispose() {
    this.sequence++;
    this.packet = null;
    this.observer?.disconnect();
    byId('overview-table').replaceChildren();
    byId('fleet-chart').replaceChildren();
    for (const id of ['metric-offline-share', 'metric-outage', 'metric-capacity', 'metric-date'])
      setText(id, '—');
  }
}
