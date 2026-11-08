/**
 * @module saved_views
 * Manage named, authorized SQL view definitions and bounded Overview previews.
 *
 * Definitions are persisted by the service rather than browser storage. Each
 * execution runs as the current viewer on the workspace's active publication;
 * sharing is not a substitute for underlying dataset permission.
 */
import { ApiError, StaleRequest, queryString, verifyPage } from './api.js';
import { byId, element, visible, datasetTitle, columnTitle } from './dom.js';
import { renderTable, exportPage } from './table.js';
import { chartOptions, renderQueryChart } from './query_chart.js';

const PAGE_SIZE = 20;
const TABLE_PAGE_SIZE = 25;
const MAX_ROWS = 1000;
const MAX_BYTES = 8 * 1024 * 1024;
const MAX_LOADED = 8;
const ROLES = [
  ['viewer', 'Viewers'],
  ['analyst', 'Analysts'],
  ['admin', 'Admins'],
];

/** Check the public saved-definition envelope before constructing controls or request paths. */
function validDefinition(view, datasetIds) {
  return (
    view &&
    typeof view.id === 'string' &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(view.id) &&
    typeof view.name === 'string' &&
    view.name.length > 0 &&
    view.name.length <= 100 &&
    datasetIds.has(view.dataset_id) &&
    typeof view.sql === 'string' &&
    new TextEncoder().encode(view.sql).length <= 16384 &&
    Number.isInteger(view.revision) &&
    view.revision > 0 &&
    Number.isInteger(view.max_rows) &&
    view.max_rows >= 1 &&
    view.max_rows <= MAX_ROWS &&
    typeof view.chart?.enabled === 'boolean' &&
    Array.isArray(view.shared_roles) &&
    view.shared_roles.length <= 3 &&
    view.shared_roles.every((role) => ROLES.some(([known]) => known === role)) &&
    typeof view.is_owner === 'boolean' &&
    typeof view.can_edit === 'boolean' &&
    typeof view.can_delete === 'boolean'
  );
}

/** Create a non-submitting control whose external label is always rendered as text. */
function button(label, handler, className = 'button button-secondary button-small') {
  const node = element('button', className, label);
  node.type = 'button';
  node.addEventListener('click', handler);
  return node;
}

/** Create a semantic labeled control without interpreting external values as markup. */
function field(label, input) {
  const wrapper = element('label', 'field');
  wrapper.append(element('span', '', label), input);
  return wrapper;
}

/** Render a safe bounded API error without exposing response bodies or SQL literals. */
function failure(container, error) {
  container.replaceChildren(element('p', '', error.message || 'The view could not be loaded.'));
  if (error.requestId)
    container.append(element('p', 'request-reference', `Reference: ${error.requestId}`));
  container.hidden = false;
}

/**
 * Combine pages of one saved execution without charting a partial SQL result.
 *
 * The immutable model, saved revision, output schema, cursor chain and retained
 * count must remain consistent. Capped results are retained with their explicit
 * truncation marker; the chart renderer explains that coverage limitation.
 */
