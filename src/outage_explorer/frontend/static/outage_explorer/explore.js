/**
 * @module explore
 * Manage schema-driven preview filters and immutable keyset pages.
 *
 * Draft controls are distinct from the applied request and displayed packet.
 * Generation/model guards discard obsolete work; export uses the actual packet
 * rather than reinterpreting newly edited controls as a completed request.
 */
import { StaleRequest, queryString, verifyPage } from './api.js';
import {
  byId,
  setText,
  visible,
  state,
  showError,
  element,
  datasetTitle,
  fillDatasets,
  schema,
  addDays,
  dayCount,
  dateLabel,
} from './dom.js';
import { renderTable, exportPage } from './table.js';

/**
 * Own preview draft/applied state, keyset cursor chain and displayed page packet.
 */
export class ExploreController {
  /**
   * Bind schema-driven filter controls and page/export actions to the shared workspace.
   */
  constructor(context) {
    this.context = context;
    this.sequence = 0;
    this.packet = null;
    this.catalog = null;
    this.page = 0;
    this.cursors = [null];
    this.applied = null;
    this.loaded = false;
    this.working = false;
    byId('dataset-select').addEventListener('change', () => {
      this.selectDataset();
      this.apply();
    });
    byId('explore-form').addEventListener('submit', (event) => {
      event.preventDefault();
      this.apply();
    });
    byId('explore-reset').addEventListener('click', () => {
      this.defaultFilters();
      this.apply();
    });
    byId('explore-prev').addEventListener('click', () => this.send(this.page - 1));
    byId('explore-next').addEventListener('click', () => this.send(this.page + 1));
    byId('explore-export').addEventListener('click', () =>
      exportPage(this.packet, this.applied?.dataset_id, this.catalog?.model_id),
    );
    for (const id of ['explore-start', 'explore-end', 'explore-facility', 'explore-page-size'])
      byId(id).addEventListener('change', () => {
        this.dirty = true;
        this.updatePager();
      });
  }
  /**
   * Resolve the selected logical dataset from this pinned authorized catalog.
   */
  dataset() {
    return this.catalog?.datasets.find((dataset) => dataset.id === byId('dataset-select').value);
  }
  /**
   * Invalidate old model work and preserve compatible draft filter intent when adopting a catalog.
   */
  configure(catalog) {
    const previous = this.catalog
      ? {
          dataset: this.selected,
          start: byId('explore-start').value,
          end: byId('explore-end').value,
          facility: byId('explore-facility').value,
          limit: byId('explore-page-size').value,
        }
      : null;
    this.catalog = catalog;
    this.sequence++;
    this.loaded = false;
    this.packet = null;
    this.applied = null;
    this.facilityVersion = null;
    const preferred =
      this.selected || (this.context.scope.persona === 'viewer' ? 'fleet_daily' : 'facility_daily');
    fillDatasets(byId('dataset-select'), catalog.datasets, preferred);
    this.selectDataset();
    if (previous && previous.dataset === this.selected) {
      byId('explore-start').value = previous.start;
      byId('explore-end').value = previous.end;
      byId('explore-facility').value = previous.facility;
      byId('explore-page-size').value = previous.limit;
    }
  }
  /**
   * Reset prior results and configure controls/metadata for the selected schema.
   */
  selectDataset() {
    this.sequence++;
    this.packet = null;
    this.applied = null;
    this.working = false;
    this.loaded = false;
    this.selected = byId('dataset-select').value;
    const dataset = this.dataset();
    if (!dataset) return;
    const names = dataset.schema.map((column) => column[0]);
    visible('date-filters', names.includes('period'));
    visible('facility-filter', names.includes('facility_id'));
    setText('dataset-description', dataset.description);
    setText('dataset-row-count', Number(dataset.rows).toLocaleString());
    setText(
      'dataset-grain',
      dataset.primary_key.map((name) => name.replaceAll('_id', '')).join(' + '),
    );
    schema(byId('schema-content'), dataset);
    this.defaultFilters();
    byId('explore-table').replaceChildren();
    this.updatePager();
    if (this.context.isView('explore')) this.loadFacilities();
  }
  /**
   * Choose the latest available 30-day window, clamped to source coverage, and mark it unapplied.
   */
  defaultFilters() {
    const dataset = this.dataset();
    if (!dataset) return;
    const hasDate = dataset.schema.some((column) => column[0] === 'period');
    const end = hasDate ? dataset.max_period : '';
    let start = end ? addDays(end, -29) : '';
    if (start && start < dataset.min_period) start = dataset.min_period;
    byId('explore-start').value = start;
    byId('explore-end').value = end;
    byId('explore-facility').value = '';
    this.dirty = true;
  }
  /**
   * Load the first preview only when needed and request optional facility choices.
   */
  async activate() {
    if (!this.loaded && !this.working) this.apply();
    this.loadFacilities();
  }
  /**
   * Load the first authorized facility page for the pinned model without changing the selected ID.
   *
   * The current selector requests at most 1,000 entries and does not follow its
   * continuation; a larger domain requires explicit lookup pagination. A model
   * change during loading schedules a current retry rather than rendering stale choices.
   */
  async loadFacilities() {
    if (
      !this.catalog?.datasets.some((dataset) => dataset.id === 'dim_facility') ||
      !this.dataset()?.schema.some((column) => column[0] === 'facility_id') ||
      this.facilityVersion === this.catalog.model_id ||
      this.facilityLoading
    )
      return;
    this.facilityLoading = true;
    const version = this.catalog.model_id;
    try {
      const data = await this.context.queue.run(
        () =>
          this.context.client.request(
            `/data/dim_facility${queryString({ limit: 1000, model_id: version })}`,
          ),
        () => this.context.alive() && this.catalog?.model_id === version,
      );
      verifyPage(data, 'dim_facility', version);
      if (!this.context.alive() || this.catalog?.model_id !== version) return;
      const names = data.columns.map((column) => column.name),
        id = names.indexOf('facility_id'),
        name = names.indexOf('facility_name');
      const selected = byId('explore-facility').value;
      byId('explore-facility').replaceChildren();
      const all = element('option', '', 'All facilities');
      all.value = '';
      byId('explore-facility').append(all);
      for (const row of [...data.rows].sort((a, b) =>
        String(a[name]).localeCompare(String(b[name])),
      )) {
        const option = element('option', '', `${row[name] || 'Facility'} · ${row[id]}`);
        option.value = row[id];
        byId('explore-facility').append(option);
      }
      if (
        selected &&
        ![...byId('explore-facility').options].some((option) => option.value === selected)
      ) {
        const option = element('option', '', `Facility ${selected}`);
        option.value = selected;
        byId('explore-facility').append(option);
      }
      byId('explore-facility').value = selected;
      this.facilityVersion = version;
    } catch (error) {
      if (!(error instanceof StaleRequest) && this.context.alive())
        this.context.notice(
          'The facility list could not be loaded. Other filters still work; revisit this view to retry.',
        );
    } finally {
      this.facilityLoading = false;
      if (
        this.context.alive() &&
        this.catalog?.model_id !== version &&
        this.context.isView('explore')
      )
        this.loadFacilities();
    }
  }
  /**
   * Validate draft dates, capture one immutable request and reset its cursor chain.
   */
  apply() {
    const dataset = this.dataset();
    if (!dataset) return;
    const hasDate = dataset.schema.some((column) => column[0] === 'period');
    const hasFacility = dataset.schema.some((column) => column[0] === 'facility_id');
    const start = hasDate ? byId('explore-start').value : '',
      end = hasDate ? byId('explore-end').value : '';
    try {
      if (start && end) dayCount(start, end);
    } catch (error) {
      state('explore', 'error', error);
      return;
    }
    // Capture controls once; later edits must not relabel the packet produced by this request.
    this.applied = {
      dataset_id: dataset.id,
      start,
      end,
      facility_id: hasFacility ? byId('explore-facility').value : '',
      limit: Number(byId('explore-page-size').value),
      model_id: this.catalog.model_id,
    };
    this.page = 0;
    this.cursors = [null];
    this.dirty = false;
    this.send(0);
  }
  /**
   * Fetch a retained keyset continuation and render only the captured generation/model.
   *
   * The copied applied request labels rows/export even if controls are edited while
   * work waits. Stale work may finish on the server but never relabel the current view.
   * @param {number} page Zero-based page index with an already known cursor.
   */
  async send(page) {
    if (!this.applied || page < 0 || (page > 0 && !this.cursors[page])) return;
    const applied = { ...this.applied },
      sequence = ++this.sequence;
    // Both queued execution and response delivery must still belong to this captured applied request.
    const current = () =>
      this.context.alive() &&
      sequence === this.sequence &&
      this.catalog?.model_id === applied.model_id;
    this.working = true;
    this.packet = null;
    state('explore', 'loading');
    this.updatePager();
    const { dataset_id: datasetId, ...parameters } = applied;
    if (this.cursors[page]) parameters.cursor = this.cursors[page];
    try {
      const result = await this.context.queue.run(
        () =>
          this.context.client.request(
            `/data/${encodeURIComponent(datasetId)}${queryString(parameters)}`,
          ),
        current,
      );
      if (!current()) return;
      this.packet = verifyPage(result, datasetId, applied.model_id);
      this.page = page;
      this.loaded = true;
      this.cursors[page + 1] = result.next_cursor;
      this.cursors.length = page + 2;
      renderTable(
        byId('explore-table'),
        result,
        `${datasetTitle(datasetId)} observations, page ${page + 1}`,
      );
      state('explore', result.rows.length ? 'content' : 'empty');
      const filters = [];
      if (applied.start || applied.end)
        filters.push(
          `${applied.start ? dateLabel(applied.start) : 'First observation'} – ${applied.end ? dateLabel(applied.end) : 'Latest observation'}`,
        );
      if (applied.facility_id) filters.push(`Facility ${applied.facility_id}`);
      setText(
        'explore-applied-filters',
        filters.length ? filters.join(' · ') : 'All available observations',
      );
    } catch (error) {
      if (!(error instanceof StaleRequest) && current()) {
        this.loaded = false;
        state('explore', 'error', error);
        showError('explore-error', error, () => this.apply());
      }
    } finally {
      if (current()) {
        this.working = false;
        this.updatePager();
      }
    }
  }
  /**
   * Enable continuation only for the displayed applied request, and export only its packet.
   */
  updatePager() {
    byId('explore-apply').disabled = this.working;
    byId('explore-prev').disabled = this.working || !this.packet || this.page === 0 || this.dirty;
    byId('explore-next').disabled = this.working || !this.packet?.next_cursor || this.dirty;
    byId('explore-export').disabled = this.working || !this.packet?.rows.length;
    setText('explore-page-label', this.packet ? `Page ${this.page + 1}` : 'No page loaded');
    if (this.packet)
      setText(
        'explore-result-meta',
        `${this.packet.returned_rows} observations shown · Version ${this.packet.model_id.slice(0, 8)}${this.dirty ? ' · Filters changed; apply to update.' : ''}`,
      );
  }
  /**
   * Invalidate in-flight render work and remove retained preview/facility DOM state.
   */
  dispose() {
    this.sequence++;
    this.packet = null;
    this.applied = null;
    byId('explore-table').replaceChildren();
    byId('explore-facility').replaceChildren();
  }
}
