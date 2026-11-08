/**
 * @module sql
 * Manage SQL drafts, bounded result pages and session-scoped text history.
 *
 * The backend owns parsing, permissions, execution and row/resource limits.
 * Client schema insertion quotes identifiers only. Executed request state is
 * separate from edited text, and no result rows are persisted in browser storage.
 */
import { ApiError, StaleRequest, verifyPage } from './api.js';
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
} from './dom.js';
import { renderTable, exportPage } from './table.js';
import { sqlExamples } from './sql_examples.js';
import { collectRetainedResult } from './query_results.js';
import { chartOptions, renderQueryChart } from './query_chart.js';

export const HISTORY_PREFIX = 'outage-explorer:history:';
/**
 * Remove this application's other history scopes without clearing unrelated browser storage.
 * @param {Storage} storage Usually sessionStorage.
 * @param {string|null} [keepKey] Exact current scope to preserve on same-session reload.
 */
export function retireHistoryScopes(storage, keepKey = null) {
  for (let index = storage.length - 1; index >= 0; index--) {
    const key = storage.key(index);
    if (key?.startsWith(HISTORY_PREFIX) && key !== keepKey) storage.removeItem(key);
  }
}
/**
 * Quote a SQL identifier by doubling embedded quotes; this does not validate SQL syntax.
 */
export const quoteIdentifier = (value) => `"${String(value).replaceAll('"', '""')}"`;
/**
 * Build a supported single-source example from permitted schema and full primary-key order.
 */
export function exampleSql(dataset) {
  const names = dataset.schema.map((column) => column[0]);
  const preferred = [
    'period',
    'facility_id',
    'generator_id',
    'capacity_mw',
    'outage_mw',
    'offline_pct',
  ];
  const selected = preferred.filter((name) => names.includes(name));
  const projection = selected.length ? selected.map(quoteIdentifier).join(', ') : '*';
  const ordering = dataset.primary_key
    .map((name) => quoteIdentifier(name) + (name === 'period' ? ' DESC' : ''))
    .join(', ');
  return `SELECT ${projection}\nFROM ${quoteIdentifier(dataset.id)}\nORDER BY ${ordering}\nLIMIT 100`;
}

/**
 * Retain at most ten permitted history entries, with a bounded SQL-text length.
 *
 * Copy only expected text/status/timestamp fields; discard result-row, credential
 * and other unexpected properties. SQL text itself is not scrubbed for secrets,
 * and the timestamp's string length is not bounded here.
 */
export function cleanHistory(value, allowedIds) {
  if (!Array.isArray(value)) return [];
  return value
    .filter(
      (item) =>
        item &&
        allowedIds.includes(item.dataset_id) &&
        typeof item.sql === 'string' &&
        item.sql.length <= 16384 &&
        typeof item.created_at === 'string' &&
        ['success', 'error'].includes(item.status),
    )
    .slice(0, 10)
    .map((item) => ({
      dataset_id: item.dataset_id,
      sql: item.sql,
      created_at: item.created_at,
      status: item.status,
    }));
}

/**
 * Best-effort removal of all scoped query text; callers clear in-memory state separately.
 */
export function clearPrivateHistory() {
  try {
    retireHistoryScopes(sessionStorage);
  } catch {
    /* Storage can be unavailable; in-memory state is cleared separately. */
  }
}

/**
 * Own per-dataset drafts, captured executions, capped cursor pages and scoped query history.
 */