export async function collectSavedViewResult(request, view, modelId, isCurrent = () => true) {
  const rows = [];
  const cursors = new Set();
  let first = null;
  let cursor = null;
  let bytes = 0;
  do {
    if (!isCurrent()) throw new StaleRequest();
    const response = await request({
      model_id: modelId,
      revision: view.revision,
      page_size: Math.min(100, view.max_rows),
      ...(cursor ? { cursor } : {}),
    });
    if (!isCurrent()) throw new StaleRequest();
    if (response?.view?.id !== view.id || response.view.revision !== view.revision)
      throw new ApiError('This saved view changed. Reload the list before running it again.', {
        code: 'view_changed',
      });
    const packet = verifyPage(response.result, view.dataset_id, modelId);
    if (
      !Number.isInteger(packet.result_rows) ||
      packet.result_rows < 0 ||
      packet.result_rows > Math.min(MAX_ROWS, view.max_rows) ||
      packet.returned_rows !== packet.rows.length ||
      typeof packet.truncated !== 'boolean'
    )
      throw new ApiError('The saved result did not match its bounded execution.', {
        code: 'invalid_response',
      });
    if (
      first &&
      (JSON.stringify(packet.columns) !== JSON.stringify(first.columns) ||
        packet.result_rows !== first.result_rows ||
        packet.truncated !== first.truncated)
    )
      throw new ApiError('The saved result changed while its pages were loading.', {
        code: 'invalid_response',
      });
    first ??= packet;
    bytes += new TextEncoder().encode(JSON.stringify(packet)).length;
    rows.push(...packet.rows);
    if (rows.length > MAX_ROWS || rows.length > first.result_rows || bytes > MAX_BYTES)
      throw new ApiError('This preview is too large. Reduce the query or its result cap.', {
        code: 'result_too_large',
      });
    cursor = packet.next_cursor ?? null;
    if (cursor && (typeof cursor !== 'string' || cursors.has(cursor) || !packet.rows.length))
      throw new ApiError('The saved result continuation was invalid.', {
        code: 'invalid_response',
      });
    if (cursor) cursors.add(cursor);
  } while (cursor);
  if (!first || rows.length !== first.result_rows)
    throw new ApiError('The complete retained result could not be loaded. Please run it again.', {
      code: 'incomplete_result',
    });
  return { ...first, rows, returned_rows: rows.length, next_cursor: null };
}

/**
 * Own persisted view metadata, owner management and lazy current-publication previews.
 *
 * The host provides the Overview bindings and onLoadSql callback. Server capability
 * fields decide which actions are shown; the server independently enforces every action.
 */
export class SavedViewsController {
  constructor(context, { onLoadSql = () => {} } = {}) {
    this.context = context;
    this.onLoadSql = onLoadSql;
    this.catalog = null;
    this.generation = 0;
    this.listGeneration = 0;
    this.views = [];
    this.cards = new Map();
    this.capabilities = { can_create: false, can_share: false };
    this.maxOwned = 50;
    this.cursorChain = [null];
    this.page = 0;
    this.loaded = false;
    this.loading = null;
    this.disposed = false;
    this.formWorking = false;
    this.formGeneration = 0;
    this.chartStates = new Map();
    this.chartObserver =
      typeof ResizeObserver === 'function'
        ? new ResizeObserver((entries) => this.resizeCharts(entries))
        : null;
    this.dialog = byId('saved-view-dialog');
    byId('saved-views-refresh')?.addEventListener('click', () => this.load(true));
    this.dialog?.addEventListener('cancel', (event) => {
      if (this.formWorking) event.preventDefault();
      else this.closeEditor();
    });
  }

  /** Invalidate previews on publication adoption while retaining persisted definitions. */
  configure(catalog) {
    this.catalog = catalog;
    this.generation++;
    this.listGeneration++;
    this.loaded = false;
    this.loading = null;
    this.page = 0;
    this.cursorChain = [null];
    this.views = [];
    this.clearCharts();
    this.cards.clear();
    this.capabilities = { can_create: false, can_share: false };
    byId('saved-views-list')?.replaceChildren();
    this.closeEditor();
    if (catalog) this.load();
  }

  /** Expose the latest server-returned authoring capability to the SQL save button. */
  canCreate() {
    return this.capabilities.can_create === true;
  }

