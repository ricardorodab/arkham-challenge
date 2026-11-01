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

export const HISTORY_PREFIX = 'outage-explorer:history:';
export function retireHistoryScopes(storage, keepKey = null) {
  for (let index = storage.length - 1; index >= 0; index--) {
    const key = storage.key(index);
    if (key?.startsWith(HISTORY_PREFIX) && key !== keepKey) storage.removeItem(key);
  }
}
export const quoteIdentifier = (value) => `"${String(value).replaceAll('"', '""')}"`;
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

export function clearPrivateHistory() {
  try {
    retireHistoryScopes(sessionStorage);
  } catch {
    /* Storage can be unavailable; in-memory state is cleared separately. */
  }
}

export class SqlController {
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
    byId('sql-editor').addEventListener('input', () => this.updatePager());
    for (const id of ['sql-max-rows', 'sql-page-size'])
      byId(id).addEventListener('change', () => this.updatePager());
    byId('sql-editor').addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        if (!byId('sql-run').disabled) this.run();
      }
      if (event.ctrlKey && event.code === 'Space') {
        event.preventDefault();
        byId('sql-complete').focus();
      }
    });
    byId('sql-example').addEventListener('click', () => {
      if (this.dataset()) {
        byId('sql-editor').value = exampleSql(this.dataset());
        this.updatePager();
        byId('sql-editor').focus();
      }
    });
    byId('sql-prev').addEventListener('click', () => this.send(this.page - 1));
    byId('sql-next').addEventListener('click', () => this.send(this.page + 1));
    byId('sql-export').addEventListener('click', () =>
      exportPage(this.packet, this.applied?.dataset_id, this.catalog?.model_id),
    );
    byId('sql-insert').addEventListener('click', () => this.complete());
    byId('sql-complete').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        this.complete();
      }
    });
    byId('sql-load-history').addEventListener('click', () => this.restoreHistory());
  }

  dataset() {
    return this.catalog?.datasets.find((dataset) => dataset.id === byId('sql-dataset').value);
  }
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

  selectDataset(draft = null) {
    if (this.selected) this.drafts.set(this.selected, byId('sql-editor').value);
    this.selected = byId('sql-dataset').value;
    this.sequence++;
    this.packet = null;
    this.applied = null;
    this.working = false;
    const dataset = this.dataset();
    if (!dataset) return;
    byId('sql-editor').value = draft ?? this.drafts.get(this.selected) ?? exampleSql(dataset);
    schema(byId('sql-schema'), dataset, (name) => this.insert(name));
    const suggestions = byId('sql-suggestions');
    suggestions.replaceChildren();
    for (const name of [dataset.id, ...dataset.schema.map((column) => column[0])]) {
      const option = element('option');
      option.value = name;
      suggestions.append(option);
    }
    state('sql', 'empty');
    setText('sql-empty', 'Your query results will appear here. Run a read-only SELECT to begin.');
    visible('sql-truncated', false);
    byId('sql-run').disabled = false;
    this.updatePager();
  }

  insert(name) {
    const editor = byId('sql-editor');
    editor.setRangeText(quoteIdentifier(name), editor.selectionStart, editor.selectionEnd, 'end');
    editor.focus();
    this.updatePager();
  }
  complete() {
    const value = byId('sql-complete').value.trim();
    const dataset = this.dataset();
    if (!dataset || ![dataset.id, ...dataset.schema.map((column) => column[0])].includes(value)) {
      byId('sql-complete').setCustomValidity('Choose a column or dataset from the suggestions.');
      byId('sql-complete').reportValidity();
      return;
    }
    byId('sql-complete').setCustomValidity('');
    this.insert(value);
    byId('sql-complete').value = '';
  }

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

  async send(page) {
    if (!this.applied || page < 0 || (page > 0 && !this.cursors[page])) return;
    const applied = { ...this.applied },
      sequence = ++this.sequence;
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
  restoreHistory() {
    const index = byId('sql-history').value;
    if (index === '' || !this.history[Number(index)]) return;
    const entry = this.history[Number(index)];
    byId('sql-dataset').value = entry.dataset_id;
    this.selectDataset(entry.sql);
    byId('sql-editor').focus();
  }
  invalidate() {
    this.sequence++;
    this.working = false;
    this.packet = null;
    this.applied = null;
    byId('sql-table').replaceChildren();
    this.updatePager();
  }
  dispose() {
    this.invalidate();
    this.history = [];
    this.drafts.clear();
    byId('sql-editor').value = '';
    this.renderHistory();
    clearPrivateHistory();
  }
}