export class SqlController {
  /**
   * Bind SQL controls and retire obsolete server-session storage scopes before reading history.
   */
  constructor(context) {
    this.context = context;
    this.sequence = 0;
    this.catalog = null;
    this.packet = null;
    this.applied = null;
    this.page = 0;
    this.cursors = [null];
    this.history = [];
    this.drafts = new Map();
    this.working = false;
    this.editor = null;
    this.disposed = false;
    this.examples = [];
    this.chartGeneration = 0;
    this.chartSelectionGeneration = 0;
    this.chartPossible = false;
    this.completePacket = null;
    this.firstPacket = null;
    this.chartShownConfig = null;
    this.chartWidth = 0;
    this.chartObserver =
      typeof ResizeObserver === 'function'
        ? new ResizeObserver((entries) => {
            const width = entries[0]?.contentRect.width;
            if (
              width > 0 &&
              Math.abs(width - this.chartWidth) > 1 &&
              this.completePacket &&
              this.chartShownConfig
            ) {
              this.chartWidth = width;
              renderQueryChart(
                byId('sql-query-chart'),
                this.completePacket,
                this.chartShownConfig,
                {
                  title: 'Query results over time',
                  tableId: 'sql-table',
                },
              );
            }
          })
        : null;
    this.chartObserver?.observe(byId('sql-query-chart'));
    this.historyKey = HISTORY_PREFIX + context.scope.session_scope;
    try {
      retireHistoryScopes(sessionStorage, this.historyKey);
    } catch {
      /* Memory-only history remains safe. */
    }
    byId('sql-form').addEventListener('submit', (event) => {
      event.preventDefault();
      this.run();
    });
    byId('sql-dataset').addEventListener('change', () => this.selectDataset());
    byId('sql-editor').addEventListener('input', () => this.draftChanged());
    for (const id of ['sql-max-rows', 'sql-page-size'])
      byId(id).addEventListener('change', () => this.updatePager());
    byId('sql-editor').addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        if (!byId('sql-run').disabled) this.run();
      }
    });
    byId('sql-example').addEventListener('click', () => {
      if (this.dataset()) {
        this.savedViewEditing = null;
        this.setSql(this.examples[0]?.sql ?? exampleSql(this.dataset()));
        this.focusEditor();
      }
    });
    byId('sql-prev').addEventListener('click', () => this.send(this.page - 1));
    byId('sql-next').addEventListener('click', () => this.send(this.page + 1));
    byId('sql-export').addEventListener('click', () =>
      exportPage(this.packet, this.applied?.dataset_id, this.catalog?.model_id),
    );
    byId('sql-load-history').addEventListener('click', () => this.restoreHistory());
    byId('sql-chart-show').addEventListener('click', () => this.showChart());
    byId('sql-save-view').addEventListener('click', () => this.saveView());
    for (const id of ['sql-chart-date', 'sql-chart-value', 'sql-chart-series'])
      byId(id).addEventListener('change', () => {
        this.chartSelectionGeneration++;
        this.chartShownConfig = null;
        byId('sql-query-chart').replaceChildren();
        visible('sql-chart-status', false);
      });
    this.initEditor();
  }

  /** Enhance the synchronized textarea with a local editor, retaining a usable fallback. */
  async initEditor() {
    try {
      const { createSqlEditor } = await import('./sql_editor.bundle.js');
      if (this.disposed || !this.context.alive()) return;
      this.editor = createSqlEditor({
        textarea: byId('sql-editor'),
        styleNonce: byId('sql-editor-host').dataset.styleNonce,
        onChange: () => this.draftChanged(),
        onRun: () => {
          if (!byId('sql-run').disabled) this.run();
        },
      });
      this.editor.setDataset(this.dataset() ?? null);
      visible('sql-editor-fallback', false);
    } catch {
      // Loading failure leaves the ordinary labeled textarea and server validation available.
      this.editor?.destroy();
      this.editor = null;
      if (!this.disposed) visible('sql-editor-fallback', true);
    }
  }

  /** Synchronize programmatic changes with the enhanced editor and its plain-text backing. */
  setSql(value, options = {}) {
    if (this.editor) this.editor.setValue(value, options);
    else byId('sql-editor').value = value;
    this.draftChanged();
  }

  /** Keep the draft indicator and starter selection independent of the last executed query. */
  draftChanged() {
    this.updatePager();
    for (const button of byId('sql-examples').querySelectorAll('[data-example-level]')) {
      const example = this.examples.find((item) => item.level === button.dataset.exampleLevel);
      button.setAttribute('aria-pressed', String(example?.sql === byId('sql-editor').value));
    }
  }

  /** Move focus to the active editing surface after explicit insertion or query loading. */
  focusEditor() {
    if (this.editor) this.editor.focus();
    else byId('sql-editor').focus();
  }

  /** Render three schema-compatible starters without adding fabricated execution history. */
  renderExamples(dataset) {
    this.examples = sqlExamples(dataset);
    const container = byId('sql-examples');
    container.replaceChildren();
    for (const example of this.examples) {
      const button = element('button', 'sql-example-card');
      button.type = 'button';
      button.dataset.exampleLevel = example.level;
      button.setAttribute('aria-pressed', 'false');
      button.append(
        element('strong', '', example.title),
        element('span', 'sql-example-description', example.description),
      );
      button.addEventListener('click', () => {
        this.savedViewEditing = null;
        this.setSql(example.sql);
        this.focusEditor();
      });
      container.append(button);
    }
  }

  /**
   * Resolve the selected source from the pinned authorized catalog, not a local role matrix.
   */
  dataset() {
    return this.catalog?.datasets.find((dataset) => dataset.id === byId('sql-dataset').value);
  }
  /**
   * Invalidate old results, preserve compatible drafts and restore only currently permitted history.
   */
  configure(catalog) {
    if (this.selected) this.drafts.set(this.selected, byId('sql-editor').value);
    this.catalog = catalog;
    this.sequence++;
    this.packet = null;
    this.applied = null;
    const selected =
      this.selected && catalog.datasets.some((dataset) => dataset.id === this.selected)
        ? this.selected
        : 'fleet_daily';
    fillDatasets(byId('sql-dataset'), catalog.datasets, selected);
    try {
      this.history = cleanHistory(
        JSON.parse(sessionStorage.getItem(this.historyKey) || '[]'),
        catalog.datasets.map((dataset) => dataset.id),
      );
    } catch {
      this.history = [];
    }
    this.renderHistory();
    this.selectDataset();
  }

  /**
   * Switch source without executing text, restoring its draft/example and identifier suggestions.
   * @param {string|null} [draft] Explicit text loaded from current-session history.
   */
  selectDataset(draft = null) {
    this.savedViewEditing = null;
    this.resetChart();
    if (this.selected) this.drafts.set(this.selected, byId('sql-editor').value);
    this.selected = byId('sql-dataset').value;
    this.sequence++;
    this.packet = null;
    this.applied = null;
    this.working = false;
    const dataset = this.dataset();
    if (!dataset) return;
    this.renderExamples(dataset);
    this.editor?.setDataset(dataset);
    this.setSql(
      draft ?? this.drafts.get(this.selected) ?? this.examples[0]?.sql ?? exampleSql(dataset),
      {
        resetHistory: true,
      },
    );
    schema(byId('sql-schema'), dataset, (name) => this.insert(name));
    state('sql', 'empty');
    setText('sql-empty', 'Your query results will appear here. Run a read-only SELECT to begin.');
    visible('sql-truncated', false);
    byId('sql-run').disabled = false;
    this.updatePager();
  }

  /**
   * Insert one quoted schema identifier at the editor selection and invalidate incompatible paging.
   */
  insert(name) {
    if (this.editor) {
      this.editor.insertIdentifier(name);
      this.draftChanged();
      return;
    }
    const editor = byId('sql-editor');
    editor.setRangeText(quoteIdentifier(name), editor.selectionStart, editor.selectionEnd, 'end');
    editor.focus();
    this.draftChanged();
  }

  /**
   * Validate a nonempty bounded draft, capture SQL/caps/model and reset the result cursor chain.
   *
   * Backend compilation, actor policy and execution limits remain authoritative.
   */
  run() {
    if (!this.catalog || !this.dataset()) {
      state('sql', 'error', new Error('Load the dataset catalog before running a query.'));
      return;
    }
    const sql = byId('sql-editor').value;
    if (!sql.trim()) {
      state('sql', 'error', new Error('Enter a SELECT query before running it.'));
      return;
    }
    if (new TextEncoder().encode(sql).length > 16384) {
      state('sql', 'error', new Error('Keep the query within 16 KiB.'));
      return;
    }
    this.resetChart();
    // Freeze the question for paging/history; editing the draft does not mutate an in-flight payload.
    this.applied = {
      dataset_id: byId('sql-dataset').value,
      sql,
      max_rows: Number(byId('sql-max-rows').value),
      page_size: Math.min(Number(byId('sql-page-size').value), Number(byId('sql-max-rows').value)),
      model_id: this.catalog.model_id,
    };
    this.page = 0;
    this.cursors = [null];
    this.send(0);
  }

  /**
   * Request a page of the same capped execution and discard replies from obsolete generations.
   *
   * Captured applied text labels the result/history; edits do not change what an
   * in-flight request means. Only the first page records one success/error history entry.
   */
  async send(page) {
    if (!this.applied || page < 0 || (page > 0 && !this.cursors[page])) return;
    const applied = { ...this.applied },
      sequence = ++this.sequence;
    // Captured execution identity remains independent of editable SQL and result-cap controls.
    const isCurrent = () =>
      this.context.alive() &&
      sequence === this.sequence &&
      this.catalog?.model_id === applied.model_id;
    this.packet = null;
    this.working = true;
    state('sql', 'loading');
    this.updatePager();
    try {
      const payload = { ...applied, ...(this.cursors[page] ? { cursor: this.cursors[page] } : {}) };
      const result = await this.context.queue.run(
        () => this.context.client.request('/query', { method: 'POST', body: payload }),
        isCurrent,
      );
      if (!isCurrent()) return;
      this.packet = verifyPage(result, applied.dataset_id, applied.model_id);
      if (page === 0) {
        const firstExecutionPage = !this.firstPacket;
        this.firstPacket = this.packet;
        if (firstExecutionPage) this.configureChart(this.packet);
      }
      this.page = page;
      this.cursors[page + 1] = result.next_cursor;
      this.cursors.length = page + 2;
      renderTable(
        byId('sql-table'),
        result,
        `SQL results for ${datasetTitle(applied.dataset_id)}, page ${page + 1}`,
      );
      state('sql', result.rows.length ? 'content' : 'empty');
      if (!result.rows.length)
        setText(
          'sql-empty',
          'The query ran successfully and returned no rows. Try a wider date range or different filters.',
        );
      visible('sql-truncated', result.truncated);
      if (result.truncated)
        setText(
          'sql-truncated',
          `The result is limited to ${applied.max_rows.toLocaleString()} rows. Refine your query for a complete answer.`,
        );
      if (page === 0) this.saveHistory(applied, 'success');
    } catch (error) {
      if (error instanceof StaleRequest || !isCurrent()) return;
      state('sql', 'error', error);
      showError('sql-error', error, () => this.run());
      if (page === 0) this.saveHistory(applied, 'error');
    } finally {
      if (isCurrent()) {
        this.working = false;
        this.updatePager();
      }
    }
  }

  /**
   * Compare edited text/caps with the displayed execution and disable incompatible continuation.
   */
  updatePager() {
    const changed =
      this.applied &&
      (byId('sql-editor').value !== this.applied.sql ||
        Number(byId('sql-max-rows').value) !== this.applied.max_rows ||
        Math.min(Number(byId('sql-page-size').value), Number(byId('sql-max-rows').value)) !==
          this.applied.page_size);
    byId('sql-run').disabled = !!this.working;
    byId('sql-prev').disabled = this.working || !this.packet || this.page === 0 || !!changed;
    byId('sql-next').disabled = this.working || !this.packet?.next_cursor || !!changed;
    byId('sql-export').disabled = this.working || !this.packet?.rows.length;
    byId('sql-save-view').disabled =
      this.working || !this.packet || !!changed || !this.context.savedViews?.canCreate();
    visible('sql-save-view', !!this.context.savedViews?.canCreate());
    setText('sql-save-view', this.savedViewEditing ? 'Update saved view' : 'Save to overview');
    byId('sql-save-view').title = changed
      ? 'Run the edited query before saving it.'
      : 'Save the last successful query as a named view.';
    byId('sql-chart-show').disabled = this.working || !this.packet || !this.chartPossible;
    setText(
      'sql-page-label',
      this.packet
        ? `Page ${this.page + 1} · ${this.packet.result_rows.toLocaleString()} retained rows`
        : 'No results yet',
    );
    if (this.packet)
      setText(
        'sql-result-meta',
        `${datasetTitle(this.applied.dataset_id)} · ${this.packet.returned_rows} rows shown · Version ${this.applied.model_id.slice(0, 8)}${this.packet.cache_hit ? ' · Cached' : ''}${changed ? ' · Query or limits changed; results show the last run.' : ''}`,
      );
  }

  /**
   * Store a deduplicated ten-entry text/status history in the actual server-session scope.
   *
   * Storage failure degrades to memory-only history; no result rows are persisted.
   */
  saveHistory(applied, status) {
    this.history = [
      {
        dataset_id: applied.dataset_id,
        sql: applied.sql,
        status,
        created_at: new Date().toISOString(),
      },
      ...this.history.filter(
        (item) => item.sql !== applied.sql || item.dataset_id !== applied.dataset_id,
      ),
    ].slice(0, 10);
    try {
      sessionStorage.setItem(this.historyKey, JSON.stringify(this.history));
    } catch {
      /* Memory-only history remains usable. */
    }
    this.renderHistory();
  }
  /**
   * Render text-only summaries of current-session queries without executing their text.
   */
  renderHistory() {
    const select = byId('sql-history');
    select.replaceChildren();
    const placeholder = element(
      'option',
      '',
      this.history.length ? 'Choose a recent query' : 'No queries in this session',
    );
    placeholder.value = '';
    select.append(placeholder);
    this.history.forEach((entry, index) => {
      const label = `${datasetTitle(entry.dataset_id)} · ${entry.status === 'error' ? 'Error · ' : ''}${entry.sql.replace(/\s+/g, ' ').slice(0, 85)}`;
      const option = element('option', '', label);
      option.value = String(index);
      select.append(option);
    });
    byId('sql-load-history').disabled = !this.history.length;
  }
  /**
   * Load one retained query into its source draft and focus the editor, without running it.
   */
  restoreHistory() {
    const index = byId('sql-history').value;
    if (index === '' || !this.history[Number(index)]) return;
    const entry = this.history[Number(index)];
    byId('sql-dataset').value = entry.dataset_id;
    this.selectDataset(entry.sql);
    this.focusEditor();
  }

  /** Retire result/chart state whenever the source or executed SQL question changes. */
  resetChart() {
    this.chartGeneration++;
    this.chartSelectionGeneration++;
    this.chartPossible = false;
    this.completePacket = null;
    this.firstPacket = null;
    this.chartShownConfig = null;
    byId('sql-query-chart').replaceChildren();
    visible('sql-chart-status', false);
  }

  /** Offer only date/numeric/category columns present in the actual SQL output. */
  configureChart(packet) {
    const options = chartOptions(packet);
    for (const [id, fields, optional] of [
      ['sql-chart-date', options.date_columns, false],
      ['sql-chart-value', options.numeric_columns, false],
      ['sql-chart-series', options.series_columns, true],
    ]) {
      const select = byId(id);
      select.replaceChildren();
      if (optional) {
        const option = element('option', '', 'One series');
        option.value = '';
        select.append(option);
      }
      for (const field of fields) {
        const name = typeof field === 'string' ? field : field.name;
        const option = element('option', '', name);
        option.value = name;
        select.append(option);
      }
    }
    const possible = options.date_columns.length && options.numeric_columns.length;
    const savedChart = this.savedViewEditing?.chart;
    if (savedChart?.enabled) {
      for (const [id, key] of [
        ['sql-chart-date', 'date_column'],
        ['sql-chart-value', 'value_column'],
        ['sql-chart-series', 'series_column'],
      ]) {
        const select = byId(id);
        if ([...select.options].some((item) => item.value === (savedChart[key] ?? '')))
          select.value = savedChart[key] ?? '';
      }
    }
    this.chartPossible = !!possible;
    byId('sql-chart-show').disabled = !possible;
    setText(
      'sql-chart-hint',
      possible
        ? 'Charts use all retained query rows, sorted by time. Repeated dates need a series field.'
        : 'For a time graph, return a date column and a numeric value. This query can still be saved as a table.',
    );
  }

  /** Describe a chart independently from the SQL result's exact-value table. */
  chartConfig() {
    return {
      enabled: true,
      date_column: byId('sql-chart-date').value,
      value_column: byId('sql-chart-value').value,
      series_column: byId('sql-chart-series').value || null,
    };
  }

  /** Load the captured query's complete retained result, never a partial result page. */
  async retainedResult() {
    if (this.completePacket) return this.completePacket;
    if (!this.applied || !this.firstPacket) throw new Error('Run the query before charting it.');
    const applied = { ...this.applied };
    const generation = this.chartGeneration;
    const current = () =>
      this.context.alive() &&
      generation === this.chartGeneration &&
      this.catalog?.model_id === applied.model_id;
    const complete = await collectRetainedResult(
      this.firstPacket,
      (cursor) =>
        this.context.queue.run(
          () =>
            this.context.client.request('/query', {
              method: 'POST',
              body: { ...applied, cursor },
            }),
          current,
        ),
      current,
    );
    if (!current()) throw new StaleRequest();
    this.completePacket = complete;
    return complete;
  }

  /** Draw the last run with explicit axes; incompatible outputs remain usable as tables. */
  async showChart() {
    const generation = this.chartGeneration;
    const selection = ++this.chartSelectionGeneration;
    const config = this.chartConfig();
    visible('sql-chart-status', true);
    setText('sql-chart-status', 'Loading the retained result for its time graph…');
    try {
      const packet = await this.retainedResult();
      if (
        generation !== this.chartGeneration ||
        selection !== this.chartSelectionGeneration ||
        !this.context.alive()
      )
        return;
      renderQueryChart(byId('sql-query-chart'), packet, config, {
        title: 'Query results over time',
        tableId: 'sql-table',
      });
      this.chartShownConfig = { ...config };
      this.chartWidth = byId('sql-query-chart').clientWidth;
      setText(
        'sql-chart-status',
        `${packet.rows.length} retained rows · Version ${packet.model_id.slice(0, 8)}`,
      );
    } catch (error) {
      if (
        error instanceof StaleRequest ||
        generation !== this.chartGeneration ||
        selection !== this.chartSelectionGeneration
      )
        return;
      setText(
        'sql-chart-status',
        error.message || 'The query cannot be plotted with these fields.',
      );
    }
  }

  /** Save a named definition of the last run; edited drafts must be executed first. */
  saveView() {
    if (!this.packet || byId('sql-save-view').disabled) return;
    this.context.savedViews?.openSave(
      {
        dataset_id: this.applied.dataset_id,
        sql: this.applied.sql,
        max_rows: this.applied.max_rows,
        packet: this.completePacket ?? this.firstPacket,
        chart: byId('sql-query-chart').childElementCount
          ? this.chartConfig()
          : (this.savedViewEditing?.chart ?? { enabled: false }),
      },
      this.savedViewEditing ?? null,
    );
  }
  /**
   * Invalidate pending render work and clear captured execution, rendered results and pager state.
   */
  invalidate() {
    this.resetChart();
    this.sequence++;
    this.working = false;
    this.packet = null;
    this.applied = null;
    byId('sql-table').replaceChildren();
    this.updatePager();
  }
  /**
   * Clear query results, drafts, editor text and scoped history on workspace/session disposal.
   */
  dispose() {
    this.disposed = true;
    this.chartObserver?.disconnect();
    this.invalidate();
    this.history = [];
    this.drafts.clear();
    this.editor?.clear();
    this.editor?.destroy();
    this.editor = null;
    this.examples = [];
    byId('sql-examples').replaceChildren();
    byId('sql-schema').replaceChildren();
    byId('sql-editor').value = '';
    this.renderHistory();
    clearPrivateHistory();
  }
}