  /** Request one metadata page only; listing views never runs their SQL automatically. */
  async load(force = false, page = this.page) {
    if (!this.catalog || this.disposed || !this.context.alive()) return;
    if (this.loading && !force) return this.loading;
    if (this.loaded && !force && page === this.page) return;
    const generation = this.generation;
    const listGeneration = ++this.listGeneration;
    const modelId = this.catalog.model_id;
    const isCurrent = () =>
      !this.disposed &&
      this.context.alive() &&
      generation === this.generation &&
      listGeneration === this.listGeneration &&
      this.catalog?.model_id === modelId;
    visible('saved-views-error', false);
    byId('saved-views-status').textContent = 'Loading saved views…';
    byId('saved-views-list').setAttribute('aria-busy', 'true');
    const promise = (async () => {
      try {
        const response = await this.context.client.request(
          `/saved-views${queryString({ limit: PAGE_SIZE, cursor: this.cursorChain[page] })}`,
        );
        if (!isCurrent()) return;
        if (
          !Array.isArray(response.views) ||
          response.views.length > PAGE_SIZE ||
          response.views.some(
            (view) => !validDefinition(view, new Set(this.catalog.datasets.map((item) => item.id))),
          ) ||
          (response.next_cursor !== null &&
            response.next_cursor !== undefined &&
            (typeof response.next_cursor !== 'string' || response.next_cursor.length > 16384)) ||
          typeof response.can_create !== 'boolean' ||
          typeof response.can_share !== 'boolean'
        )
          throw new ApiError('The saved-view list could not be read.', {
            code: 'invalid_response',
          });
        this.capabilities = {
          can_create: response.can_create,
          can_share: response.can_share,
        };
        this.maxOwned = Number.isInteger(response.max_owned) ? response.max_owned : 50;
        this.views = response.views;
        this.page = page;
        this.cursorChain[page + 1] = response.next_cursor ?? null;
        this.cursorChain.length = page + 2;
        this.loaded = true;
        this.renderList();
        this.context.onSavedViewCapabilities?.(this.capabilities);
      } catch (error) {
        if (error instanceof StaleRequest || !isCurrent()) return;
        failure(byId('saved-views-error'), error);
        byId('saved-views-status').textContent = 'Saved views are unavailable. Try refreshing.';
      } finally {
        if (isCurrent()) {
          byId('saved-views-list').setAttribute('aria-busy', 'false');
          this.loading = null;
        }
      }
    })();
    this.loading = promise;
    return promise;
  }

  /** Replace the current metadata page and discard its obsolete result DOM and buffers. */
  renderList() {
    const list = byId('saved-views-list');
    this.clearCharts();
    list.replaceChildren();
    for (const state of this.cards.values()) state.sequence++;
    this.cards.clear();
    byId('saved-views-description').textContent = this.capabilities.can_create
      ? `Create a query in SQL workspace, then save its table and optional time-series chart here. Each account can keep up to ${this.maxOwned} saved views.`
      : 'Shared tables and graphs appear here when their data is permitted for your account.';
    byId('saved-views-status').textContent = this.views.length
      ? `${this.views.length} saved view${this.views.length === 1 ? '' : 's'} on this page. Load a view to see its current data.`
      : this.capabilities.can_create
        ? 'No saved views yet. Save a query from SQL workspace to get started.'
        : 'No shared views are available for your account.';
    for (const view of this.views) {
      const card = element('article', 'panel saved-view-card');
      card.dataset.viewId = view.id;
      const heading = element('div', 'saved-view-card-heading');
      const title = element('h3', '', view.name);
      title.id = `saved-view-title-${view.id}`;
      card.setAttribute('aria-labelledby', title.id);
      const labels = view.shared_roles?.length
        ? view.is_owner
          ? `Shared with ${ROLES.filter(([id]) => view.shared_roles.includes(id))
              .map(([, label]) => label)
              .join(', ')}`
          : 'Shared with you'
        : 'Private';
      const metadata = element(
        'p',
        'saved-view-meta',
        `${datasetTitle(view.dataset_id)} · ${labels}${view.is_owner ? ' · Your view' : ''}`,
      );
      const summary = element('div');
      summary.append(title, metadata);
      const actions = element('div', 'saved-view-actions');
      const output = element('div', 'saved-view-result');
      const status = element('p', 'saved-view-meta', 'Not loaded.');
      status.setAttribute('role', 'status');
      const error = element('div', 'notice notice-error');
      error.setAttribute('role', 'alert');
      error.hidden = true;
      const state = { view, card, output, status, error, packet: null, page: 0, sequence: 0 };
      const run = button('Load view', () => this.runView(state));
      state.run = run;
      actions.append(run);
      if (view.can_edit) {
        actions.append(
          button('Edit view', () => this.openSave({ ...view, packet: state.packet }, view)),
        );
        actions.append(
          button('Open in SQL', () => {
            this.onLoadSql(view);
          }),
        );
      }
      if (view.can_delete) {
        state.deleteButton = button(
          'Delete',
          () => this.confirmDelete(state),
          'button button-quiet button-small',
        );
        actions.append(state.deleteButton);
      }
      heading.append(summary, actions);
      card.append(heading, status, error, output);
      const definition = element('details', 'saved-view-definition');
      const sql = element('pre', 'saved-view-sql', view.sql);
      sql.tabIndex = 0;
      sql.setAttribute('role', 'region');
      sql.setAttribute('aria-label', `${view.name} SQL definition`);
      definition.append(element('summary', '', 'Query definition'), sql);
      card.append(definition);
      list.append(card);
      this.cards.set(view.id, state);
    }
    const pager = byId('saved-views-pagination');
    pager.replaceChildren();
    if (this.page || this.cursorChain[this.page + 1]) {
      const previous = button('Previous views', () => this.load(true, this.page - 1));
      previous.disabled = this.page === 0;
      const next = button('Next views', () => this.load(true, this.page + 1));
      next.disabled = !this.cursorChain[this.page + 1];
      pager.append(previous, element('span', 'page-label', `Page ${this.page + 1}`), next);
    }
  }

  /** Execute lazily as the current viewer and assemble only the retained bounded result. */
  async runView(state) {
    if (!this.catalog || !this.context.alive()) return;
    const generation = this.generation;
    const sequence = ++state.sequence;
    const modelId = this.catalog.model_id;
    const isCurrent = () =>
      !this.disposed &&
      this.context.alive() &&
      generation === this.generation &&
      state.sequence === sequence &&
      this.cards.get(state.view.id) === state &&
      this.catalog?.model_id === modelId;
    state.run.disabled = true;
    state.card.setAttribute('aria-busy', 'true');
    state.error.hidden = true;
    state.status.textContent = 'Loading the complete retained result…';
    this.detachChart(state);
    state.output.replaceChildren();
    state.packet = null;
    try {
      const packet = await collectSavedViewResult(
        (body) =>
          this.context.queue.run(
            () =>
              this.context.client.request(`/saved-views/${state.view.id}/run`, {
                method: 'POST',
                body,
              }),
            isCurrent,
          ),
        state.view,
        modelId,
        isCurrent,
      );
      if (!isCurrent()) return;
      this.releaseOldPreview(state);
      state.packet = packet;
      state.loadedAt = Date.now();
      state.page = 0;
      this.renderResult(state);
    } catch (error) {
      if (error instanceof StaleRequest || !isCurrent()) return;
      state.status.textContent = 'This view could not be loaded.';
      failure(state.error, error);
    } finally {
      if (isCurrent()) {
        state.run.disabled = false;
        state.run.textContent = state.packet ? 'Reload view' : 'Load view';
        state.card.setAttribute('aria-busy', 'false');
      }
    }
  }

  /** Bound in-memory previews while leaving every persisted definition available to reload. */
  releaseOldPreview(current) {
    const loaded = [...this.cards.values()]
      .filter((state) => state !== current && state.packet)
      .sort((left, right) => left.loadedAt - right.loadedAt);
    while (loaded.length >= MAX_LOADED) {
      const state = loaded.shift();
      this.detachChart(state);
      state.packet = null;
      state.output.replaceChildren();
      state.status.textContent =
        'Preview released to keep the workspace bounded. Load it again when needed.';
      state.run.textContent = 'Load view';
    }
  }

  /** Draw a complete retained chart with a paged exact-value table as its keyboard alternative. */
  renderResult(state) {
    const { packet, view } = state;
    const tableId = `saved-view-table-${view.id}`;
    this.detachChart(state);
    state.output.replaceChildren();
    state.status.textContent = `${packet.rows.length.toLocaleString()} retained rows · Version ${packet.model_id.slice(0, 8)}${packet.truncated ? ' · Capped result; refine the SQL for complete coverage.' : ''}`;
    if (!packet.rows.length) {
      state.output.append(
        element('p', 'empty-state', 'This query returned no rows on the active publication.'),
      );
      return;
    }
    if (view.chart?.enabled) {
      const chart = element('div', 'saved-view-chart');
      // Measure the mounted container rather than drawing a desktop fallback into detached DOM.
      state.output.append(chart);
      state.chartElement = chart;
      state.chartWidth = chart.clientWidth;
      if (this.drawChart(state)) {
        this.chartStates.set(chart, state);
        this.chartObserver?.observe(chart);
      } else this.detachChart(state);
    }
    const table = element('table', 'data-table');
    table.id = tableId;
    const start = state.page * TABLE_PAGE_SIZE;
    const page = { ...packet, rows: packet.rows.slice(start, start + TABLE_PAGE_SIZE) };
    renderTable(table, page, `${view.name}: exact values, page ${state.page + 1}`, {
      showTypes: false,
    });
    const wrap = element('div', 'table-wrap');
    wrap.tabIndex = 0;
    wrap.setAttribute('role', 'region');
    wrap.setAttribute(
      'aria-label',
      `${view.name} table, page ${state.page + 1}, scroll for additional columns`,
    );
    wrap.append(table);
    state.output.append(wrap);
    const pager = element('div', 'pagination');
    const previous = button('Previous rows', () => {
      state.page--;
      this.renderResult(state);
      state.output.querySelector('.table-wrap')?.focus();
    });
    previous.disabled = state.page === 0;
    const next = button('Next rows', () => {
      state.page++;
      this.renderResult(state);
      state.output.querySelector('.table-wrap')?.focus();
    });
    next.disabled = start + TABLE_PAGE_SIZE >= packet.rows.length;
    const exportButton = button('Export this page', () =>
      exportPage(page, view.dataset_id, packet.model_id),
    );
    const actions = element('div', 'pagination-actions');
    actions.append(previous, element('span', 'page-label', `Page ${state.page + 1}`), next);
    pager.append(
      element(
        'p',
        'pagination-note',
        'Chart covers all retained rows; table pages keep exact values.',
      ),
      actions,
      exportButton,
    );
    state.output.append(pager);
  }

  /** Redraw approved retained data for a changed visible width without querying the service. */
  resizeCharts(entries) {
    if (this.disposed || !this.context.alive() || this.context.isView?.('overview') === false)
      return;
    for (const entry of entries) {
      const state = this.chartStates.get(entry.target);
      if (
        !state ||
        this.cards.get(state.view.id) !== state ||
        state.chartElement !== entry.target ||
        !entry.target.isConnected ||
        !state.packet ||
        state.packet.model_id !== this.catalog?.model_id ||
        !state.view.chart?.enabled
      )
        continue;
      const width = entry.contentRect.width;
      if (width > 0 && Math.abs(width - (state.chartWidth || 0)) > 1) {
        state.chartWidth = width;
        if (!this.drawChart(state)) this.detachChart(state);
      }
    }
  }

  /** Replace only the chart figure; table paging, exact rows and focus context remain intact. */
  drawChart(state) {
    const chart = state.chartElement;
    if (!chart || !state.packet) return false;
    const restoreFocus = chart.contains(chart.ownerDocument.activeElement);
    try {
      renderQueryChart(chart, state.packet, state.view.chart, {
        title: state.view.name,
        tableId: `saved-view-table-${state.view.id}`,
      });
      if (restoreFocus) chart.querySelector('button')?.focus({ preventScroll: true });
      return true;
    } catch (error) {
      chart.replaceChildren(element('p', 'notice notice-subtle', error.message));
      return false;
    }
  }

  /** Retire an individual chart when its packet or result DOM is replaced or evicted. */
  detachChart(state) {
    if (state.chartElement) {
      this.chartObserver?.unobserve(state.chartElement);
      this.chartStates.delete(state.chartElement);
    }
    state.chartElement = null;
    state.chartWidth = null;
  }

  /** Disconnect the shared observer before replacing a page or retiring its publication. */
  clearCharts() {
    this.chartObserver?.disconnect();
    this.chartStates.clear();
    for (const state of this.cards.values()) {
      state.chartElement = null;
      state.chartWidth = null;
    }
  }

  /** Ask for deletion inline; only an explicit second action sends the revision-bound request. */
  confirmDelete(state) {
    state.card.querySelector('.saved-view-confirm')?.remove();
    const confirmation = element('div', 'notice saved-view-confirm');
    confirmation.setAttribute('role', 'group');
    confirmation.setAttribute('aria-label', `Delete ${state.view.name}`);
    const cancel = button('Keep view', () => {
      confirmation.remove();
      state.deleteButton?.focus();
    });
    const remove = button('Delete saved view', async () => {
      const generation = this.generation;
      remove.disabled = cancel.disabled = true;
      try {
        await this.context.client.request(`/saved-views/${state.view.id}`, {
          method: 'DELETE',
          body: { revision: state.view.revision },
        });
        if (this.disposed || !this.context.alive() || generation !== this.generation) return;
        state.sequence++;
        this.detachChart(state);
        state.packet = null;
        this.context.notice?.('Saved view deleted.');
        await this.load(true, 0);
        this.focusListStatus();
      } catch (error) {
        if (this.disposed || !this.context.alive() || generation !== this.generation) return;
        remove.disabled = cancel.disabled = false;
        failure(state.error, error);
      }
    });
    confirmation.append(
      element(
        'p',
        '',
        `Delete “${state.view.name}”? This removes its saved query and chart definition.`,
      ),
      cancel,
      remove,
    );
    state.card.append(confirmation);
    cancel.focus();
  }

  /** Open a private-by-default definition form from an executed SQL question or owned view. */
  async openSave(specification, existing = null) {
    if (!this.loaded) await this.load();
    if (this.disposed || !this.catalog || !this.context.alive()) return;
    if (existing ? !existing.can_edit : !this.capabilities.can_create) {
      this.context.notice?.(
        'Your current permissions do not allow creating or editing saved queries.',
      );
      return;
    }
    const dataset = this.catalog.datasets.find((item) => item.id === specification.dataset_id);
    if (!dataset) return;
    const generation = this.generation;
    const formGeneration = ++this.formGeneration;
    const modelId = this.catalog.model_id;
    const dialog = this.dialog;
    if (!dialog) return;
    this.returnFocus = document.activeElement;
    dialog.replaceChildren();
    const form = element('form', 'saved-view-form');
    const title = element('h2', '', existing ? 'Edit saved view' : 'Save table and graph');
    title.id = 'saved-view-dialog-title';
    const description = element(
      'p',
      'saved-view-meta',
      'Save this query as a named Overview view. Results run against the publication you are currently exploring.',
    );
    const controls = element('fieldset', 'saved-view-fields');
    controls.append(element('legend', 'sr-only', 'View settings'));
    const name = element('input');
    name.type = 'text';
    name.required = true;
    name.maxLength = 100;
    name.value = existing?.name ?? '';
    name.autocomplete = 'off';
    const sql = element('pre', 'saved-view-sql', specification.sql);
    sql.tabIndex = 0;
    sql.setAttribute('role', 'region');
    sql.setAttribute('aria-label', 'Saved SQL definition');
    const sqlDetails = element('details');
    sqlDetails.append(element('summary', '', `Query · ${datasetTitle(dataset.id)}`), sql);
    controls.append(field('View name', name), sqlDetails);
    const chartEnabled = element('input');
    chartEnabled.type = 'checkbox';
    chartEnabled.checked =
      specification.chart?.enabled === true || specification.chart_config?.enabled === true;
    const chartLabel = element('label', 'saved-view-checkbox');
    chartLabel.append(chartEnabled, element('span', '', 'Include a time-series graph'));
    const settings = element('div', 'saved-view-chart-fields');
    const choices = specification.packet
      ? chartOptions(specification.packet)
      : {
          date_columns: dataset.schema
            .filter(([, type]) => /^(?:DATE|TIMESTAMP)/i.test(type))
            .map(([column, type]) => ({ name: column, type })),
          numeric_columns: dataset.schema
            .filter(([, type]) =>
              /^(?:U?(?:TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|INT\d*|DECIMAL|NUMERIC|DOUBLE|FLOAT|REAL)/i.test(
                type,
              ),
            )
            .map(([column, type]) => ({ name: column, type })),
          series_columns: dataset.schema.map(([column, type]) => ({ name: column, type })),
        };
    const currentChart = specification.chart ?? specification.chart_config ?? {};
    const chartInput = (key, label, values, optional = false) => {
      const input = element('input');
      const list = element('datalist');
      list.id = `saved-view-${key}-suggestions`;
      for (const column of values) {
        const option = element('option');
        option.value = column.name;
        option.label = `${columnTitle(column.name)} · ${column.type}`;
        list.append(option);
      }
      input.setAttribute('list', list.id);
      input.autocomplete = 'off';
      input.maxLength = 128;
      input.value = currentChart[key] ?? (optional ? '' : (values[0]?.name ?? ''));
      input.placeholder = optional ? 'One series' : 'Exact result column name';
      const wrapper = field(label, input);
      wrapper.append(list);
      settings.append(wrapper);
      return input;
    };
    const dateColumn = chartInput('date_column', 'Date column', choices.date_columns);
    const valueColumn = chartInput('value_column', 'Numeric value column', choices.numeric_columns);
    const seriesColumn = chartInput(
      'series_column',
      'Series column (optional)',
      choices.series_columns,
      true,
    );
    settings.append(
      element(
        'p',
        'saved-view-meta',
        'Use result column names, including SQL aliases. Each date and series pair must identify one row; aggregate duplicates in SQL.',
      ),
    );
    const syncChart = () => {
      settings.hidden = !chartEnabled.checked;
      dateColumn.required = valueColumn.required = chartEnabled.checked;
    };
    chartEnabled.addEventListener('change', syncChart);
    syncChart();
    controls.append(chartLabel, settings);
    const audience = element('fieldset', 'saved-view-audience');
    audience.append(element('legend', '', 'Who can see this view?'));
    const roleInputs = [];
    const allowedPersonas = dataset.allowed_personas?.map((role) => role.toLowerCase());
    if (this.capabilities.can_share) {
      for (const [role, label] of ROLES) {
        const checkbox = element('input');
        checkbox.type = 'checkbox';
        checkbox.value = role;
        checkbox.checked = existing?.shared_roles?.includes(role) ?? false;
        checkbox.disabled = !!allowedPersonas && !allowedPersonas.includes(role);
        const wrapper = element('label', 'saved-view-checkbox');
        wrapper.append(checkbox, element('span', '', label));
        audience.append(wrapper);
        roleInputs.push(checkbox);
      }
      audience.append(
        element(
          'p',
          'saved-view-meta',
          'Leave all unchecked for a private view. Sharing includes the query text and requires the recipient to have dataset access.',
        ),
      );
    } else
      audience.append(element('p', 'saved-view-meta', 'This view is private to your account.'));
    controls.append(audience);
    const error = element('div', 'notice notice-error');
    error.setAttribute('role', 'alert');
    error.hidden = true;
    const status = element('p', 'saved-view-meta');
    status.setAttribute('role', 'status');
    const cancel = button('Cancel', () => this.closeEditor());
    const submit = element('button', 'button', existing ? 'Save changes' : 'Save view');
    submit.type = 'submit';
    const actions = element('div', 'saved-view-actions');
    actions.append(cancel, submit);
    form.append(title, description, controls, error, status, actions);
    dialog.append(form);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (this.formWorking || !form.reportValidity()) return;
      const isCurrent = () =>
        !this.disposed &&
        this.context.alive() &&
        generation === this.generation &&
        formGeneration === this.formGeneration &&
        this.catalog?.model_id === modelId;
      if (!isCurrent()) return;
      const body = {
        name: name.value.trim(),
        dataset_id: specification.dataset_id,
        sql: specification.sql,
        max_rows: specification.max_rows,
        model_id: modelId,
        chart: chartEnabled.checked
          ? {
              enabled: true,
              date_column: dateColumn.value.trim(),
              value_column: valueColumn.value.trim(),
              series_column: seriesColumn.value.trim() || null,
            }
          : { enabled: false },
        shared_roles: roleInputs
          .filter((input) => input.checked && !input.disabled)
          .map((input) => input.value),
        ...(existing ? { revision: existing.revision } : {}),
      };
      this.formWorking = true;
      controls.disabled = cancel.disabled = submit.disabled = true;
      error.hidden = true;
      status.textContent = 'Validating the query and saving this view…';
      try {
        await this.context.queue.run(
          () =>
            this.context.client.request(existing ? `/saved-views/${existing.id}` : '/saved-views', {
              method: existing ? 'PUT' : 'POST',
              body,
            }),
          isCurrent,
        );
        if (!isCurrent()) return;
        this.formWorking = false;
        this.closeEditor();
        this.context.notice?.(existing ? 'Saved view updated.' : 'Saved view added to Overview.');
        await this.load(true, 0);
        this.focusListStatus();
      } catch (caught) {
        if (caught instanceof StaleRequest || !isCurrent()) return;
        failure(error, caught);
        status.textContent = '';
      } finally {
        if (isCurrent()) {
          this.formWorking = false;
          controls.disabled = cancel.disabled = submit.disabled = false;
        }
      }
    });
    if (!dialog.open) dialog.showModal();
    name.focus();
  }

  /** Clear form values and return focus without retaining another user's SQL in hidden DOM. */
  closeEditor() {
    this.formGeneration++;
    this.formWorking = false;
    if (this.dialog?.open) this.dialog.close();
    this.dialog?.replaceChildren();
    const previous = this.returnFocus;
    this.returnFocus = null;
    if (previous?.isConnected) previous.focus();
  }

  /** Preserve a useful keyboard location when a metadata refresh replaces Overview controls. */
  focusListStatus() {
    if (!this.context.isView?.('overview')) return;
    const status = byId('saved-views-status');
    if (status) {
      status.tabIndex = -1;
      status.focus();
    }
  }

  /** Retire all private metadata, result buffers and dialog state on session disposal. */
  dispose() {
    this.disposed = true;
    this.generation++;
    this.listGeneration++;
    this.closeEditor();
    this.views = [];
    this.clearCharts();
    this.cards.clear();
    this.catalog = null;
    this.capabilities = { can_create: false, can_share: false };
    byId('saved-views-list')?.replaceChildren();
    byId('saved-views-pagination')?.replaceChildren();
    byId('saved-views-error')?.replaceChildren();
    if (byId('saved-views-status')) byId('saved-views-status').textContent = '';
  }
}
